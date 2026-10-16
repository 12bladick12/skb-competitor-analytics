"""Authorized, paginated archive reads; private pilot data is admin-only."""
from cloud.access import current_access
from .gate import released
from .store import CourtStore


class CourtService:
    def __init__(self, settings, identity, store_factory=CourtStore):
        self.settings=settings;self.identity=identity;self.store_factory=store_factory

    def context(self, import_id):
        config=self.settings();access=current_access(self.identity(),config)
        if not access.allowed:raise PermissionError('Доступ отозван.')
        store=self.store_factory(config)
        active=store.query("SELECT import_id FROM skb_analytics.state WHERE key='active'")
        if not active or active[0]['import_id']!=import_id:raise ValueError('Набор данных изменился.')
        if access.role!='admin' and not released(store.get(import_id,'court_release','public')):
            raise PermissionError('Судебный раздел ещё проходит проверку.')
        return store,access

    def archive(self, import_id, *, kind='court_case', entity='', role='', jurisdiction='', status='',
                query='', date_from='', date_to='', amount_from='', amount_to='', currency='', brand='', offset=0):
        if kind not in ('court_case','court_enforcement'):raise ValueError('Неизвестный вид реестра.')
        store,_=self.context(import_id)
        field='claim_amount' if kind=='court_case' else 'balance'
        condition='''r.import_id=$1 AND r.kind=$2
          AND ($3='' OR r.payload->'entity_ids' ? $3)
          AND ($4='' OR EXISTS(SELECT 1 FROM jsonb_array_elements(r.payload->'participants') p
               WHERE p->>'role'=$4 AND r.payload->'entity_ids' ? (p->>'inn') AND ($3='' OR p->>'inn'=$3)
                 AND ($13='' OR EXISTS(SELECT 1 FROM skb_analytics.records e WHERE e.import_id=r.import_id
                     AND e.kind='court_entity' AND e.key=p->>'inn' AND e.payload->>'brand'=$13))))
          AND ($5='' OR r.payload->>'jurisdiction'=$5)
          AND ($6='' OR r.payload->>'status'=$6)
          AND ($7='' OR strpos(lower(r.payload->>'number' || ' ' || COALESCE(r.payload->>'subject','')),lower($7))>0)
          AND ($8='' OR r.payload->>'registered_on'>=$8)
          AND ($9='' OR r.payload->>'registered_on'<=$9)
          AND ($10='' OR (r.payload->'FIELD'->>'amount')::numeric>=NULLIF($10,'')::numeric)
          AND ($11='' OR (r.payload->'FIELD'->>'amount')::numeric<=NULLIF($11,'')::numeric)
          AND ($12='' OR r.payload->'FIELD'->>'currency'=$12)
          AND ($13='' OR EXISTS(SELECT 1 FROM skb_analytics.records e WHERE e.import_id=r.import_id
               AND e.kind='court_entity' AND e.payload->>'brand'=$13 AND r.payload->'entity_ids' ? e.key))'''.replace('FIELD',field)
        params=[import_id,kind,entity,role,jurisdiction,status,query,date_from,date_to,str(amount_from),str(amount_to),currency,brand]
        count=store.query('SELECT count(*) total FROM skb_analytics.records r WHERE '+condition,params)[0]['total']
        rows=store.query('SELECT payload FROM skb_analytics.records r WHERE '+condition+
                         ' ORDER BY r.payload->>\'registered_on\' DESC NULLS LAST,r.key LIMIT 20 OFFSET $14',params+[max(0,int(offset))])
        return int(count),[r['payload'] for r in rows]

    def overview(self, import_id):
        store,_=self.context(import_id)
        counts=store.query('''SELECT kind,count(DISTINCT CASE WHEN kind='court_event' THEN payload->>'id' ELSE key END) total
            FROM skb_analytics.records WHERE import_id=$1
            AND kind IN ('court_case','court_event','court_enforcement','court_candidate') GROUP BY kind''',[import_id])
        return {'counts':{r['kind']:int(r['total']) for r in counts},
                'entities':list(store.all(import_id,'court_entity')),'checks':list(store.all(import_id,'court_check')),
                'release':store.get(import_id,'court_release','public')}

    def details(self, import_id, case_id, kind, offset=0):
        if kind not in ('court_event','court_document','court_enforcement'):raise ValueError('Неизвестный раздел.')
        store,_=self.context(import_id)
        return store.records(import_id,kind,case_id=case_id,limit=50,offset=max(0,int(offset)))
