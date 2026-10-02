"""Bounded read-only diagnostics; never logs SQL literals or connection secrets."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from price_monitor.sensoren_local import settings_from_file
from price_monitor.db_batches import batch

OUT = ROOT / 'analysis' / 'startup_fix_2026_10_02'
settings = {**settings_from_file(ROOT / '.streamlit' / 'secrets.toml'), 'reuse_connections': False}
results = {'checked_at': datetime.now(timezone.utc).isoformat(), 'read_only': True, 'probes': {}}

def probe(name, sql, params=None):
    start = time.monotonic()
    try:
        rows = batch(settings, ['SET TRANSACTION READ ONLY', "SET LOCAL statement_timeout='15s'", sql], params,
                     serialize=False, timeout=25)
        value = {'seconds': round(time.monotonic()-start, 3), 'rows': rows}
    except Exception as exc:
        message = getattr(getattr(exc, 'diag', None), 'message_primary', '') or ''
        for secret in settings.values():
            if isinstance(secret, str) and len(secret)>2: message=message.replace(secret, '[redacted]')
        value = {'seconds': round(time.monotonic()-start, 3), 'error': type(exc).__name__,
                 'sqlstate': getattr(exc, 'sqlstate', None), 'message': message}
    results['probes'][name] = value
    OUT.mkdir(exist_ok=True)
    (OUT / 'root_cause_diagnostics.json').write_text(json.dumps(results, indent=2, default=str), encoding='utf-8')
    # Explain output contains no values, but only summarize its plan separately.
    if not name.startswith('plan_'): print(json.dumps({name:value}, default=str), flush=True)
    return value.get('rows', [])

probe('server', """SELECT current_setting('server_version') version,
    current_setting('default_transaction_read_only') default_read_only,
    round(pg_database_size(current_database())/1048576.0,1) database_mib,
    to_regclass('extensions.pg_stat_statements')::text statements_extension,
    to_regclass('public.pg_stat_statements')::text statements_public""")
probe('activity', """SELECT pid,state,wait_event_type,wait_event,
    round(EXTRACT(epoch FROM clock_timestamp()-query_start)::numeric,1) age_seconds,
    cardinality(pg_blocking_pids(pid)) blockers,
    CASE WHEN position('worker_lease' in query)>0 THEN 'worker_lease'
         WHEN position('product_enrichment' in query)>0 THEN 'enrichment'
         WHEN position('catalog_pages' in query)>0 THEN 'catalog_pages'
         WHEN position('current_details_json' in query)>0 THEN 'comparison_catalog'
         WHEN position('pg_advisory_xact_lock' in query)>0 THEN 'serialized_batch'
         ELSE 'other' END operation
    FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
    AND backend_type='client backend' ORDER BY query_start LIMIT 30""")
probe('tables', """SELECT relname,n_live_tup,n_dead_tup,n_tup_ins,n_tup_upd,
    seq_scan,idx_scan,last_autovacuum,last_autoanalyze,
    round(pg_total_relation_size(relid)/1048576.0,2) size_mib
    FROM pg_stat_user_tables WHERE schemaname='price_monitor'
    ORDER BY pg_total_relation_size(relid) DESC LIMIT 12""")
ext = results['probes']['server'].get('rows', [{}])[0]
relation = ext.get('statements_extension') or ext.get('statements_public')
if relation in ('extensions.pg_stat_statements', 'public.pg_stat_statements', 'pg_stat_statements'):
    probe('statements', """SELECT queryid,calls,round(total_exec_time::numeric,1) total_ms,
        round(mean_exec_time::numeric,1) mean_ms,round(max_exec_time::numeric,1) max_ms,
        rows,shared_blks_read,shared_blks_hit,temp_blks_written,
        CASE WHEN position('current_details_json' in query)>0 THEN 'comparison_catalog'
             WHEN position('pg_advisory_xact_lock' in query)>0 THEN 'advisory_lock'
             WHEN position('worker_lease' in query)>0 THEN 'worker_lease'
             WHEN position('product_enrichment' in query)>0 THEN 'enrichment'
             WHEN position('catalog_pages' in query)>0 THEN 'catalog_pages'
             WHEN position('observations' in query)>0 THEN 'observations'
             ELSE 'other' END operation
        FROM """+relation+""" WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
        ORDER BY total_exec_time DESC LIMIT 15""")

class Captured(Exception):
    pass
class Capture:
    def batch(self, sql, params):
        self.sql, self.params = sql, params
        raise Captured()

def first_query(cls):
    repo = Capture()
    try: next(cls(repo).iter_comparison_products())
    except Captured: pass
    return repo.sql, repo.params

live = {'__name__':'price_monitor.diagnostic_live_library', '__package__':'price_monitor'}
exec(compile((OUT/'diagnostic_live_library.py').read_text(encoding='utf-8'), 'live_library', 'exec'), live)
from price_monitor.library import Library
for name, cls in [('published',live['Library']), ('split_candidate',Library)]:
    sql, params=first_query(cls)
    plans=probe('plan_'+name, 'EXPLAIN (FORMAT JSON) '+sql, params)
    if plans:
        plan=plans[0]['QUERY PLAN'][0]
        nodes=[]
        def walk(node):
            nodes.append({key:node[key] for key in ('Node Type','Relation Name','Index Name',
                'Plan Rows','Total Cost','Join Type','Subplan Name') if key in node})
            for child in node.get('Plans',[]):walk(child)
        walk(plan['Plan'])
        print(json.dumps({'plan_'+name:{'nodes':nodes}}, default=str), flush=True)
probe('ping_end', 'SELECT 1 AS connected')
