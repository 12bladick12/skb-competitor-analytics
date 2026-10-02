"""Verify every transferred value against one retained PostgreSQL snapshot."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'data/local_migration_deps'), str(ROOT)]

from scripts.export_prices_compressed import table_digest
from scripts.export_prices_to_sqlite import PRICE_TABLES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--snapshot-at', required=True)
    args = parser.parse_args()
    import psycopg
    from psycopg import sql
    from price_monitor.db_connection import DeadlineConnection
    from price_monitor.db_settings import connection_settings
    settings = connection_settings(tomllib.loads((ROOT / '.streamlit/secrets.toml').read_text(encoding='utf-8'))['database'])
    folder = ROOT / 'data/local'

    def connect():
        c = DeadlineConnection.connect(**settings, connect_timeout=8, prepare_threshold=None,
            cursor_factory=psycopg.ClientCursor, autocommit=True,
            options='-c default_transaction_read_only=on -c statement_timeout=90000 -c idle_in_transaction_session_timeout=0')
        c.io_timeout = 100
        return c

    with connect() as keeper:
        cursor = keeper.execute(sql.SQL('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT {}; SELECT pg_export_snapshot()').format(sql.Literal(args.snapshot)))
        while not cursor.description:
            cursor.nextset()
        snapshot = cursor.fetchone()[0]
        (folder / 'verification_snapshot.json').write_text(json.dumps({'pid': os.getpid(), 'snapshot': snapshot, 'snapshot_at': args.snapshot_at}), encoding='utf-8')
        print('Verification snapshot retained.', flush=True)

        def read(statement, schema=False):
            for attempt in range(8):
                try:
                    with connect() as c:
                        q = c.execute(sql.SQL('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT {}; ').format(sql.Literal(snapshot)) + statement + sql.SQL('; COMMIT'))
                        result = None
                        while True:
                            if q.description:
                                result = [(v.name, v.type_code) for v in q.description] if schema else q.fetchall()
                            if not q.nextset():
                                break
                        return result
                except psycopg.OperationalError:
                    if attempt == 7:
                        raise
                    time.sleep(5)

        expected = {'snapshot_at': args.snapshot_at, 'tables': {}}
        for table in sorted(PRICE_TABLES):
            fields = read(sql.SQL('SELECT * FROM price_monitor.{} LIMIT 0').format(sql.Identifier(table)), schema=True)
            count, digest = read(sql.SQL("SELECT count(*),md5(coalesce(string_agg(h,'' ORDER BY h COLLATE \"C\"),'')) FROM (SELECT md5(to_jsonb(t)::text) h FROM price_monitor.{} t) rows").format(sql.Identifier(table)))[0]
            expected['tables'][table] = {'count': count, 'digest': digest, 'fields': fields}
            print(f'Source checksum: {table} ({count})', flush=True)
        (folder / 'expected_snapshot_hashes.json').write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding='utf-8')
        print('All source checksums saved. Waiting for finished local snapshot.', flush=True)
        source = folder / 'supabase_snapshot.sqlite3'
        deadline = time.monotonic() + 7200
        while not source.exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Export has not finished; source checksums are saved')
            time.sleep(2)
        results = {}
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as local:
            for table, entry in expected['tables'].items():
                matches = table_digest(local, table, entry['fields']) == entry['digest']
                results[table] = {'rows': entry['count'], 'content_matches': matches}
                print(f'Local content: {table} {matches}', flush=True)
        (folder / 'content_verification.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        if not all(v['content_matches'] for v in results.values()):
            raise ValueError('Content checksum mismatch')
        print('All table contents match the source snapshot.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Verification failed: ' + type(exc).__name__, file=sys.stderr, flush=True)
        raise SystemExit(1)
