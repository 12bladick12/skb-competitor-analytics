"""Read-only deployment preflight; output excludes credentials and file contents."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from price_monitor.sensoren_local import settings_from_file
from price_monitor.catalog_storage import CatalogRepository

parser=argparse.ArgumentParser()
parser.add_argument('--settings',default='.streamlit/secrets.toml')
args=parser.parse_args()
repo=CatalogRepository(settings=settings_from_file(args.settings))
for label,sql in [('runs',"SELECT id,state FROM runs WHERE state IN ('queued','running')"),
                  ('catalog','SELECT source,manufacturer,count(*) n FROM rules GROUP BY source,manufacturer'),
                  ('workers','SELECT owner FROM worker_lease'),
                  ('external','SELECT source,owner,enabled FROM external_sources')]:
    print(label,json.dumps(repo.batch(sql),ensure_ascii=True),flush=True)
