"""Read-only observation of a rollout; never starts or rewrites queued work."""
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from price_monitor.catalog_storage import CatalogRepository
from price_monitor.sensoren_local import settings_from_file

repo=CatalogRepository(settings={**settings_from_file(ROOT/'.streamlit/secrets.toml'),'reuse_connections':False})
out=ROOT/'data'/'reliability_audit'
out.mkdir(exist_ok=True)
snapshots=[]
baseline=None
previous_owner='sensoren-monthly1-doc1-watchdog1-bd509281-86ce-47c7-9725-6896c0d97d76'
for attempt in range(18):
    try:
        owners=repo.batch("""SELECT 'cloud' source,owner,heartbeat FROM worker_lease
            UNION ALL SELECT source,owner,heartbeat FROM external_sources WHERE source='sensoren'""")
        rows={r['source']:r for r in repo.progress(11) if r['source'] in ('sensoren','teko')}
        summary={source:{key:row.get(key) for key in ('state','positions','visited','products_left','last_checked','work_phase','recoveries','detail')} for source,row in rows.items()}
        new_cloud=any(r['source']=='cloud' and r['owner'].startswith('router2-monthly1-sites5-isolated1-') for r in owners)
        new_sensoren=any(r['source']=='sensoren' and r['owner']!=previous_owner and time.time()-r['heartbeat']<60 for r in owners)
        snapshot={'observed_at':time.time(),'new_cloud':new_cloud,'new_sensoren':new_sensoren,'summary':summary}
        snapshots.append(snapshot)
        print(json.dumps(snapshot,ensure_ascii=False),flush=True)
        if new_cloud and new_sensoren:
            if baseline is None:baseline=summary
            elif summary['sensoren']['positions']>baseline['sensoren']['positions'] and summary['teko']['visited']>baseline['teko']['visited']:
                report={'passed':True,'baseline':baseline,'latest':summary,'snapshots':snapshots}
                (out/'live_recovery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print('Rollout verified: both collectors are advancing',flush=True)
                break
    except Exception as exc:
        print('Observation retry: '+type(exc).__name__,flush=True)
    time.sleep(20)
else:
    (out/'live_recovery.json').write_text(json.dumps({'passed':False,'snapshots':snapshots},ensure_ascii=False,indent=2),encoding='utf-8')
    raise SystemExit('Live progress not confirmed within observation window')
