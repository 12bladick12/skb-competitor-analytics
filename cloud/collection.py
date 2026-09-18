"""Run the existing verified collector in an isolated, disposable SQLite workspace."""
import hashlib
import json
from pathlib import Path

from src.config_loader import load_competitors
from src.evidence_store import EvidenceStore
from src.models import AppSettings
from src.storage import Storage
from src.verified_monitor import VerifiedMonitor
from web.facts import Facts, month


def settings_for(root):
    return AppSettings(root_dir=root,data_dir=root/'data',raw_dir=root/'raw',processed_dir=root/'processed',
        snapshots_dir=root/'snapshots',reports_dir=root/'reports',logs_dir=root/'logs',database=root/'facts.db',
        user_agent='SKB-Induction-CompetitorMonitor/1.0',proxy_mode='direct',translation_enabled=False,
        telegram_use_telethon=False,request_delay_seconds=1.5,max_archive_pages=30,telegram_max_pages=100)


def collect(library, period, root, read, progress, baselines=(), monitor_factory=VerifiedMonitor):
    settings=settings_for(root)
    competitors=load_competitors(Path(__file__).with_name('competitors.yaml'))
    snapshots=settings.snapshots_dir
    snapshots.mkdir(parents=True)
    storage=Storage(settings.database)
    monitor=None
    objects={}
    def materialize(asset_id):
        path=snapshots/(asset_id+'.html')
        if not path.exists():
            content=read(asset_id)
            if hashlib.sha256(content).hexdigest()!=asset_id:
                raise ValueError('Повреждён сохранённый первоисточник.')
            path.write_bytes(content)
        return str(path.resolve())
    def insert(table, row):
        columns={r['name'] for r in storage.conn.execute('PRAGMA table_info('+table+')')}
        selected={k:v for k,v in row.items() if k in columns}
        names=list(selected)
        storage.conn.execute('INSERT OR REPLACE INTO '+table+'('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',
                             [selected[k] for k in names])
    def asset(raw):
        path=Path(raw).resolve()
        if not path.is_relative_to(snapshots.resolve()):
            raise ValueError('Снимок находится вне рабочего каталога.')
        content=path.read_bytes()
        digest=hashlib.sha256(content).hexdigest()
        objects[digest]=content
        return digest
    try:
        storage.init_schema()
        EvidenceStore(storage)
        for c in competitors:
            storage.upsert_competitor(c)
        for event in library['event'].values():
            value=dict(event)
            value['evidence_json']=json.dumps({**{k:v for k,v in event['provenance'].items() if v},
                'snapshots':[materialize(i) for i in event['evidence_ids']]})
            insert('verified_events',value)
        for run in library['run'].values():
            insert('collection_runs',run)
            for check in run['checks']:
                insert('source_checks',check)
        for baseline in baselines:
            value=dict(baseline)
            value['evidence_path']=materialize(value.pop('evidence_id'))
            insert('product_baselines',value)
        storage.conn.commit()
        monitor=monitor_factory(storage,settings,competitors)
        result=monitor.run(month(period)[1],progress=progress)
        facts=Facts(settings,competitors)
        records=[]
        def add(kind,key,payload):
            records.append({'kind':kind,'key':str(key),'payload':payload})
        all_events=[dict(r) for r in storage.conn.execute('SELECT * FROM verified_events ORDER BY id')]
        for raw in all_events:
            value=facts.public_event(raw)
            value.pop('evidence_url',None)
            evidence=json.loads(raw['evidence_json'])
            value['provenance']={k:evidence.get(k,'') for k in ('official_url','source_url','article_url','date_evidence')}
            value['evidence_ids']=[asset(p) for p in evidence.get('snapshots',[])]
            add('event',value['id'],value)
        for row in storage.conn.execute('SELECT * FROM product_baselines'):
            value=dict(row)
            value['evidence_id']=asset(value.pop('evidence_path'))
            add('collector_baseline',hashlib.sha256((value['competitor_code']+value['url']).encode()).hexdigest(),value)
        for row in storage.conn.execute('SELECT * FROM event_versions'):
            value=dict(row)
            payload=json.loads(value.pop('payload_json'))
            evidence=payload.get('evidence',{})
            evidence['snapshot_ids']=[asset(p) for p in evidence.pop('snapshots',[])]
            value['payload']=payload
            add('event_history',f"cloud-{value['event_id']}-{value['content_hash']}",value)
        for row in storage.conn.execute('SELECT * FROM collection_runs WHERE id=?',(result['run_id'],)):
            value=dict(row)
            value['checks']=[dict(c) for c in storage.conn.execute('SELECT * FROM source_checks WHERE run_id=?',(row['id'],))]
            add('run',value['id'],value)
        periods=set(library['period']) | {period}
        for key in sorted(periods):
            events,checks,links=facts.projection(key)
            add('period',key,{'key':key,'event_ids':[e['id'] for e in events],'checks':checks,'links':links})
        return records,objects,result
    finally:
        if monitor:
            monitor.close()
        storage.close()
