"""Add passport tables without rebuilding prices; checkpoint an optional catalog run."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from price_monitor.catalog_storage import CatalogRepository
from price_monitor.sensoren_local import settings_from_file
from price_monitor.passport_schema import SCHEMA
from price_monitor.sensoren_payload import SCHEMA as TRANSFER_SCHEMA
from price_monitor.postgres import schema_sql
from price_monitor.models import utcnow
from price_monitor.db_connection import DatabaseIOTimeout

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--settings',default='.streamlit/secrets.toml')
    parser.add_argument('--checkpoint-run',type=int);args=parser.parse_args()
    repo=CatalogRepository(settings={**settings_from_file(args.settings),'reuse_connections':False})
    folder=Path('data/passport_rollout');folder.mkdir(exist_ok=True)
    if args.checkpoint_run:
        rid=args.checkpoint_run
        checkpoint={'run':repo.batch('SELECT * FROM runs WHERE id=%(id)s',{'id':rid}),
                    'sources':repo.sources(rid),'progress':repo.progress(rid)}
        path=folder/f'run_{rid}_before.json'
        if not path.exists():path.write_text(json.dumps(checkpoint,ensure_ascii=False,indent=2),encoding='utf-8')
        repo.batch(["UPDATE runs SET state='cancelled',cancel_requested=1,finished_at=%(now)s WHERE id=%(id)s AND state IN ('running','queued')",
                    "UPDATE catalog_pages SET state='cancelled' WHERE run_id=%(id)s AND state IN ('pending','processing') AND EXISTS(SELECT 1 FROM runs WHERE id=%(id)s AND state='cancelled')",
                    "UPDATE catalog_sources SET state='cancelled',finished_at=%(now)s WHERE run_id=%(id)s AND state IN ('pending','running') AND EXISTS(SELECT 1 FROM runs WHERE id=%(id)s AND state='cancelled')"],{'id':rid,'now':utcnow()})
        print('Catalog checkpoint saved',rid,flush=True)
    if repo.batch("SELECT id FROM runs WHERE state IN ('running','queued')"):
        raise RuntimeError('Checkpoint the active run before migration')
    evidence="SELECT count(*) n,md5(string_agg(id::text||':'||status||':'||checked_at||':'||coalesce(price,'')||':'||details_json,'|' ORDER BY id)) checksum FROM observations"
    before=repo.batch(evidence)
    statements=[s for s in schema_sql(SCHEMA).split(';') if s.strip()]+TRANSFER_SCHEMA
    for index,sql in enumerate(statements):
        # These are exclusively idempotent CREATE/REPLACE statements.
        for attempt in range(3):
            try:repo.batch(sql);break
            except DatabaseIOTimeout:
                if attempt==2:raise
        print('Schema',index+1,'/',len(statements),flush=True)
    from price_monitor.product_state import ProductState
    for source in ('megak','teko','sensor','beskonta','sensoren'):
        ProductState(repo).bootstrap(source)
        print('Lifecycle backfilled',source,flush=True)
    after=repo.batch(evidence)
    result={'before':before,'after':after,'history_unchanged':before==after,'checked_at':utcnow()}
    (folder/'migration.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    if before!=after:raise RuntimeError('Price history changed during migration; inspect before resuming')
    print(json.dumps(result),flush=True)

if __name__=='__main__':main()
