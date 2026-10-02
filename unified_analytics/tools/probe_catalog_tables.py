"""Small read-only table probes with a server-enforced ten-second ceiling."""
from pathlib import Path
import json,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from price_monitor.sensoren_local import settings_from_file
from price_monitor.db_batches import batch
settings={**settings_from_file(ROOT/'.streamlit/secrets.toml'),'reuse_connections':False}
queries={
 'rules':'SELECT id,source,article FROM rules ORDER BY id LIMIT 100',
 'index':'SELECT rule_id,title FROM product_index ORDER BY rule_id LIMIT 100',
 'prices':'SELECT article,source_model FROM own_product_prices LIMIT 100',
 'join':'SELECT q.id,i.title FROM rules q LEFT JOIN product_index i ON i.rule_id=q.id ORDER BY q.id LIMIT 100',
 'vacuum':'SELECT relname,n_live_tup,n_dead_tup,last_autovacuum,last_autoanalyze FROM pg_stat_user_tables WHERE schemaname=\'price_monitor\' AND relname IN (\'rules\',\'product_index\',\'jobs\',\'catalog_pages\',\'observations\')'}
report={}
for name,sql in queries.items():
    start=time.monotonic()
    try:
        rows=batch(settings,['SET TRANSACTION READ ONLY',"SET LOCAL statement_timeout='10s'",sql],serialize=False,timeout=15)
        result={'rows':len(rows),'bytes':len(json.dumps(rows,default=str).encode())}
        if name=='vacuum':result['statistics']=rows
    except Exception as exc:result={'error':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None)}
    result['seconds']=round(time.monotonic()-start,2);report[name]=result
    print(json.dumps({name:result},default=str),flush=True)
(ROOT/'analysis/search_load_recovery_2026_10_02/table_probe.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
