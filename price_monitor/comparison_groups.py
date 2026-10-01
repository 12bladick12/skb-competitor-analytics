"""Compare every brand directly with a reference; prices never influence matching."""
from collections import defaultdict
from datetime import datetime, timezone

from .automatic_price_terms import current_terms
from .matching import evaluate, rank
from .matching_normalize import normalize_sensor, model_key
from .price_terms import positive, price_views, terms_for

OUR_BRAND='СКБ Индукция'
BRAND_COLORS={OUR_BRAND:'#18756A','ТЕКО':'#7A1F2B','BESKONTA':'#BE661B','МЕГА-К':'#5B52A3',
              'СЕНСОР':'#2364AA','Autonics':'#CA4672','Balluff':'#597D25','Pepperl+Fuchs':'#925F46',
              'ifm':'#C87C00','LANBAO':'#228EAA','SICK':'#5F718B'}


def competitor_record(row, *, compact=False, lazy_details=False):
    sensor=normalize_sensor(row)
    if compact:
        # Every catalog row needs typed values, not a second copy of the full
        # decoder/evidence tree. Rehydrate the reference only when comparing it.
        sensor.raw={}
        sensor.decoding={key:sensor.decoding.get(key) for key in ('supported','requires_review')}
        keys=('rule_id','source','manufacturer','article','product_url','title','category','model',
              'last_price','last_currency','price_checked_at','checked_at','status','our_article',
              'selected','props','_confirmed_geometry','_price_snapshot_terms','price_text','last_price_text','_catalog_details_hash')
        details=row.get('_specifications') or {}
        row={key:row[key] for key in keys if key in row}
        row['_specifications']={key:details[key] for key in ('attributes','description','category','price_terms') if key in details}
        if lazy_details and row.get('_catalog_details_hash'):
            from .catalog_search import characteristic_values
            dimensions=characteristic_values({**row,'sensor':sensor}).get('dimensions')
            if dimensions:row['_search_dimensions']=dimensions
            row.pop('props',None)
            row['_specifications']={key:details[key] for key in ('price_terms',) if key in details}
    return {**row,'entry_id':'competitor:'+str(row['rule_id']),'brand':row['manufacturer'],
            'model':row['article'],'is_ours':False,'sensor':sensor}


def own_record(sensor, price=None):
    return {'entry_id':'ours:'+sensor.id,'brand':OUR_BRAND,'model':sensor.model,'article':sensor.article,
            'is_ours':True,'sensor':sensor,'own_price':price,'catalog_id':sensor.id}


class IndexedMatch:
    """Keep rankings compact; full field evidence is needed for chosen rows only."""
    def __init__(self, reference, match):
        self.reference=reference
        self.candidate=match.candidate
        self.status=match.status
        self.profile=match.profile
        self.priority=match.priority

    def __getattr__(self, name):
        match=evaluate(self.reference,self.candidate,self.profile)
        match.priority=self.priority
        return getattr(match,name)


class ComparisonIndex:
    def __init__(self, competitors, own_sensors, own_prices, unmatched_prices=(), reference_loader=None):
        self.records={}
        self.buckets=defaultdict(list)
        self._alternatives={}
        self.reference_loader=reference_loader
        for row in competitors:
            self.add(competitor_record(row,compact=True,lazy_details=reference_loader is not None))
        for sensor in own_sensors:
            self.add(own_record(sensor,own_prices.get(sensor.id)))
        for price in unmatched_prices:
            sensor=normalize_sensor({'code':'price:'+price['article'],'model':price['source_model'],
                                     'article':price['article'],'manufacturer':OUR_BRAND})
            self.add(own_record(sensor,price))

    def add(self, record):
        self.records[record['entry_id']]=record
        values=record['sensor'].values
        self.buckets[(values.get('body_type'),values.get('diameter'))].append(record)

    def search(self, query):
        words=[model_key(w) for w in query.split() if w]
        exact=model_key(query)
        rows=[r for r in self.records.values() if all(w in model_key(r['brand']+' '+r['model']+' '+r.get('article','')) for w in words)]
        return sorted(rows,key=lambda r:(model_key(r['model'])!=exact and model_key(r.get('article'))!=exact,
                                        not r['is_ours'],r['brand'],r['model']))

    def alternatives(self, anchor, profile='auto'):
        cache_key=(anchor['entry_id'],profile)
        if cache_key in self._alternatives:return self._alternatives[cache_key]
        reference=self.reference_loader(anchor) if self.reference_loader and not anchor['is_ours'] else anchor
        sensor=anchor['sensor'] if anchor['is_ours'] else normalize_sensor(reference)
        v=sensor.values
        if sensor.family not in (None,'inductive') or not any(v.get(k) is not None for k in ('diameter','output','sn')):
            return {}
        candidates=[r for (body,diameter),rows in self.buckets.items()
                    if (v.get('body_type') is None or body is None or body==v['body_type'])
                    and (v.get('diameter') is None or diameter is None or diameter==v['diameter']) for r in rows]
        by_brand=defaultdict(list)
        # Evaluate against the actual searched model, never transitively via SKB.
        for row in candidates:
            if row['entry_id']==anchor['entry_id'] or row['brand']==anchor['brand']:
                continue
            # A missing diameter/output cannot make an M8/NAMUR candidate an
            # apparent M18/PNP analogue simply by passing the conflict filter.
            critical=('diameter','output','function')
            if any(v.get(k) is not None and row['sensor'].values.get(k) is None for k in critical):
                continue
            match=evaluate(sensor,row['sensor'],profile)
            if match.status not in ('incompatible','unsupported'):
                by_brand[row['brand']].append((row,match))
        result={}
        for brand,pairs in by_brand.items():
            # Rank per manufacturer; duplicate dealer identities stay distinct.
            ranked=rank([m for _,m in pairs])
            order={id(m):i for i,m in enumerate(ranked)}
            # Unknown mandatory fields cannot make an unrelated, poorly described
            # item the default ahead of an otherwise compatible documented one.
            status_order={'direct':0,'close':1,'review':2}
            ordered=sorted(pairs,key=lambda pair:(status_order[pair[1].status],
                sum(f['outcome']=='missing' and f['group']=='Обязательные' for f in pair[1].fields),
                order[id(pair[1])]))
            result[brand]=[(row,IndexedMatch(sensor,match)) for row,match in ordered]
        # Keep only a bounded number of reviewed groups in the shared index.
        if len(self._alternatives)>=6:self._alternatives.pop(next(iter(self._alternatives)))
        self._alternatives[cache_key]=result
        return result


def price_info(record):
    if record['is_ours']:
        row=record.get('own_price') or {}
        terms={'basis':'net','rate':float(row['vat_rate']) if row else None,'unit':row.get('unit',''),
               'source_url':'','evidence':'Прайс СКБ: исходные цены без НДС; ставка 22% задана владельцем.',
               'checked_at':row.get('imported_at')}
        return {'raw':positive(row.get('net_price')),'internet':positive(row.get('net_price')),
                'gross':positive(row.get('gross_price')),'net':positive(row.get('net_price')),
                'currency':row.get('currency','RUB'),'date':row.get('effective_date'),'terms':terms,
                'source':row.get('source_name','Цена СКБ не загружена'),'source_url':'',
                'source_row':row.get('source_row'),'source_sheet':row.get('source_sheet')}
    terms=current_terms(record)
    verified=record.get('_automatic_price_terms') or {}
    verified_date=verified.get('checked_at') if verified.get('verified_price') is not None else None
    return {'raw':positive(record.get('last_price')),**price_views(record.get('last_price'),terms),
            'currency':record.get('last_currency') or record.get('currency') or 'RUB',
            'date':verified_date or record.get('price_checked_at'),'terms':terms,
            'source':record.get('source'),'source_url':record.get('product_url')}


def delta_between(record, baseline, basis='gross'):
    a,b=price_info(record),price_info(baseline)
    if a['raw'] is None or b['raw'] is None:
        return None,None,'Нет цены выбранной модели'
    if a['currency']!=b['currency']:
        return None,None,'Разные валюты'
    u,v=a['terms'].get('unit'),b['terms'].get('unit')
    if u and v and u!=v:
        return None,None,'Разные единицы продажи'
    left,right=a.get(basis),b.get(basis)
    notes=[]
    if basis=='internet' or left is None or right is None:
        left,right=a['raw'],b['raw']
        same=a['terms'].get('basis') in ('net','gross') and a['terms'].get('basis')==b['terms'].get('basis')
        if not same:notes.append('По опубликованным ценам; НДС не выровнен')
    if not u or not v:notes.append('Единица цены не указана источником')
    # Unknown conditions are disclosed, rather than represented as confirmed.
    return left-right,(left-right)/right*100,' · '.join(notes)


def history_points(record, history, basis):
    points=[];segment=0
    if record['is_ours']:
        info=price_info(record)
        if info.get(basis) is not None and info['date']:
            points.append({'date':info['date']+'T00:00:00+00:00','price':info[basis],'segment':'0',
                           'model':record['model'],'brand':record['brand'],'entry_id':record['entry_id']})
        return points
    currency=price_info(record)['currency']
    for row in sorted(history,key=lambda r:(r.get('checked_at',''),r.get('id',0))):
        terms=terms_for(row)
        value=price_views(row.get('price'),terms).get(basis)
        if row.get('status')!='priced' or row.get('currency')!=currency or value is None:
            segment+=1;continue
        points.append({'date':row['checked_at'],'price':value,'segment':str(segment),'model':record['model'],
                       'brand':record['brand'],'entry_id':record['entry_id']})
    # A recheck is a NEW actual quote. Today's VAT never re-labels an old point.
    verified=record.get('_automatic_price_terms') or {}
    value=price_views(verified.get('verified_price'),verified).get(basis)
    stamp=verified.get('checked_at')
    if stamp and value is not None and verified.get('verified_currency')==currency and not any(p['date']==stamp for p in points):
        points.append({'date':stamp,'price':value,'segment':str(segment+1),'model':record['model'],
                       'brand':record['brand'],'entry_id':record['entry_id']})
    return points
