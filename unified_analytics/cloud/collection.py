"""Run the existing verified collector in an isolated, disposable SQLite workspace."""
import hashlib
import json
import re
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


def collect(library, period, root, read, progress, baselines=(), monitor_factory=VerifiedMonitor,
            *, intelligence_config=None, intelligence_only=False, intelligence_factory=None, case_imports=()):
    settings=settings_for(root)
    competitors=load_competitors(Path(__file__).with_name('competitors.yaml'))
    snapshots=settings.snapshots_dir
    snapshots.mkdir(parents=True)
    storage=Storage(settings.database)
    monitor=None
    objects={}
    def materialize(asset_id):
        if not isinstance(asset_id,str) or not re.fullmatch('[a-f0-9]{64}',asset_id):
            raise ValueError('Некорректный идентификатор снимка.')
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
    def paths(value):
        # Catalogues keep the complete set of listing pages, not one filename.
        if isinstance(value,str):
            value=json.loads(value) if value.lstrip().startswith('[') else [value]
        if not isinstance(value,list) or not value or any(not isinstance(p,str) or not p for p in value):
            raise ValueError('Не сохранены подтверждающие страницы каталога.')
        return value
    def portable_check(check, restore=False):
        value=dict(check)
        if value.get('cursor') and value['kind']!='telegram':
            cursor=json.loads(value['cursor'])
            context={}
            for url,(card,proof) in cursor.get('article_context',{}).items():
                if restore:
                    # Legacy cursors may contain paths from another machine/run.
                    # Re-fetch that listing instead of trusting an old local path.
                    if not isinstance(proof,dict) or not proof.get('snapshot_id'):
                        continue
                    proof=materialize(proof['snapshot_id'])
                else:
                    if not proof:
                        continue
                    proof={'snapshot_id':asset(proof)}
                context[url]=[card,proof]
            cursor['article_context']=context
            value['cursor']=json.dumps(cursor,ensure_ascii=False)
        return value
    try:
        storage.init_schema()
        EvidenceStore(storage)
        for c in competitors:
            storage.upsert_competitor(c)
        for event in library['event'].values():
            value=dict(event)
            value['evidence_json']=json.dumps({**event.get('evidence',{}),**{k:v for k,v in event['provenance'].items() if v},
                'snapshots':[materialize(i) for i in event['evidence_ids']]})
            insert('verified_events',value)
        for run in library['run'].values():
            insert('collection_runs',run)
            for check in run['checks']:
                insert('source_checks',portable_check(check,restore=True))
        for baseline in baselines:
            value=dict(baseline)
            ids=value.pop('evidence_ids',None) or [value.pop('evidence_id')]
            value['evidence_path']=json.dumps([materialize(i) for i in ids])
            insert('product_baselines',value)
        storage.conn.commit()
        resolved_period=month(period)[1]
        evidence_store=EvidenceStore(storage,initialize=False)
        if intelligence_only:
            result={'run_id':evidence_store.start(resolved_period),'status':'running'}
        else:
            monitor=monitor_factory(storage,settings,competitors)
            result=monitor.run(resolved_period,progress=progress)
        intelligence=None
        if intelligence_config is not None or intelligence_only:
            from src.intelligence import collect_intelligence
            factory=intelligence_factory or collect_intelligence
            seeds=json.loads(Path(__file__).with_name('intelligence_sources.json').read_text(encoding='utf-8'))['seeds']
            intelligence=factory(library,resolved_period,snapshots,progress,
                                 intelligence_config or {},seeds=seeds,case_imports=case_imports)
            objects.update(intelligence['objects'])
            for record in intelligence['records']:
                value=record['payload']
                if record['kind']=='intel_mention' and value.get('access_state')=='success' and value['status']!='confirmed':
                    storage.conn.execute("UPDATE verified_events SET status='needs_review',reason='publisher_recheck' WHERE competitor_code=? AND kind='mentions' AND url=?",
                                         (value['competitor_code'],value['url']))
            for event in intelligence['events']:
                evidence_store.record(event)
            for check in intelligence['checks']:
                evidence_store.check(result['run_id'],check['competitor_code'],check['kind'],check['url'],
                                     check['status'],check['reason'],items=check['items'])
            result['status']=evidence_store.finish(result['run_id'])
        facts=Facts(settings,competitors)
        records=list(intelligence['records']) if intelligence else []
        def add(kind,key,payload):
            records.append({'kind':kind,'key':str(key),'payload':payload})
        all_events=[dict(r) for r in storage.conn.execute('SELECT * FROM verified_events ORDER BY id')]
        for raw in all_events:
            value=facts.public_event(raw)
            if raw['kind'] in ('mentions','litigation'):
                value['description']=raw['summary'] or value['description']
            value.pop('evidence_url',None)
            evidence=json.loads(raw['evidence_json'])
            value['evidence']={k:v for k,v in evidence.items() if k!='snapshots'}
            value['provenance']={k:evidence.get(k,'') for k in ('official_url','source_url','article_url','date_evidence')}
            value['evidence_ids']=[asset(p) for p in evidence.get('snapshots',[])]
            add('event',value['id'],value)
        for row in storage.conn.execute('SELECT * FROM product_baselines'):
            value=dict(row)
            value['evidence_ids']=[asset(p) for p in paths(value.pop('evidence_path'))]
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
            if intelligence:
                value['intelligence']={k:intelligence[k] for k in ('status','model_status','model_calls')}
            value['checks']=[portable_check(c) for c in storage.conn.execute('SELECT * FROM source_checks WHERE run_id=?',(row['id'],))]
            add('run',value['id'],value)
        periods=set(library['period']) | {period}
        for key in sorted(periods):
            events,checks,links=facts.projection(key)
            add('period',key,{'key':key,'event_ids':[e['id'] for e in events],'checks':[portable_check(c) for c in checks],'links':links})
        return records,objects,result
    finally:
        if monitor:
            monitor.close()
        storage.close()
