"""Conservative lifecycle tracking over completed, scoped catalog manifests."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

from .models import normalize, utcnow
from .passports import digest


class ProductState:
    def __init__(self, repository): self.repo = repository

    def bootstrap(self, source):
        # Old observations remain immutable. These dates are their real dates,
        # not the time this additive migration happens to run.
        self.repo.batch(['''INSERT INTO product_lifecycle(rule_id,first_seen,last_seen,checked_at,current_url)
            SELECT q.id,min(o.checked_at),max(o.checked_at),max(o.checked_at),q.product_url
            FROM rules q JOIN jobs j ON j.rule_id=q.id JOIN observations o ON o.job_id=j.id
            WHERE q.source=%(source)s AND o.http_status=200 AND o.status IN ('priced','on_request','no_price')
            GROUP BY q.id,q.product_url ON CONFLICT(rule_id) DO NOTHING''',
            '''INSERT INTO product_aliases(source,url,article,rule_id)
            SELECT q.source,q.product_url,q.article,q.id FROM rules q JOIN product_lifecycle l ON l.rule_id=q.id
            WHERE q.source=%(source)s ON CONFLICT(source,url,article) DO NOTHING'''], {'source': source})

    def lookup(self,results):
        known = {}
        for offset in range(0,len(results),100):
            params={}; conditions=[]
            for n,(rule,_) in enumerate(results[offset:offset+100]):
                params.update({f's{n}':rule.source,f'b{n}':rule.manufacturer,f'a{n}':rule.article})
                conditions.append(f'(source=%(s{n})s AND manufacturer=%(b{n})s AND article=%(a{n})s)')
            rows=self.repo.batch('SELECT id,source,manufacturer,article,product_url,url_template FROM rules WHERE '+' OR '.join(conditions),params)
            for row in rows:known.setdefault((row['source'],row['manufacturer'],row['article']),[]).append(row)
        return known

    def identities(self, results, original_url=''):
        """A parsed full article AND manufacturer must match uniquely. No fuzzy merge."""
        output = [];known=self.lookup(results)
        for rule, observation in results:
            matches = known.get((rule.source,rule.manufacturer,rule.article),[])
            if len(matches) == 1 and matches[0]['product_url']==original_url:
                rule = replace(rule, product_url=matches[0]['product_url'], url_template=matches[0]['url_template'])
            output.append((rule, observation))
        return output

    def relocated(self,results,original_url,client):
        """Verify a moved URL before inserting a second identity for the same model."""
        from .transport import FetchError
        checked={};output=[];by_model=self.lookup(results)
        for rule,observation in results:
            known=by_model.get((rule.source,rule.manufacturer,rule.article),[])
            if len(known)!=1 or known[0]['product_url'] in (original_url,rule.url):
                output.append((rule,observation));continue
            previous=known[0]
            aliases=self.repo.batch('SELECT 1 FROM product_aliases WHERE rule_id=%(id)s AND url=%(url)s',
                                    {'id':previous['id'],'url':observation.url})
            confirmed=bool(aliases)
            if not confirmed:
                old=previous['product_url']
                if old not in checked:
                    try:
                        target,code,_=client.fetch_document(old,html_only=True)
                        checked[old]=(target,code)
                    except FetchError:checked[old]=('',0)
                target,code=checked[old]
                # Full manufacturer/article in the new card is already parsed.
                # The old URL must redirect to it or have actually disappeared.
                confirmed=(code==200 and target==observation.url) or code in (404,410)
            if confirmed:rule=replace(rule,product_url=previous['product_url'],url_template=previous['url_template'])
            output.append((rule,observation))
        return output

    def capture(self, run_id, source, page_id, results, owner, defer=False):
        params = self.repo.params(run_id, source, owner)
        params['page'] = page_id
        guard = self.repo.allowed()
        accumulated=[]
        # Batch per page; variant-heavy pages do not issue one cloud request per field.
        for offset in range(0, len(results), 100):
            sql = []
            for n, (rule, observation) in enumerate(results[offset:offset+100]):
                if observation.status not in ('priced', 'on_request', 'no_price') or observation.http_status != 200: continue
                prefix = f'life{offset+n}_'
                data = {'key': rule.key, 'article': rule.article, 'url': observation.url,
                        'checked': observation.checked_at, 'state': 'discontinued' if observation.availability == 'discontinued' else 'active',
                        'event': digest(['seen', rule.key, observation.checked_at]),
                        'change': digest(['specifications', rule.key, observation.details_json])}
                params.update({prefix+k: v for k,v in data.items()})
                v = lambda k: f'%({prefix}{k})s'
                rid = f'(SELECT id FROM rules WHERE rule_key={v("key")})'
                sql += [f'''INSERT INTO product_events(id,rule_id,kind,detail,created_at)
                    SELECT {v('event')},{rid},CASE WHEN l.state='archived' THEN 'restored' ELSE 'discovered' END,{v('url')},{v('checked')}
                    FROM rules q LEFT JOIN product_lifecycle l ON l.rule_id=q.id
                    WHERE q.rule_key={v('key')} AND (l.rule_id IS NULL OR l.state='archived') AND {guard} ON CONFLICT(id) DO NOTHING''',
                    f'''INSERT INTO product_lifecycle(rule_id,first_seen,last_seen,checked_at,current_url,state)
                    SELECT {rid},{v('checked')},{v('checked')},{v('checked')},{v('url')},{v('state')} WHERE {guard}
                    ON CONFLICT(rule_id) DO UPDATE SET last_seen=excluded.last_seen,checked_at=excluded.checked_at,
                    current_url=excluded.current_url,state=excluded.state,missing_count=0,missing_kind='',missing_since=NULL,last_missing_check=NULL,note='' ''',
                    f'''INSERT INTO product_aliases(source,url,article,rule_id) SELECT %(source)s,{v('url')},{v('article')},{rid}
                    WHERE {guard} ON CONFLICT(source,url,article) DO NOTHING''',
                    f'''INSERT INTO product_events(id,rule_id,kind,detail,created_at)
                    SELECT {v('change')},{rid},'specifications',{v('url')},{v('checked')} WHERE {guard} ON CONFLICT(id) DO NOTHING''']
            accumulated.extend(sql)
        if self.complete_variants(results):
            accumulated.append('''INSERT INTO catalog_variant_sets(run_id,source,url,articles_json,checked_at)
                SELECT %(run)s,%(source)s,%(url)s,%(articles)s,%(now)s WHERE '''+guard+'''
                ON CONFLICT(run_id,source,url) DO UPDATE SET articles_json=excluded.articles_json,checked_at=excluded.checked_at''')
            params.update(url=results[0][1].url,articles=json.dumps([r.article for r,o in results]))
        if defer:return accumulated,params
        if accumulated:self.repo.batch(accumulated,params)

    @staticmethod
    def complete_variants(results):
        return bool(results) and all(r.source=='beskonta' and o.status in ('priced','on_request','no_price')
            and json.loads(o.details_json).get('variant_set_complete') for r,o in results)

    def variant_absence(self, run_id, source, url, articles, owner):
        p=self.repo.params(run_id,source,owner)
        p.update(url=url,before=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='seconds'))
        if not self.repo.batch("SELECT 1 FROM catalog_integrity WHERE run_id=%(run)s AND source=%(source)s AND state='complete'",p):return
        rows=self.repo.batch('''SELECT q.id,q.article,l.last_missing_check,l.missing_kind,l.missing_count
            FROM rules q JOIN product_lifecycle l ON l.rule_id=q.id WHERE q.source=%(source)s AND l.current_url=%(url)s''',p)
        for row in rows:
            if row['article'] in articles:continue
            self.absent(row,p,'variant','Исполнение отсутствует в полностью прочитанном составе карточки')

    def absent(self,row,p,kind,detail):
        if row['last_missing_check'] and row['last_missing_check']>p['before']:return
        count=row['missing_count']+1 if row['missing_kind']==kind else 1
        params={**p,'id':row['id'],'state':'archived' if count>=2 else 'review','count':count,'kind':kind,
                'detail':detail+f'; независимая проверка {count}','event':digest(['absence',row['id'],p['now'],kind])}
        self.repo.batch(['''UPDATE product_lifecycle SET state=%(state)s,missing_count=%(count)s,missing_kind=%(kind)s,
            missing_since=COALESCE(missing_since,%(now)s),last_missing_check=%(now)s,checked_at=%(now)s,note=%(detail)s
            WHERE rule_id=%(id)s AND '''+self.repo.allowed(),
            '''INSERT INTO product_events(id,rule_id,kind,detail,created_at)
            SELECT %(event)s,%(id)s,%(state)s,%(detail)s,%(now)s WHERE '''+self.repo.allowed()+' ON CONFLICT(id) DO NOTHING'],params)

    def manifest(self, run_id, source, owner, brands):
        """Queue targeted checks only once all navigation completed successfully."""
        from .catalog import page_id
        p = {**self.repo.params(run_id, source, owner), 'brands': json.dumps(sorted(brands)),
             'before': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='seconds')}
        if self.repo.cancelled(run_id, source, owner): return False
        existing = self.repo.batch('SELECT state FROM catalog_integrity WHERE run_id=%(run)s AND source=%(source)s', p)
        if existing: return False
        pages = self.repo.batch('SELECT url,kind,state,http_status FROM catalog_pages WHERE run_id=%(run)s AND source=%(source)s', p)
        navigation = [r for r in pages if r['kind'] in ('sitemap','listing')]
        products = {r['url'] for r in pages if r['kind']=='product'}
        complete = bool(navigation and products) and all(r['state']=='done' and r['http_status']==200 for r in navigation)
        # A product fetch failure doesn't erase a manifest, but no products are
        # archived based on it without an independent address verification.
        p.update(state='complete' if complete else 'incomplete', note='' if complete else 'Не подтверждён полный обход навигации; архивирование отключено')
        self.repo.batch('''INSERT INTO catalog_integrity(run_id,source,state,brands_json,checked_at,note)
            SELECT %(run)s,%(source)s,%(state)s,%(brands)s,%(now)s,%(note)s WHERE '''+self.repo.allowed()+
            ' ON CONFLICT(run_id,source) DO NOTHING', p)
        if not complete: return False
        self.bootstrap(source)
        for page in pages:
            if page['kind']=='product' and page['http_status'] in (404,410):
                self.verify(run_id,source,page['url'],page['http_status'],[],owner)
        variants=self.repo.batch('SELECT * FROM catalog_variant_sets WHERE run_id=%(run)s AND source=%(source)s',p)
        for row in variants:self.variant_absence(run_id,source,row['url'],json.loads(row['articles_json']),owner)
        rows = self.repo.batch('''SELECT q.id,q.manufacturer,q.product_url,l.current_url,l.state,l.last_missing_check
            ,l.missing_kind FROM rules q JOIN product_lifecycle l ON l.rule_id=q.id WHERE q.source=%(source)s''', p)
        missing = set()
        for row in rows:
            if row['manufacturer'] not in brands: continue
            url = row['current_url'] or row['product_url']
            if url in products and row['state']!='archived' and row['missing_kind']!='variant': continue
            if row['last_missing_check'] and row['last_missing_check'] > p['before']: continue
            missing.add(url)
            self.repo.batch("UPDATE product_lifecycle SET state=CASE WHEN state='archived' THEN state ELSE 'review' END,note='Ссылка отсутствует в завершённом обходе' WHERE rule_id=%(id)s AND "+self.repo.allowed(), {**p,'id':row['id']})
        self.repo.add_pages(run_id, source, [('verify',url) for url in sorted(missing)], owner)
        for url in missing:
            self.repo.batch("UPDATE catalog_pages SET kind='verify',state='pending' WHERE run_id=%(run)s AND source=%(source)s AND url=%(url)s AND state='cached' AND "+self.repo.allowed(),{**p,'url':url})
        return bool(missing)

    def verify(self, run_id, source, original_url, code, results, owner):
        """No prices here. Two independent, spaced absence checks retain history."""
        p = self.repo.params(run_id, source, owner)
        p.update(url=original_url, before=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='seconds'))
        source_row = self.repo.source(run_id, source)
        brands = json.loads(source_row['brands_json']) if source_row else []
        rows = self.repo.batch('''SELECT q.*,l.state,l.current_url,l.missing_count,l.last_missing_check,l.missing_kind
            FROM rules q JOIN product_lifecycle l ON l.rule_id=q.id WHERE q.source=%(source)s
            AND (l.current_url=%(url)s OR (l.current_url='' AND q.product_url=%(url)s))''', p)
        successful = {(r.manufacturer, normalize(r.article)): o for r,o in results
                      if o.http_status==200 and o.status in ('priced','on_request','no_price')}
        for row in rows:
            if row['manufacturer'] not in brands: continue
            found = successful.get((row['manufacturer'],normalize(row['article'])))
            params = {**p, 'id':row['id'], 'event':digest(['lifecycle',row['id'],p['now'],code])}
            if code==200 and found:
                params['target'] = found.url
                params['restored_state']='discontinued' if found.availability=='discontinued' else 'active'
                self.repo.batch(["""UPDATE product_lifecycle SET state=%(restored_state)s,checked_at=%(now)s,last_seen=%(now)s,
                    missing_count=0,missing_kind='',missing_since=NULL,last_missing_check=NULL,current_url=%(target)s,
                    note='Карточка доступна; ссылка отсутствует в перечне каталога' WHERE rule_id=%(id)s AND """+self.repo.allowed(),
                    '''INSERT INTO product_aliases(source,url,article,rule_id) SELECT source,%(target)s,article,id FROM rules
                    WHERE id=%(id)s AND '''+self.repo.allowed()+' ON CONFLICT(source,url,article) DO NOTHING'],params)
            elif code in (404,410):
                if self.repo.batch("SELECT 1 FROM catalog_integrity WHERE run_id=%(run)s AND source=%(source)s AND state='complete'",p):
                    self.absent(row,p,'http',f'HTTP {code}')
            else:
                # An empty/changed variant table is not proof of deletion.
                self.repo.batch("UPDATE product_lifecycle SET note='Не удалось подтвердить наличие исполнения; нужна проверка' WHERE rule_id=%(id)s AND "+self.repo.allowed(),params)
        if code==200 and self.complete_variants(results):
            self.variant_absence(run_id,source,original_url,[r.article for r,o in results],owner)
