"""Read-only collection snapshot. Never prints connection settings or SQL text."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from price_monitor.db_batches import batch
from price_monitor.sensoren_local import settings_from_file


def main():
    settings = {**settings_from_file(ROOT / '.streamlit/secrets.toml'), 'reuse_connections': False}
    queries = {
        'runs': "SELECT id,state,cancel_requested,started_at,finished_at FROM runs ORDER BY id DESC LIMIT 3",
        'leases': "SELECT source,enabled,owner,protocol,round((EXTRACT(EPOCH FROM clock_timestamp())-heartbeat)::numeric) AS heartbeat_age_seconds FROM external_sources",
        'worker': "SELECT owner,round((EXTRACT(EPOCH FROM clock_timestamp())-heartbeat)::numeric) AS heartbeat_age_seconds FROM worker_lease",
        'health': "SELECT source,owner,run_id,phase,url,recoveries,detail,round((EXTRACT(EPOCH FROM clock_timestamp())-activity_at)::numeric) AS activity_age_seconds,round((EXTRACT(EPOCH FROM clock_timestamp())-completed_at)::numeric) AS completed_age_seconds FROM collector_health",
        'sources': "SELECT run_id,source,state,detail FROM catalog_sources WHERE run_id=(SELECT max(id) FROM runs) ORDER BY source",
        'queue': "SELECT source,state,count(*) AS pages,max(checked_at) AS last_checked FROM catalog_pages WHERE run_id=(SELECT max(id) FROM runs) GROUP BY source,state ORDER BY source,state",
        'processing': "SELECT source,kind,url FROM catalog_pages WHERE run_id=(SELECT max(id) FROM runs) AND state='processing' LIMIT 10",
        'positions': "SELECT q.source,count(*) AS saved_positions,max(o.checked_at) AS last_saved FROM observations o JOIN jobs j ON j.id=o.job_id JOIN rules q ON q.id=j.rule_id WHERE j.run_id=(SELECT max(id) FROM runs) GROUP BY q.source ORDER BY q.source",
        'activity': "SELECT state,wait_event_type,wait_event,count(*) AS connections FROM pg_stat_activity WHERE datname=current_database() GROUP BY state,wait_event_type,wait_event",
    }
    snapshot = {'observed_at_epoch': time.time()}
    for name, sql in queries.items():
        try:
            snapshot[name] = batch(settings, sql)
        except Exception as exc:
            snapshot[name] = {'error': type(exc).__name__}
        print(json.dumps({name: snapshot[name]}, ensure_ascii=False, default=str), flush=True)
    target = ROOT / 'data' / 'collection_diagnostics'
    target.mkdir(exist_ok=True)
    path = target / (time.strftime('%Y%m%d_%H%M%S') + '.json')
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


if __name__ == '__main__':
    main()
