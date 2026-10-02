"""One-off, read-only PostgreSQL snapshot; never used during local app startup."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Only durable price/catalog data. Retired passports, transport payloads and
# cloud worker leases are deliberately outside this migration.
PRICE_TABLES = {
    'automatic_price_terms', 'catalog_integrity', 'catalog_pages', 'catalog_sources',
    'catalog_variant_sets', 'comparison_items', 'comparison_price_terms', 'jobs',
    'monthly_job_reuse', 'monthly_page_memory', 'observations', 'own_price_imports',
    'own_product_prices', 'product_aliases', 'product_documents', 'product_enrichment',
    'product_events', 'product_index', 'product_lifecycle', 'product_scope', 'rules',
    'runs', 'source_pauses',
}


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def sqlite_type(pg_type):
    if pg_type in {'smallint', 'integer', 'bigint', 'boolean'}:
        return 'INTEGER'
    if pg_type in {'real', 'double precision'}:
        return 'REAL'
    if pg_type == 'bytea':
        return 'BLOB'
    return 'TEXT'


def value_for_sqlite(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def export_snapshot(settings, destination, port=None):
    import psycopg
    from psycopg import sql
    from price_monitor.db_connection import DeadlineConnection
    from price_monitor.db_settings import connection_settings
    from price_monitor.storage import Store

    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Snapshot destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.partial')
    Store(staging).close()
    report = {'source': 'Supabase / price_monitor', 'snapshot_at': None, 'tables': {}}
    with closing(sqlite3.connect(staging)) as local:
        local.execute('PRAGMA journal_mode=DELETE')
        print('Connecting to the read-only price snapshot...', flush=True)
        connection = connection_settings(settings)
        if port is not None:
            connection['port'] = port
        with DeadlineConnection.connect(**connection, connect_timeout=10,
                             options='-c default_transaction_read_only=on -c statement_timeout=90000',
                             prepare_threshold=None, cursor_factory=psycopg.ClientCursor) as remote:
            remote.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            print('Read-only snapshot transaction started.', flush=True)
            report['snapshot_at'] = remote.execute('SELECT transaction_timestamp()').fetchone()[0].isoformat()
            schema = defaultdict(list)
            for table in sorted(PRICE_TABLES):
                cursor = remote.execute(sql.SQL('SELECT * FROM price_monitor.{} LIMIT 0').format(sql.Identifier(table)))
                kinds = {16: 'boolean', 20: 'bigint', 21: 'smallint', 23: 'integer',
                         700: 'real', 701: 'double precision', 17: 'bytea'}
                schema[table] = [(column.name, kinds.get(column.type_code, 'text')) for column in cursor.description]
                print('Schema: ' + table, flush=True)
            print('Price table metadata loaded.', flush=True)
            if not {'rules', 'runs', 'jobs', 'observations', 'product_documents'} <= schema.keys():
                raise ValueError('Required price tables are missing')
            local.execute('BEGIN')
            for index, (table, fields) in enumerate(schema.items()):
                existing = {r[1] for r in local.execute('PRAGMA table_info(' + quoted(table) + ')')}
                if not existing:
                    local.execute('CREATE TABLE ' + quoted(table) + ' (' + ','.join(
                        quoted(name) + ' ' + sqlite_type(kind) for name, kind in fields) + ')')
                else:
                    for name, kind in fields:
                        if name not in existing:
                            local.execute('ALTER TABLE ' + quoted(table) + ' ADD COLUMN ' +
                                          quoted(name) + ' ' + sqlite_type(kind))
                expected = remote.execute(sql.SQL('SELECT count(*) FROM price_monitor.{}').format(sql.Identifier(table))).fetchone()[0]
                insert = 'INSERT INTO ' + quoted(table) + ' (' + ','.join(quoted(f[0]) for f in fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')'
                count = 0
                with remote.cursor(name='local_export_' + str(index)) as cursor:
                    cursor.execute(sql.SQL('SELECT {} FROM price_monitor.{}').format(
                        sql.SQL(',').join(sql.Identifier(f[0]) for f in fields), sql.Identifier(table)))
                    while rows := cursor.fetchmany(1000):
                        local.executemany(insert, [tuple(value_for_sqlite(v) for v in row) for row in rows])
                        count += len(rows)
                if count != expected:
                    raise ValueError('Row count mismatch: ' + table)
                report['tables'][table] = count
                print(f'{table}: {count}', flush=True)
            if local.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Foreign key check failed')
            if local.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite integrity check failed')
            local.commit()
    staging.rename(destination)
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    report['integrity_check'] = 'ok'
    report['foreign_key_check'] = 'ok'
    destination.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def prepare_working_copy(snapshot, destination):
    """Preserve raw snapshot, but do not restart copied cloud jobs automatically."""
    snapshot, destination = Path(snapshot).resolve(), Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Local database already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.partial')
    with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(staging)) as local:
            source.backup(local)
            stamp = datetime.now(timezone.utc).isoformat()
            active = [r[0] for r in local.execute("SELECT id FROM runs WHERE state IN ('queued','running')")]
            for run in active:
                local.execute("UPDATE runs SET state='cancelled',cancel_requested=1,finished_at=?,note=note || ? WHERE id=?",
                              (stamp, ' | Остановлен в локальной копии при переносе из Supabase; сохранённые результаты оставлены.', run))
                local.execute("UPDATE catalog_sources SET state='cancelled',finished_at=? WHERE run_id=? AND state IN ('pending','running')", (stamp, run))
                local.execute("UPDATE catalog_pages SET state='cancelled' WHERE run_id=? AND state IN ('pending','processing')", (run,))
                local.execute("UPDATE jobs SET state='pending' WHERE run_id=? AND state='processing'", (run,))
            local.execute('DELETE FROM worker_lease')
            local.execute('DELETE FROM collector_health')
            local.execute("UPDATE external_sources SET enabled=0,owner='',heartbeat=0")
            local.commit()
    staging.rename(destination)
    return active


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--secrets', type=Path, default=ROOT / '.streamlit/secrets.toml')
    parser.add_argument('--snapshot', type=Path, default=ROOT / 'data/local/supabase_snapshot.sqlite3')
    parser.add_argument('--database', type=Path, default=ROOT / 'data/local/prices.sqlite3')
    parser.add_argument('--driver-path', type=Path)
    parser.add_argument('--port', type=int, help='Override pooler port for this one-off export')
    args = parser.parse_args()
    if args.driver_path:
        sys.path.insert(0, str(args.driver_path.resolve()))
    try:
        if args.database.exists():
            raise FileExistsError('Local database already exists')
        settings = tomllib.loads(args.secrets.read_text(encoding='utf-8'))['database']
        export_snapshot(settings, args.snapshot, args.port)
        active = prepare_working_copy(args.snapshot, args.database)
        print(json.dumps({'result': 'ok', 'paused_local_runs': active}), flush=True)
        return 0
    except Exception as exc:
        # Database errors can contain hostnames and credentials.
        print('Export failed: ' + type(exc).__name__ + '. Existing databases were not replaced.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
