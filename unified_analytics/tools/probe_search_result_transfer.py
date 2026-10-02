"""Inspect execution versus delivery for only 50 public catalog identities."""
from pathlib import Path
import json,re,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from price_monitor.library import SEARCH_IDENTITY_SELECT
from price_monitor.sensoren_local import settings_from_file
from price_monitor.db_settings import connection_settings
from price_monitor.db_connection import DeadlineConnection
from price_monitor.db_batches import batch
import psycopg
from psycopg.rows import dict_row
settings=settings_from_file(ROOT/'.streamlit/secrets.toml')
params={'after':0,'limit':50}
sql=re.sub(r'\b(rules|product_index|comparison_items|product_scope)\b',r'price_monitor.\1',SEARCH_IDENTITY_SELECT)
def single(query):
    with DeadlineConnection.connect(**connection_settings(settings),autocommit=True,connect_timeout=10,
         cursor_factory=psycopg.ClientCursor,prepare_threshold=None,row_factory=dict_row) as conn:
        conn.io_timeout=20;conn.io_deadline=time.monotonic()+20
        return conn.execute(query,params).fetchall()
report={}
for label,read in [('single_rows',lambda:single(sql)),
    ('execution_plan',lambda:single('EXPLAIN (ANALYZE,BUFFERS,TIMING FALSE,FORMAT JSON) '+sql)),
    ('json_batch',lambda:batch({**settings,'reuse_connections':False},
       'SELECT jsonb_agg(to_jsonb(t)) payload FROM ('+SEARCH_IDENTITY_SELECT+') t',params,timeout=20))]:
    start=time.monotonic()
    try:
        rows=read();result={'rows':len(rows),'bytes':len(json.dumps(rows,default=str).encode())}
        if label=='execution_plan':result['plan']=rows
        if label=='json_batch':result['decoded_rows']=len(rows[0]['payload'] or [])
    except Exception as exc:result={'error':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None)}
    result['seconds']=round(time.monotonic()-start,2);report[label]=result
    print(json.dumps({label:result},default=str),flush=True)
(ROOT/'analysis/search_load_recovery_2026_10_02/result_transfer_probe.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
