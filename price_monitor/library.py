"""Search and explicit comparisons over collected products, without fuzzy matching."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from .models import utcnow
from .sources import SOURCES


PRODUCT_SELECT="""SELECT q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,
    COALESCE(i.title,o.title,'') title,COALESCE(i.category,'') category,
    COALESCE(i.attributes_count,0) attributes_count,
    COALESCE(o.status,'pending') status,o.price,o.currency,o.availability,o.checked_at,o.detail,
    o.url,o.price_text,o.availability_text,o.http_status,o.response_hash,i.updated_at specifications_checked_at,
    p.price last_price,p.currency last_currency,p.checked_at price_checked_at,
    c.our_article,c.our_price,c.our_currency,c.note,c.updated_at our_price_updated_at,
    CASE WHEN c.rule_id IS NULL THEN 0 ELSE 1 END selected
    FROM rules q LEFT JOIN product_index i ON i.rule_id=q.id
    LEFT JOIN observations o ON o.id=(SELECT oo.id FROM observations oo JOIN jobs j ON j.id=oo.job_id
        WHERE j.rule_id=q.id ORDER BY oo.checked_at DESC,oo.id DESC LIMIT 1)
    LEFT JOIN observations p ON p.id=(SELECT oo.id FROM observations oo JOIN jobs j ON j.id=oo.job_id
        WHERE j.rule_id=q.id AND oo.status='priced' AND oo.price IS NOT NULL
        ORDER BY oo.checked_at DESC,oo.id DESC LIMIT 1)
    LEFT JOIN comparison_items c ON c.rule_id=q.id"""


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

    def summary(self):
        return self.repo.batch("""SELECT (SELECT count(*) FROM rules) products,
            (SELECT count(*) FROM product_index WHERE attributes_count>0) with_specs,
            (SELECT count(*) FROM comparison_items) selected,
            (SELECT max(checked_at) FROM observations) checked_at""")[0]

    def products(self,query='',source='',brand='',selected=False,offset=0,limit=50,rule_id=None):
        p={'offset':max(0,int(offset)),'limit':min(1000,max(1,int(limit)))}
        where=['1=1']
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
        conditions=['j.rule_id IN ('+','.join(f'%(id{i})s' for i in range(len(rule_ids)))+')']
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
            options=self.repo.batch('SELECT q.* FROM rules q WHERE '+' OR '.join(conditions),params) if conditions else []
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
        return self.repo.batch('''SELECT j.id job_id,j.run_id,q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,
            COALESCE(o.status,j.state) status,o.price,o.currency,o.availability,o.checked_at,o.http_status,o.detail,
            o.url,o.title,o.price_text,o.availability_text,o.response_hash,o.details_json
            FROM jobs j JOIN rules q ON q.id=j.rule_id LEFT JOIN observations o ON o.job_id=j.id
            WHERE j.run_id=%(run)s ORDER BY j.id LIMIT %(limit)s OFFSET %(offset)s''',{'run':run_id,'limit':limit,'offset':offset})

    def export_products(self,query='',source='',brand='',selected=False):
        rows=[];offset=0
        while True:
            total,page=self.products(query,source,brand,selected,offset,1000)
            if page:
                params={f'id{i}':row['rule_id'] for i,row in enumerate(page)}
                documents=self.repo.batch('''SELECT i.rule_id,d.details_json FROM product_index i
                    JOIN product_documents d ON d.fingerprint=i.details_hash WHERE i.rule_id IN ('''+','.join(f'%(id{i})s' for i in range(len(page)))+')',params)
                by_id={row['rule_id']:json.loads(row['details_json']) for row in documents}
                for row in page:
                    detail=by_id.get(row['rule_id'],{})
                    row['characteristics_json']=json.dumps(detail.get('attributes',[]),ensure_ascii=False)
                    row['description']=detail.get('description','')
                    row['documents_json']=json.dumps(detail.get('documents',[]),ensure_ascii=False)
            rows.extend(page);offset+=len(page)
            if offset>=total or not page:break
        return rows
