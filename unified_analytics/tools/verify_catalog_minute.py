"""Read one deployed catalog page using the proposed 60-second read timeout."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from price_monitor.sensoren_local import settings_from_file
from price_monitor.db_batches import batch

OUT=ROOT/'analysis'/'startup_fix_2026_10_02'
class Captured(Exception):pass
class Capture:
    def batch(self,sql,params):
        self.sql,self.params=sql,params
        raise Captured()

live={'__name__':'price_monitor.diagnostic_live_library','__package__':'price_monitor'}
exec(compile((OUT/'diagnostic_live_library.py').read_text(encoding='utf-8'),'live_library','exec'),live)
repo=Capture()
try:next(live['Library'](repo).iter_comparison_products())
except Captured:pass
settings={**settings_from_file(ROOT/'.streamlit'/'secrets.toml'),'reuse_connections':False}
report={'checked_at':datetime.now(timezone.utc).isoformat(),'server_timeout_seconds':60,
        'client_deadline_seconds':75,'read_only':True,'requested_rows':repo.params['limit']}
start=time.monotonic()
try:
    rows=batch(settings,repo.sql,repo.params)
    report.update(success=True,rows=len(rows))
except Exception as exc:
    message=getattr(getattr(exc,'diag',None),'message_primary','') or ''
    for value in settings.values():
        if isinstance(value,str) and len(value)>2:message=message.replace(value,'[redacted]')
    report.update(success=False,error=type(exc).__name__,sqlstate=getattr(exc,'sqlstate',None),message=message)
report['elapsed_seconds']=round(time.monotonic()-start,2)
(OUT/'minute_timeout_probe.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report),flush=True)
