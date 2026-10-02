"""Compare bounded single-statement reads with the current transaction batch."""
from pathlib import Path
import json
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import psycopg
from psycopg.rows import dict_row
from price_monitor.sensoren_local import settings_from_file
from price_monitor.db_settings import connection_settings
from price_monitor.db_connection import DeadlineConnection
from price_monitor.db_batches import batch

settings=settings_from_file(ROOT/'.streamlit'/'secrets.toml')
report={}
def single(sql):
    with DeadlineConnection.connect(**connection_settings(settings),autocommit=True,connect_timeout=10,
        cursor_factory=psycopg.ClientCursor,prepare_threshold=None,row_factory=dict_row) as conn:
        conn.io_timeout=15;conn.io_deadline=time.monotonic()+15
        return conn.execute(sql).fetchall()

queries=[('single_ping',lambda:single('SELECT 1 AS connected')),
 ('batch_ping',lambda:batch({**settings,'reuse_connections':False},'SELECT 1 AS connected',timeout=15)),
 ('single_activity',lambda:single("""SELECT pid,state,wait_event_type,wait_event,
  round(EXTRACT(epoch FROM clock_timestamp()-query_start)::numeric,1) age_seconds,
  round(EXTRACT(epoch FROM clock_timestamp()-xact_start)::numeric,1) transaction_seconds,
  cardinality(pg_blocking_pids(pid)) blockers,
  CASE WHEN position('worker_lease' in query)>0 THEN 'worker_lease'
       WHEN position('product_enrichment' in query)>0 THEN 'enrichment'
       WHEN position('own_product_prices' in query)>0 THEN 'own_prices'
       WHEN position('catalog_pages' in query)>0 THEN 'catalog_pages'
       WHEN position('current_details_json' in query)>0 THEN 'comparison_catalog'
       ELSE 'other' END operation
  FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
  AND backend_type='client backend' ORDER BY query_start LIMIT 25"""))]
for name,read in queries:
    start=time.monotonic()
    try:result={'rows':read()}
    except Exception as exc:
        message=getattr(getattr(exc,'diag',None),'message_primary','') or ''
        for value in settings.values():
            if isinstance(value,str) and len(value)>2:message=message.replace(value,'[redacted]')
        result={'error':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None),'message':message}
    result['seconds']=round(time.monotonic()-start,2);report[name]=result
    print(json.dumps({name:result},default=str),flush=True)
(ROOT/'analysis/search_load_recovery_2026_10_02/transport_probe.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
