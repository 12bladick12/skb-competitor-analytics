"""Bounded read-only probes to distinguish SQL work from result transfer."""
from pathlib import Path
import json,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from price_monitor.library import Library
from price_monitor.sensoren_local import settings_from_file
from price_monitor.db_batches import batch
class Captured(Exception):pass
class Capture:
    def batch(self,sql,params):self.sql,self.params=sql,params;raise Captured()
capture=Capture()
try:next(Library(capture).iter_search_identities())
except Captured:pass
settings={**settings_from_file(ROOT/'.streamlit/secrets.toml'),'reuse_connections':False}
report={}
for label,sql,limit in [('materialized_bytes','SELECT count(*) rows,sum(octet_length(row_to_json(t)::text)) bytes FROM ('+capture.sql+') t',5000),
                       ('rows_50',capture.sql,50),('rows_500',capture.sql,500)]:
    start=time.monotonic()
    try:
        rows=batch(settings,sql,{**capture.params,'limit':limit},timeout=20)
        result={'rows':len(rows),'bytes':len(json.dumps(rows,default=str).encode())}
        if label=='materialized_bytes':result['aggregate']=rows
    except Exception as exc:result={'error':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None)}
    result['seconds']=round(time.monotonic()-start,2);report[label]=result
    print(json.dumps({label:result},default=str),flush=True)
(ROOT/'analysis/search_load_recovery_2026_10_02/page_size_probe.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
