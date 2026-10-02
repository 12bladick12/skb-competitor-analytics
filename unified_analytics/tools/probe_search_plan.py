"""Read planner estimates for the names query without executing its scan."""
from pathlib import Path
import json,re,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from price_monitor.library import Library
from price_monitor.sensoren_local import settings_from_file
from price_monitor.db_settings import connection_settings
from price_monitor.db_connection import DeadlineConnection
import psycopg
from psycopg.rows import dict_row
class Captured(Exception):pass
class Capture:
    def batch(self,sql,params):self.sql,self.params=sql,params;raise Captured()
capture=Capture()
try:next(Library(capture).iter_search_identities())
except Captured:pass
sql=re.sub(r'\b(rules|product_index|comparison_items|product_scope)\b',r'price_monitor.\1',capture.sql)
settings=settings_from_file(ROOT/'.streamlit/secrets.toml')
report={}
for label,query,params in [
    ('estimates',"SELECT relname,reltuples,relpages FROM pg_class WHERE oid IN ('price_monitor.rules'::regclass,'price_monitor.product_scope'::regclass,'price_monitor.product_index'::regclass)",{}),
    ('plan','EXPLAIN (FORMAT JSON) '+sql,capture.params)]:
    start=time.monotonic()
    try:
        with DeadlineConnection.connect(**connection_settings(settings),autocommit=True,connect_timeout=10,
             cursor_factory=psycopg.ClientCursor,prepare_threshold=None,row_factory=dict_row) as conn:
            conn.io_timeout=15;conn.io_deadline=time.monotonic()+15
            rows=conn.execute(query,params).fetchall()
        report[label]={'seconds':round(time.monotonic()-start,2),'rows':rows}
    except Exception as exc:report[label]={'error':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None)}
    print(json.dumps({label:report[label]},default=str),flush=True)
(ROOT/'analysis/search_load_recovery_2026_10_02/search_plan.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
