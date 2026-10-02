"""Durable automatic characteristics, separate from immutable source snapshots."""
import json
import logging
import threading

REVISION = 3
VERSION = 'automatic-characteristics-2026-10-01-v3'
EXPECTED_RULES = 'notation-registry-2026-10-01-v4'
EXPECTED_MEGAK = 'megak-ps-vb-2026-10-01-v2'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS product_enrichment (
 rule_id INTEGER PRIMARY KEY REFERENCES rules(id), details_hash TEXT NOT NULL,
 source TEXT NOT NULL, manufacturer TEXT NOT NULL, article TEXT NOT NULL,
 title TEXT NOT NULL, category TEXT NOT NULL,
 revision INTEGER NOT NULL, version TEXT NOT NULL, payload_json TEXT NOT NULL,
 filled_count INTEGER NOT NULL, checked_at TEXT NOT NULL
);
'''


def current_version():
    from .notations import VERSION as rules
    from .megak_notation import VERSION as megak_rules
    # A rolling Cloud deployment can retain imported modules in old workers.
    # Never stamp their results as the current generation. Increment REVISION
    # for subsequent enrichment releases; database CAS fences older replicas.
    if rules!=EXPECTED_RULES or megak_rules!=EXPECTED_MEGAK:raise RuntimeError('Designation modules require a process reload')
    return VERSION+'/'+rules


def characteristics(record):
    from .matching_normalize import normalize_sensor
    from .matching import FIELD_LABELS,display
    sensor=normalize_sensor(record)
    decoded=sensor.decoding
    expected=EXPECTED_MEGAK if decoded.get('brand')=='МЕГА-К' else EXPECTED_RULES
    if decoded and decoded.get('version')!=expected:
        raise RuntimeError('Cached normalization requires a process reload')
    attrs=[]
    if sensor.family!='inductive':
        return {'attributes':[],'version':current_version(),'requires_review':False,'conflicts':[]}
    for name,entry in decoded.get('fields',{}).items():
        if entry.get('application')!='filled':continue
        attrs.append({'name':FIELD_LABELS.get(name,name),'value':display(entry['value'],name),
                      'group':'Автоматическое дозаполнение','key':name,'typed_value':entry['value'],
                      'source_url':entry.get('source_url',decoded.get('source_url','')),
                      'evidence':entry['token']+' · '+entry['section'],
                      'rule_version':decoded.get('version',''),
                      'requires_review':bool(decoded.get('requires_review'))})
    return {'attributes':attrs,'version':current_version(),'requires_review':bool(decoded.get('requires_review')),
            'conflicts':[name for name,e in decoded.get('fields',{}).items() if e.get('application') in ('conflict','card_conflict')]}


def identity(record):
    from .catalog_schema import document
    from .models import utcnow
    attrs=record.get('_specifications') or {}
    fingerprint=record.get('details_hash') or document(json.dumps(attrs,ensure_ascii=False))[0]
    return dict(id=int(record['rule_id']),hash=fingerprint,source=record.get('source',''),
                manufacturer=record.get('manufacturer',''),article=record.get('article',''),
                title=record.get('title') or '',category=record['category'] if record.get('category') is not None else attrs.get('category',''),
                revision=REVISION,version=current_version(),now=utcnow())


def prepare(record):
    payload=characteristics(record)
    return {**identity(record),'payload':json.dumps(payload,ensure_ascii=False,separators=(',',':')),
            'count':len(payload['attributes'])}


SAVE = '''INSERT INTO product_enrichment(rule_id,details_hash,source,manufacturer,article,title,category,
 revision,version,payload_json,filled_count,checked_at)
 SELECT q.id,%(hash)s,%(source)s,%(manufacturer)s,%(article)s,%(title)s,%(category)s,
 %(revision)s,%(version)s,%(payload)s,%(count)s,%(now)s
 FROM rules q JOIN product_index i ON i.rule_id=q.id
 WHERE q.id=%(id)s AND i.details_hash=%(hash)s AND q.source=%(source)s
 AND q.manufacturer=%(manufacturer)s AND q.article=%(article)s AND i.title=%(title)s AND i.category=%(category)s
 ON CONFLICT(rule_id) DO UPDATE SET details_hash=excluded.details_hash,source=excluded.source,
 manufacturer=excluded.manufacturer,article=excluded.article,title=excluded.title,category=excluded.category,
 revision=excluded.revision,version=excluded.version,payload_json=excluded.payload_json,
 filled_count=excluded.filled_count,checked_at=excluded.checked_at
 WHERE product_enrichment.revision<=excluded.revision AND
 (product_enrichment.details_hash<>excluded.details_hash OR product_enrichment.version<>excluded.version
 OR product_enrichment.payload_json<>excluded.payload_json OR product_enrichment.source<>excluded.source
 OR product_enrichment.article<>excluded.article OR product_enrichment.manufacturer<>excluded.manufacturer
 OR product_enrichment.title<>excluded.title OR product_enrichment.category<>excluded.category)'''


class Enrichment:
    def __init__(self,repo):self.repo=repo

    def save(self,records):
        self.save_prepared([prepare(r) for r in records])

    def save_prepared(self,records):
        for offset in range(0,len(records),25):
            sql=[];params={}
            for n,data in enumerate(records[offset:offset+25]):
                if not data['hash']:continue
                statement=SAVE
                for k,v in data.items():
                    statement=statement.replace('%('+k+')s','%('+k+str(n)+')s');params[k+str(n)]=v
                sql.append(statement)
            if sql:self.repo.batch(sql,params)

    def attach(self,rows):
        """Current records only. Historic observations must not update the projection."""
        current=[r for r in rows if r.get('rule_id') is not None and 'details_json' not in r]
        if not current:return rows
        params={f'id{n}':r['rule_id'] for n,r in enumerate(current)}
        stored=self.repo.batch('SELECT * FROM product_enrichment WHERE rule_id IN ('+
                               ','.join('%('+k+')s' for k in params)+')',params)
        known={r['rule_id']:r for r in stored};pending=[]
        for row in current:
            data=identity(row);old=known.get(row['rule_id'])
            valid=old and all(old[k]==data[v] for k,v in [('details_hash','hash'),('source','source'),
                ('manufacturer','manufacturer'),('article','article'),('title','title'),('category','category'),('version','version')])
            if not valid:data=prepare(row)
            row['_enrichment']=json.loads(old['payload_json'] if valid else data['payload'])
            row['automatic_attributes_count']=len(row['_enrichment']['attributes'])
            if not valid:pending.append(data)
        if pending:self.save_prepared(pending)
        return rows

    def process_batch(self,limit=40):
        rows=self.repo.batch('''SELECT q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,i.title,i.category,
            i.details_hash,d.details_json FROM product_index i JOIN rules q ON q.id=i.rule_id
            JOIN product_documents d ON d.fingerprint=i.details_hash
            LEFT JOIN product_enrichment e ON e.rule_id=q.id
            WHERE e.rule_id IS NULL OR (e.revision<=%(revision)s AND (e.version<>%(version)s OR e.details_hash<>i.details_hash
            OR e.article<>q.article OR e.manufacturer<>q.manufacturer OR e.source<>q.source OR e.title<>i.title OR e.category<>i.category))
            ORDER BY q.id LIMIT %(limit)s''',{'version':current_version(),'revision':REVISION,'limit':max(1,min(200,int(limit)))})
        for row in rows:row['_specifications']=json.loads(row.pop('details_json'))
        self.save(rows)
        return len(rows)


class EnrichmentWorker:
    def __init__(self,repo):
        self.service=Enrichment(repo);self.stopping=threading.Event();self.version=VERSION
        # Hot reload must not leave a previous code revision writing in parallel.
        for t in threading.enumerate():
            if t.name=='price-characteristics':
                old=getattr(getattr(t,'_target',None),'__self__',None)
                if old is not None and hasattr(old,'stopping'):old.stopping.set()
        self.thread=threading.Thread(target=self._loop,name='price-characteristics',daemon=True);self.thread.start()

    def _loop(self):
        if self.stopping.wait(90):return
        while not self.stopping.is_set():
            try:delay=15 if self.service.process_batch() else 300
            except Exception as exc:
                logging.getLogger(__name__).warning('Automatic characteristics retry: %s',type(exc).__name__);delay=60
            self.stopping.wait(delay)

    def close(self):self.stopping.set();self.thread.join(timeout=2)
