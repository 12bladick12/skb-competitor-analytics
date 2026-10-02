"""Add catalog tables/columns without changing historical runs or prices."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from price_monitor.catalog_storage import CatalogRepository
from price_monitor.catalog_schema import SCHEMA
from price_monitor.postgres import schema_sql
from price_monitor.sensoren_local import settings_from_file


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--secrets',default='.streamlit/secrets.toml')
    args=parser.parse_args();repository=CatalogRepository(settings=settings_from_file(Path(args.secrets)))
    statements=[statement for statement in schema_sql(SCHEMA).split(';') if statement.strip()]
    statements.extend([
        "ALTER TABLE observations ADD COLUMN IF NOT EXISTS details_json TEXT NOT NULL DEFAULT '{}'",
        'ALTER TABLE external_sources ADD COLUMN IF NOT EXISTS protocol INTEGER NOT NULL DEFAULT 1',
        "SELECT (SELECT count(*) FROM runs) runs,(SELECT count(*) FROM observations) observations,(SELECT count(*) FROM runs WHERE state IN ('queued','running')) active_runs"
    ])
    print(repository.batch(statements))


if __name__=='__main__':main()
