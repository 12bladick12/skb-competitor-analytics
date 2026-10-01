"""Search and explicit comparisons over collected products, without fuzzy matching."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from .models import utcnow
from .sources import SOURCES
from .scope import VISIBLE,RESULT_VISIBLE


PRODUCT_SELECT="""SELECT q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,
    COALESCE(i.title,o.title,'') title,COALESCE(i.category,'') category,
    COALESCE(i.attributes_count,0) attributes_count,
    COALESCE(o.status,'pending') status,o.price,o.currency,o.availability,o.checked_at,o.detail,
    o.url,o.price_text,o.availability_text,o.http_status,o.response_hash,i.updated_at specifications_checked_at,
    p.price last_price,p.currency last_currency,p.checked_at price_checked_at,
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
