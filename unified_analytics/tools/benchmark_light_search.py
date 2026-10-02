"""Read-only first-page and own-price timing; output counts, never price rows."""
from datetime import datetime,timezone
from itertools import islice
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from price_monitor.sensoren_local import settings_from_file
from price_monitor.catalog_storage import CatalogRepository
from price_monitor.library import Library
from price_monitor.own_prices import OwnPrices

repo=CatalogRepository(settings={**settings_from_file(ROOT/'.streamlit'/'secrets.toml'),'reuse_connections':False})
results={'checked_at':datetime.now(timezone.utc).isoformat(),'read_only':True}
for label,read in [('own_prices',lambda:OwnPrices(repo).current()),
                   ('search_first_1000',lambda:list(islice(Library(repo).iter_search_products(),1000)))]:
    start=time.monotonic()
    try:
        rows=read()
        results[label]={'rows':len(rows),'response_bytes':len(json.dumps(rows,default=str).encode()),'success':True}
    except Exception as exc:
        results[label]={'error':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None),'success':False}
    results[label]['seconds']=round(time.monotonic()-start,2)
    print(json.dumps({label:results[label]}),flush=True)
folder=ROOT/'analysis'/'search_load_recovery_2026_10_02'
folder.mkdir(exist_ok=True)
(folder/'benchmark.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
