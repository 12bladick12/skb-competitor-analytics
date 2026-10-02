"""Search and explicit comparisons over collected products, without fuzzy matching."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from .models import utcnow
from .sources import SOURCES
from .scope import VISIBLE,RESULT_VISIBLE

COMPARISON_PAGE_SIZE=500


PRODUCT_SELECT="""SELECT q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,
    COALESCE(i.title,o.title,'') title,COALESCE(i.category,'') category,
    COALESCE(i.attributes_count,0) attributes_count,
    COALESCE(o.status,'pending') status,o.price,o.currency,o.availability,o.checked_at,o.detail,
    o.url,o.price_text,o.availability_text,o.http_status,o.response_hash,i.updated_at specifications_checked_at,
    p.price last_price,p.currency last_currency,p.checked_at price_checked_at,
    p.price_text last_price_text,p.details_json last_price_details_json,
    c.our_article,c.our_price,c.our_currency,c.note,c.updated_at our_price_updated_at,
    COALESCE(l.state,'active') product_state,l.note product_state_note,l.checked_at lifecycle_checked_at,
    CASE WHEN c.rule_id IS NULL THEN 0 ELSE 1 END selected
    FROM rules q LEFT JOIN product_index i ON i.rule_id=q.id
    LEFT JOIN observations o ON o.id=(SELECT oo.id FROM observations oo JOIN jobs j ON j.id=oo.job_id
        WHERE j.rule_id=q.id ORDER BY oo.checked_at DESC,oo.id DESC LIMIT 1)
    LEFT JOIN observations p ON p.id=(SELECT oo.id FROM observations oo JOIN jobs j ON j.id=oo.job_id
        WHERE j.rule_id=q.id AND oo.status='priced' AND oo.price IS NOT NULL
        ORDER BY oo.checked_at DESC,oo.id DESC LIMIT 1)
    LEFT JOIN comparison_items c ON c.rule_id=q.id
    LEFT JOIN product_lifecycle l ON l.rule_id=q.id"""


def price_value(value):
    if value is None or str(value).strip()=='':return None
    try:
        number=Decimal(str(value).replace('\xa0','').replace(' ','').replace(',','.'))
    except InvalidOperation:
        raise ValueError('Наша цена должна быть числом') from None
    if not number.is_finite() or number<=0 or number>Decimal('1000000000000'):
        raise ValueError('Наша цена должна быть больше нуля и не больше 1 000 000 000 000')
    return format(number,'f')


class Library:
    def __init__(self,repository):self.repo=repository

    def with_specifications(self,rows):
        """Resolve observation hashes in batches, preserving historical snapshots."""
        output=[]
        for offset in range(0,len(rows),100):
            page=[dict(x) for x in rows[offset:offset+100]]
            references=set();current=[]
            for row in page:
                if 'last_price_details_json' not in row:continue
                try:price_payload=json.loads(row.get('last_price_details_json') or '{}')
                except (ValueError,TypeError):price_payload={}
                row['_price_payload']=price_payload
                if price_payload.get('ref'):references.add(price_payload['ref'])
            for row in page:
                if '_specifications' in row:continue
                if 'details_json' not in row:
                    if row.get('rule_id') is not None:current.append(row['rule_id'])
                    continue
                try:payload=json.loads(row.get('details_json') or '{}')
                except (ValueError,TypeError):payload={}
                row['_specifications']=payload if 'ref' not in payload else {}
                if payload.get('ref'):references.add(payload['ref']);row['_details_ref']=payload['ref']
            docs={}
            if references:
                params={f'h{i}':h for i,h in enumerate(references)}
                found=self.repo.batch('SELECT fingerprint,details_json FROM product_documents WHERE fingerprint IN ('+','.join(f'%(h{i})s' for i in range(len(params)))+')',params)
                docs={x['fingerprint']:json.loads(x['details_json']) for x in found}
            latest={}
            if current:
                params={f'id{i}':rid for i,rid in enumerate(current)}
                found=self.repo.batch('SELECT i.rule_id,d.details_json FROM product_index i JOIN product_documents d ON d.fingerprint=i.details_hash WHERE i.rule_id IN ('+','.join(f'%(id{i})s' for i in range(len(params)))+')',params)
                latest={x['rule_id']:json.loads(x['details_json']) for x in found}
            from .passport_review import confirmed
            geometry=confirmed(self.repo,[r['rule_id'] for r in page if 'details_json' not in r and r.get('rule_id') is not None])
            for row in page:
                if '_price_payload' in row:
                    from .price_terms import parse_terms
                    price_payload=row.pop('_price_payload')
                    if price_payload.get('ref'):price_payload=docs.get(price_payload['ref'],{})
                    row['_price_snapshot_terms']=price_payload.get('price_terms') or parse_terms(row.get('last_price_text'))
                ref=row.pop('_details_ref',None)
                if ref:row['_specifications']=docs.get(ref,{})
                elif 'details_json' not in row and '_specifications' not in row:row['_specifications']=latest.get(row.get('rule_id'),{})
                if row.get('rule_id') in geometry and 'details_json' not in row:row['_confirmed_geometry']=geometry[row['rule_id']]
                output.append(row)
        from .enrichment import Enrichment
        for offset in range(0,len(output),100):Enrichment(self.repo).attach(output[offset:offset+100])
        return output

    def summary(self):
        return self.repo.batch(f"""SELECT (SELECT count(*) FROM rules q WHERE {VISIBLE}) products,
            (SELECT count(*) FROM product_index i JOIN rules q ON q.id=i.rule_id WHERE attributes_count>0 AND {VISIBLE}) with_specs,
            (SELECT count(*) FROM comparison_items c JOIN rules q ON q.id=c.rule_id WHERE {VISIBLE}) selected,
            (SELECT max(checked_at) FROM observations) checked_at""")[0]

    def price_terms(self):
        return {r['rule_id']:json.loads(r['terms_json']) for r in self.repo.batch('SELECT rule_id,terms_json FROM comparison_price_terms')}

    def save_price_terms(self, rule_id, ours, competitor):
        for term in (ours, competitor):
            if term.get('basis') not in ('unknown', 'gross', 'net'): raise ValueError('Неизвестные условия НДС')
            rate=term.get('rate')
            if rate is not None and not 0 <= float(rate) <= 100: raise ValueError('Ставка НДС должна быть от 0 до 100%')
            if term.get('unit') not in ('', 'piece'): raise ValueError('Неизвестная единица цены')
        self.repo.batch('''INSERT INTO comparison_price_terms(rule_id,terms_json,updated_at) VALUES(%(id)s,%(terms)s,%(now)s)
            ON CONFLICT(rule_id) DO UPDATE SET terms_json=excluded.terms_json,updated_at=excluded.updated_at''',
            {'id':int(rule_id),'terms':json.dumps({'ours':ours,'competitor':competitor},ensure_ascii=False),'now':utcnow()})

    def products(self,query='',source='',brand='',selected=False,offset=0,limit=50,rule_id=None):
        p={'offset':max(0,int(offset)),'limit':min(1000,max(1,int(limit)))}
        where=[VISIBLE]
        if query.strip():
            for n,word in enumerate(query.casefold().split()[:8]):
                where.append(f"lower(q.article||' '||q.manufacturer||' '||COALESCE(i.title,'')||' '||COALESCE(i.category,'')) LIKE %(word{n})s ESCAPE '!' ")
                p[f'word{n}']='%'+word.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
        if source:where.append('q.source=%(source)s');p['source']=source
        if brand:where.append('q.manufacturer=%(brand)s');p['brand']=brand
        if selected:where.append('c.rule_id IS NOT NULL')
        if rule_id is not None:where.append('q.id=%(rule_id)s');p['rule_id']=int(rule_id)
        sql=PRODUCT_SELECT+' WHERE '+' AND '.join(where)
        count=self.repo.batch('SELECT count(*) total FROM ('+sql+') found',p)[0]['total']
        return count,self.repo.batch(sql+' ORDER BY q.source,q.manufacturer,q.article,q.id LIMIT %(limit)s OFFSET %(offset)s',p)

    def select(self,ids):
        ids=list(dict.fromkeys(int(x) for x in ids))
        for offset in range(0,len(ids),200):
            p={'now':utcnow()};sql=[]
            for i,rule in enumerate(ids[offset:offset+200]):
                p[f'id{i}']=rule
                sql.append(f"INSERT INTO comparison_items(rule_id,updated_at) SELECT id,%(now)s FROM rules WHERE id=%(id{i})s ON CONFLICT(rule_id) DO NOTHING")
            if sql:self.repo.batch(sql,p)

    def save_comparisons(self,rows):
        values=[]
        for row in rows:
            currency=str(row.get('our_currency') or 'RUB').strip().upper()
            if currency not in ('RUB','USD','EUR','CNY'):raise ValueError('Валюта: RUB, USD, EUR или CNY')
            values.append({**row,'our_price':price_value(row.get('our_price')),'our_currency':currency})
        for offset in range(0,len(values),200):
            p={'now':utcnow()};sql=[]
            for i,row in enumerate(values[offset:offset+200]):
                for key in ('rule_id','our_price','our_currency','our_article','note'):
                    p[f'{key}{i}']=row.get(key,'' if key not in ('our_price','rule_id') else None)
                sql.append(f'''DELETE FROM comparison_price_terms WHERE rule_id=%(rule_id{i})s AND EXISTS
                    (SELECT 1 FROM comparison_items c WHERE c.rule_id=%(rule_id{i})s AND c.our_article<>%(our_article{i})s)''')
                sql.append(f'''INSERT INTO comparison_items(rule_id,our_price,our_currency,our_article,note,updated_at)
                    VALUES(%(rule_id{i})s,%(our_price{i})s,%(our_currency{i})s,%(our_article{i})s,%(note{i})s,%(now)s)
                    ON CONFLICT(rule_id) DO UPDATE SET our_price=excluded.our_price,our_currency=excluded.our_currency,
                    our_article=excluded.our_article,note=excluded.note,updated_at=excluded.updated_at''')
            if sql:self.repo.batch(sql,p)

    def remove(self,rule_id):
        self.repo.batch('DELETE FROM comparison_items WHERE rule_id=%(id)s',{'id':int(rule_id)})

    def history(self,rule_ids,start=None,end=None):
        if not rule_ids:return []
        p={f'id{i}':int(x) for i,x in enumerate(rule_ids)}
        conditions=[VISIBLE,'j.rule_id IN ('+','.join(f'%(id{i})s' for i in range(len(rule_ids)))+')']
        if start:conditions.append('o.checked_at>=%(start)s');p['start']=str(start)
        if end:conditions.append('o.checked_at<%(end)s');p['end']=str(end)
        return self.repo.batch('''SELECT j.rule_id,j.run_id,q.source,q.manufacturer,q.article,q.product_url,o.*
            FROM jobs j JOIN observations o ON o.job_id=j.id JOIN rules q ON q.id=j.rule_id
            WHERE '''+' AND '.join(conditions)+' ORDER BY o.checked_at,o.id',p)

    def details(self,rule_id):
        rows=self.repo.batch('''SELECT d.details_json FROM product_index i JOIN product_documents d ON d.fingerprint=i.details_hash WHERE i.rule_id=%(id)s''',{'id':int(rule_id)})
        return json.loads(rows[0]['details_json']) if rows else {}

    def resolve(self,rows):
        """Exact identification only. URL required if multiple rules match."""
        found,errors,seen=[],[],set()
        # One round trip per chunk, including exact identifiers; never download the full catalog.
        for offset in range(0,len(rows),100):
            subset=rows[offset:offset+100];params={};conditions=[]
            for i,row in enumerate(subset):
                params.update({f's{i}':row['source'],f'b{i}':row['manufacturer'],f'a{i}':row['article'].casefold()})
                conditions.append(f'(q.source=%(s{i})s AND q.manufacturer=%(b{i})s AND lower(q.article)=%(a{i})s)')
            options=self.repo.batch('SELECT q.* FROM rules q WHERE '+VISIBLE+' AND ('+' OR '.join(conditions)+')',params) if conditions else []
            for row in subset:
                candidates=[q for q in options if q['source']==row['source'] and q['manufacturer']==row['manufacturer'] and q['article'].casefold()==row['article'].casefold() and (not row.get('product_url') or q['product_url'].rstrip('/')==row['product_url'].rstrip('/'))]
                if len(candidates)!=1:
                    errors.append({'Строка':row['_row'],'Артикул':row['article'],'Ошибка':'Нет в базе: сначала выполните сбор' if not candidates else 'Несколько карточек: укажите product_url'})
                elif candidates[0]['id'] in seen:
                    errors.append({'Строка':row['_row'],'Артикул':row['article'],'Ошибка':'Позиция повторяется в файле'})
                else:
                    seen.add(candidates[0]['id']);found.append({**row,'rule_id':candidates[0]['id']})
        return found,errors

    def result_page(self,run_id,offset=0,limit=100):
        from .monthly import RESULT_FROM, RESULT_STATUS, RESULT_DETAIL
        return self.repo.batch(f'''SELECT j.id job_id,j.run_id,q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,
            {RESULT_STATUS} status,o.price,o.currency,o.availability,o.checked_at,o.http_status,{RESULT_DETAIL} detail,
            o.url,o.title,o.price_text,o.availability_text,o.response_hash,o.details_json,
            reuse.observation_id reused_observation_id,o.status original_status {RESULT_FROM}
            WHERE j.run_id=%(run)s AND '''+RESULT_VISIBLE+''' ORDER BY j.id LIMIT %(limit)s OFFSET %(offset)s''',{'run':run_id,'limit':limit,'offset':offset})

    def result_count(self,run_id):
        from .monthly import RESULT_FROM
        return self.repo.batch('SELECT count(*) total '+RESULT_FROM+' WHERE j.run_id=%(run)s AND '+RESULT_VISIBLE,{'run':run_id})[0]['total']

    def export_products(self,query='',source='',brand='',selected=False):
        rows=[];offset=0
        while True:
            total,page=self.products(query,source,brand,selected,offset,1000)
            rows.extend(self.with_specifications(page));offset+=len(page)
            if offset>=total or not page:break
        return rows

    def comparison_products(self):
        return list(self.iter_comparison_products())

    def iter_search_identities(self):
        """Names for autocomplete, without documents, prices or observation history."""
        after=0;size=5000
        while True:
            rows=self.repo.batch('''SELECT q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,
                COALESCE(i.title,'') title,COALESCE(i.category,'') category,
                i.details_hash _catalog_details_hash,c.our_article,
                CASE WHEN c.rule_id IS NULL THEN 0 ELSE 1 END selected
                FROM rules q LEFT JOIN product_index i ON i.rule_id=q.id
                LEFT JOIN comparison_items c ON c.rule_id=q.id
                WHERE '''+VISIBLE+''' AND q.id>%(after)s ORDER BY q.id LIMIT %(limit)s''',
                {'after':after,'limit':size})
            yield from rows
            if len(rows)<size:break
            after=rows[-1]['rule_id']

    def iter_search_products(self):
        """Read current characteristics only; history is fetched for visible models."""
        from .passport_review import confirmed
        after=0
        size=1000
        while True:
            rows=self.repo.batch('''SELECT q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,
                COALESCE(i.title,'') title,COALESCE(i.category,'') category,
                i.details_hash _catalog_details_hash,d.details_json,
                c.our_article,CASE WHEN c.rule_id IS NULL THEN 0 ELSE 1 END selected
                FROM (SELECT q.* FROM rules q WHERE '''+VISIBLE+''' AND q.id>%(after)s
                    ORDER BY q.id LIMIT %(limit)s) q
                LEFT JOIN product_index i ON i.rule_id=q.id
                LEFT JOIN product_documents d ON d.fingerprint=i.details_hash
                LEFT JOIN comparison_items c ON c.rule_id=q.id ORDER BY q.id''',
                {'after':after,'limit':size})
            if not rows:break
            geometry=confirmed(self.repo,[row['rule_id'] for row in rows])
            for row in rows:
                row['_specifications']=json.loads(row.pop('details_json') or '{}')
                if row['rule_id'] in geometry:row['_confirmed_geometry']=geometry[row['rule_id']]
                yield row
            if len(rows)<size:break
            after=rows[-1]['rule_id']

    def comparison_prices(self,ids):
        """Fetch quote snapshots only for displayed models, preserving failed checks."""
        ids=list(dict.fromkeys(int(value) for value in ids))
        result={}
        for start in range(0,len(ids),100):
            subset=ids[start:start+100]
            quotes=self.comparison_observations(subset)
            latest={row['rule_id']:row for row in quotes if row['_latest_rank']==1}
            priced={row['rule_id']:row for row in quotes if row['_priced_rank']==1
                    and row['status']=='priced' and row['price'] is not None}
            rows=[]
            for rid in subset:
                current=latest.get(rid,{});price=priced.get(rid,{})
                rows.append({'rule_id':rid,'status':current.get('status','pending'),
                    'checked_at':current.get('checked_at'),'price_text':current.get('price_text'),
                    'last_price':price.get('price'),'last_currency':price.get('currency'),
                    'price_checked_at':price.get('checked_at'),'last_price_text':price.get('price_text'),
                    'last_price_details_json':price.get('details_json'),
                    'current_details_json':'{}','current_details_hash':None})
            for row in self._comparison_page(rows):
                row.pop('_specifications',None);row.pop('_catalog_details_hash',None)
                result[row['rule_id']]=row
        return result

    def iter_comparison_products(self):
        """Read bounded cards, quotes and reviews with separate indexed queries."""
        selected="""SELECT q.* FROM rules q
            LEFT JOIN product_index visible_index ON visible_index.rule_id=q.id
            LEFT JOIN product_scope visible_scope ON visible_scope.rule_id=q.id
            WHERE q.id>%(after)s AND (q.source<>'teko' OR
                (visible_scope.state='confirmed' AND visible_scope.manufacturer=%(visible_brand)s
                 AND visible_scope.details_hash=visible_index.details_hash))
            ORDER BY q.id LIMIT %(limit)s"""
        sql="""SELECT q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,
            i.title,COALESCE(i.category,'') category,COALESCE(i.attributes_count,0) attributes_count,
            i.updated_at specifications_checked_at,d.details_json current_details_json,
            i.details_hash current_details_hash,c.our_article,c.our_price,c.our_currency,c.note,
            c.updated_at our_price_updated_at,CASE WHEN c.rule_id IS NULL THEN 0 ELSE 1 END selected,
            COALESCE(l.state,'active') product_state,l.note product_state_note,l.checked_at lifecycle_checked_at
            FROM ("""+selected+""") q LEFT JOIN product_index i ON i.rule_id=q.id
            LEFT JOIN product_documents d ON d.fingerprint=i.details_hash
            LEFT JOIN comparison_items c ON c.rule_id=q.id
            LEFT JOIN product_lifecycle l ON l.rule_id=q.id ORDER BY q.id"""
        from .passport_review import confirmed
        after=0
        while True:
            page=self.repo.batch(sql,{'after':after,'limit':COMPARISON_PAGE_SIZE,'visible_brand':SOURCES['teko'].brands[0]})
            if not page:break
            ids=[row['rule_id'] for row in page]
            quotes=self.comparison_observations(ids)
            latest={row['rule_id']:row for row in quotes if row['_latest_rank']==1}
            priced={row['rule_id']:row for row in quotes if row['_priced_rank']==1 and row['status']=='priced' and row['price'] is not None}
            geometry=confirmed(self.repo,ids)
            for row in page:
                current=latest.get(row['rule_id'],{});price=priced.get(row['rule_id'],{})
                if row.get('title') is None:row['title']=current.get('title') or ''
                row['status']=current.get('status','pending')
                for key in ('price','currency','availability','checked_at','detail','url','price_text',
                            'availability_text','http_status','response_hash'):
                    row[key]=current.get(key)
                for target,source in (('last_price','price'),('last_currency','currency'),('price_checked_at','checked_at'),
                                      ('last_price_text','price_text'),('last_price_details_json','details_json')):
                    row[target]=price.get(source)
                if row['rule_id'] in geometry:row['_confirmed_geometry']=geometry[row['rule_id']]
            yield from self._comparison_page(page)
            if len(page)<COMPARISON_PAGE_SIZE:break
            after=page[-1]['rule_id']

    def comparison_observations(self,ids):
        if not ids:return []
        params={f'id{i}':value for i,value in enumerate(ids)}
        return self.repo.batch("""SELECT * FROM (
            SELECT j.rule_id,o.*,
                row_number() OVER (PARTITION BY j.rule_id ORDER BY o.checked_at DESC,o.id DESC) _latest_rank,
                row_number() OVER (PARTITION BY j.rule_id ORDER BY
                    CASE WHEN o.status='priced' AND o.price IS NOT NULL THEN 0 ELSE 1 END,
                    o.checked_at DESC,o.id DESC) _priced_rank
            FROM jobs j JOIN observations o ON o.job_id=j.id
            WHERE j.rule_id IN ("""+','.join(f'%({key})s' for key in params)+""")
            ) ranked WHERE _latest_rank=1 OR (_priced_rank=1 AND status='priced' AND price IS NOT NULL)""",params)

    def _comparison_page(self,rows):
        """Resolve only this page before passing records to the compact index."""
        docs={};references=set()
        for row in rows:
            details=json.loads(row.pop('current_details_json') or '{}')
            fingerprint=row.pop('current_details_hash')
            row['_catalog_details_hash']=fingerprint
            row['_specifications']=details
            if fingerprint:docs[fingerprint]=details
            payload=json.loads(row.pop('last_price_details_json',None) or '{}')
            row['_price_payload']={'ref':payload['ref']} if payload.get('ref') else {'price_terms':payload.get('price_terms')}
            if payload.get('ref'):references.add(payload['ref'])
            geometry=row.pop('geometry_fingerprint',None)
            fields=row.pop('geometry_fields',None);reviewer=row.pop('geometry_reviewer',None);updated=row.pop('geometry_updated_at',None)
            if geometry:
                row['_confirmed_geometry']={'fingerprint':geometry,'fields':json.loads(fields),
                    'reviewer':reviewer,'updated_at':updated}
        missing=list(references-docs.keys())
        for offset in range(0,len(missing),1000):
            params={f'h{i}':value for i,value in enumerate(missing[offset:offset+1000])}
            found=self.repo.batch('SELECT fingerprint,details_json FROM product_documents WHERE fingerprint IN ('+
                ','.join(f'%({key})s' for key in params)+')',params)
            docs.update({row['fingerprint']:json.loads(row['details_json']) for row in found})
        from .price_terms import parse_terms
        for row in rows:
            payload=row.pop('_price_payload')
            if payload.get('ref'):payload=docs.get(payload['ref'],{})
            row['_price_snapshot_terms']=payload.get('price_terms') or parse_terms(row.get('last_price_text'))
            yield row

    def comparison_reference(self, row):
        """Restore evidence from the same immutable snapshot used by the index."""
        fingerprint=row.get('_catalog_details_hash')
        if not fingerprint:return row
        found=self.repo.batch('SELECT details_json FROM product_documents WHERE fingerprint=%(hash)s',{'hash':fingerprint})
        if not found:raise ValueError('Исходная карточка для подбора недоступна. Обновите базу поиска.')
        return {**row,'_specifications':json.loads(found[0]['details_json'])}
