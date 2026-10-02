"""Bounded, read-only transfer for connections that stall on large PG responses."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import closing
from datetime import datetime, timezone
import argparse
import base64
import json
import hashlib
from decimal import Decimal
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import threading
import traceback
import tomllib
import uuid
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'data/local_migration_deps'))

from scripts.export_prices_to_sqlite import PRICE_TABLES, prepare_working_copy, quoted, value_for_sqlite


def jsonb_text(value):
    """PostgreSQL jsonb's text form, used only to verify retained rows."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (len(k.encode('utf-8')), k.encode('utf-8')))
        return '{' + ', '.join(jsonb_text(k) + ': ' + jsonb_text(value[k]) for k in keys) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(map(jsonb_text, value)) + ']'
    if isinstance(value, Decimal):
        return format(value, 'f')
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def table_digest(local, table, fields):
    hashes = []
    columns = [name for name, _ in fields]
    for row in local.execute('SELECT ' + ','.join(map(quoted, columns)) + ' FROM ' + quoted(table)):
        record = dict(zip(columns, row))
        for name, oid in fields:
            value = record[name]
            if value is None:
                continue
            if oid == 16:
                record[name] = bool(value)
            elif oid in (20, 21, 23):
                record[name] = int(value)
            elif oid in (700, 701, 1700):
                record[name] = Decimal(str(value))
            elif oid in (114, 3802):
                record[name] = json.loads(value, parse_float=Decimal)
        hashes.append(hashlib.md5(jsonb_text(record).encode('utf-8')).hexdigest())
    return hashlib.md5(''.join(sorted(hashes)).encode('ascii')).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume-from', type=Path)
    parser.add_argument('--snapshot')
    parser.add_argument('--snapshot-at')
    parser.add_argument('--workers', type=int, choices=range(1, 17), default=4)
    parser.add_argument('--port', type=int, default=6543)
    parser.add_argument('--verify-resume', action='store_true', help='Revalidate saved tables against a new snapshot by content checksum')
    parser.add_argument('--repair-text', action='store_true', help='Repair legacy PGP literal Latin-1 decoding before content verification')
    parser.add_argument('--retain-pages', action='append', choices=sorted(PRICE_TABLES), default=[])
    args = parser.parse_args()
    if args.resume_from and not args.verify_resume and not (args.snapshot and args.snapshot_at):
        parser.error('Resume requires the still-live source snapshot and its original timestamp')
    if args.retain_pages and not args.snapshot:
        parser.error('Retaining partial pages requires the same live snapshot')
    if args.repair_text and not args.verify_resume:
        parser.error('Legacy text repair requires content verification')
    import pgpy
    import psycopg
    from cryptography.utils import CryptographyDeprecationWarning
    from psycopg import sql
    from price_monitor.db_connection import DeadlineConnection
    from price_monitor.db_settings import connection_settings
    from price_monitor.storage import Store

    warnings.filterwarnings('ignore', category=DeprecationWarning)
    warnings.filterwarnings('ignore', category=CryptographyDeprecationWarning)
    settings = connection_settings(tomllib.loads((ROOT / '.streamlit/secrets.toml').read_text(encoding='utf-8'))['database'])
    settings['port'] = args.port
    folder = ROOT / 'data/local'
    destination = folder / 'supabase_snapshot.sqlite3'
    working = folder / 'prices.sqlite3'
    if destination.exists() or working.exists():
        raise FileExistsError('Destination already exists')
    folder.mkdir(parents=True, exist_ok=True)
    staging = folder / ('compressed-' + uuid.uuid4().hex + '.partial')
    Store(staging).close()
    key = secrets.token_hex(24)
    report = {'source': 'Supabase / price_monitor', 'tables': {}, 'transfer': 'compressed read-only snapshot'}

    def connect():
        c = DeadlineConnection.connect(**settings, connect_timeout=10, prepare_threshold=None,
            cursor_factory=psycopg.ClientCursor, autocommit=True,
            application_name='price-local-migration-20261002',
            options='-c default_transaction_read_only=on -c statement_timeout=25000 -c idle_in_transaction_session_timeout=0')
        c.io_timeout = 10
        return c

    with connect() as keeper, closing(sqlite3.connect(staging)) as local:
        begin = sql.SQL('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; ')
        if args.snapshot:
            begin += sql.SQL('SET TRANSACTION SNAPSHOT {}; ').format(sql.Literal(args.snapshot))
        keeper_cursor = keeper.execute(begin + sql.SQL('SELECT pg_export_snapshot(),transaction_timestamp()'))
        while not keeper_cursor.description:
            keeper_cursor.nextset()
        snapshot, stamp = keeper_cursor.fetchone()
        report['snapshot_at'] = args.snapshot_at or stamp.isoformat()
        if args.resume_from:
            (folder / 'resume_ready.json').write_text(json.dumps({
                'pid': os.getpid(), 'imported_snapshot': args.snapshot,
                'exported_snapshot': snapshot, 'source': str(args.resume_from.resolve())}), encoding='utf-8')
            if not args.verify_resume or args.snapshot:
                signal = folder / 'resume_continue'
                signal.unlink(missing_ok=True)
                print('Same snapshot retained. Waiting for previous exporter to stop.', flush=True)
                while not signal.exists():
                    time.sleep(0.5)
            # Opening for writing recovers the old process's interrupted SQLite
            # transaction. Only its already committed, verified tables survive.
            with closing(sqlite3.connect(args.resume_from)) as previous:
                previous.execute('SELECT count(*) FROM sqlite_master').fetchone()
                if previous.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Resume source integrity check failed')
                previous.backup(local)
            report['resume_validation'] = 'content checksum' if args.verify_resume else 'same live snapshot'
        if args.repair_text:
            repaired = 0
            for table in PRICE_TABLES:
                fields = [r[1] for r in local.execute('PRAGMA table_info(' + quoted(table) + ')')]
                for row in local.execute('SELECT rowid,* FROM ' + quoted(table)).fetchall():
                    changes = {}
                    for name, value in zip(fields, row[1:]):
                        if isinstance(value, str):
                            try:
                                decoded = value.encode('latin-1').decode('utf-8')
                            except UnicodeError:
                                continue
                            if decoded != value:
                                changes[name] = decoded
                    if changes:
                        local.execute('UPDATE ' + quoted(table) + ' SET ' + ','.join(quoted(k) + '=?' for k in changes) + ' WHERE rowid=?', (*changes.values(), row[0]))
                        repaired += len(changes)
                local.commit()
            print(f'Legacy text decoding repaired in {repaired} cells; content verification follows.', flush=True)
        local.execute('PRAGMA journal_mode=DELETE')
        local.execute('CREATE TABLE IF NOT EXISTS _transfer_pages (table_name TEXT, page_offset INTEGER, page_size INTEGER, row_count INTEGER, PRIMARY KEY(table_name,page_offset))')
        local.commit()
        print('Consistent read-only snapshot opened.', flush=True)

        thread_state = threading.local()
        connections = []
        connections_lock = threading.Lock()

        def read(statement, params=None, schema=False):
            for attempt in range(12):
                c = getattr(thread_state, 'connection', None)
                try:
                    if c is None or c.closed:
                        c = connect()
                        thread_state.connection = c
                        thread_state.reads = 0
                        with connections_lock:
                            connections.append(c)
                    batch = sql.SQL('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT {}; ').format(sql.Literal(snapshot)) + statement + sql.SQL('; COMMIT')
                    with c.cursor() as cursor:
                        cursor.execute(batch, params)
                        result = None
                        while True:
                            if cursor.description:
                                result = [(v.name, v.type_code) for v in cursor.description] if schema else cursor.fetchall()
                            if not cursor.nextset():
                                break
                        thread_state.reads += 1
                        # The current network path stops responding after several
                        # exchanges. Rotate before that happens, bounding sockets.
                        if thread_state.reads >= 1:
                            c.close()
                            thread_state.connection = None
                        return result
                except (psycopg.OperationalError, psycopg.errors.QueryCanceled) as exc:
                    if c is not None:
                        c.close()
                    thread_state.connection = None
                    detail = str(exc)
                    for value in settings.values():
                        if isinstance(value, str) and len(value) > 2:
                            detail = detail.replace(value, '[redacted]')
                    print(f'Connection retry {attempt + 1}/12: {type(exc).__name__}: {detail[:240]}', flush=True)
                    traceback.clear_frames(exc.__traceback__)
                    if attempt == 11:
                        raise RuntimeError('Read retries exhausted; committed pages are retained') from None
                time.sleep(min(15, 2 + attempt))

        def page(table, columns, order, offset, limit, attempt=0):
            if table == 'product_documents' and limit > 6:
                return page(table, columns, order, offset, 6) + page(table, columns, order, offset + 6, limit - 6)
            statement = sql.SQL("""WITH packet AS MATERIALIZED (SELECT encode(extensions.pgp_sym_encrypt(
                coalesce(json_agg(t)::text,'[]'),%s,'compress-algo=1,compress-level=9,cipher-algo=aes256,s2k-mode=1,unicode-mode=1'),'base64')
                AS payload
                FROM (SELECT * FROM price_monitor.{table} WHERE ({keys}) IN
                    (SELECT {keys} FROM price_monitor.{table} ORDER BY {keys} OFFSET %s LIMIT %s)
                    ORDER BY {keys}) t)
                SELECT length(payload),CASE WHEN length(payload)<=10000 THEN payload ELSE NULL END FROM packet""").format(
                    table=sql.Identifier(table), keys=sql.SQL(',').join(sql.Identifier(k) for k in order))
            try:
                length, encoded = read(statement, (key, offset, limit))[0]
                if encoded is None and limit > 1:
                    half = limit // 2
                    return page(table, columns, order, offset, half) + page(table, columns, order, offset + half, limit - half)
                if encoded is None:
                    # An unusually large single document still travels losslessly
                    # in bounded, deterministic base64 slices of its original JSON.
                    parts = []
                    start = 1
                    while True:
                        raw = sql.SQL("""SELECT substring(encode(convert_to(row_to_json(t)::text,'UTF8'),'base64') FROM %s FOR 4000)
                            FROM (SELECT * FROM price_monitor.{table} WHERE ({keys}) IN
                                (SELECT {keys} FROM price_monitor.{table} ORDER BY {keys} OFFSET %s LIMIT 1)) t""").format(
                            table=sql.Identifier(table), keys=sql.SQL(',').join(sql.Identifier(k) for k in order))
                        chunk = read(raw, (start, offset))[0][0]
                        parts.append(chunk)
                        if len(chunk) < 4000:
                            break
                        start += 4000
                    record = json.loads(base64.b64decode(''.join(parts)))
                    return [tuple(value_for_sqlite(record.get(k)) for k in columns)]
                payload = pgpy.PGPMessage.from_blob(base64.b64decode(encoded)).decrypt(key).message
                records = json.loads(payload)
                return [tuple(value_for_sqlite(r.get(k)) for k in columns) for r in records]
            except (psycopg.OperationalError, psycopg.errors.QueryCanceled):
                if attempt < 2:
                    time.sleep(1)
                    return page(table, columns, order, offset, limit, attempt + 1)
                raise

        batch_sizes = {'product_documents': 12, 'rules': 45, 'product_enrichment': 60,
                       'product_index': 40, 'catalog_pages': 45, 'product_events': 100,
                       'product_aliases': 80, 'monthly_page_memory': 80}
        pool = ThreadPoolExecutor(max_workers=args.workers)
        try:
            for table in sorted(PRICE_TABLES):
                print('Preparing ' + table, flush=True)
                fields = read(sql.SQL('SELECT * FROM price_monitor.{} LIMIT 0').format(sql.Identifier(table)), schema=True)
                columns = [name for name, _ in fields]
                count = read(sql.SQL('SELECT count(*) FROM price_monitor.{}').format(sql.Identifier(table)))[0][0]
                existing = list(local.execute('PRAGMA table_info(' + quoted(table) + ')'))
                known = {r[1] for r in existing}
                for column in columns:
                    if column not in known:
                        local.execute('ALTER TABLE ' + quoted(table) + ' ADD COLUMN ' + quoted(column) + ' TEXT')
                present = local.execute('SELECT count(*) FROM ' + quoted(table)).fetchone()[0]
                retain_partial = table in args.retain_pages and present < count
                if args.verify_resume and present and not retain_partial:
                    matches = False
                    if present == count:
                        remote_digest = read(sql.SQL("SELECT md5(coalesce(string_agg(h,'' ORDER BY h COLLATE \"C\"),'')) FROM (SELECT md5(to_jsonb(t)::text) h FROM price_monitor.{} t) rows").format(sql.Identifier(table)))[0][0]
                        matches = table_digest(local, table, fields) == remote_digest
                    if not matches:
                        local.execute('DELETE FROM ' + quoted(table))
                        local.execute('DELETE FROM _transfer_pages WHERE table_name=?', (table,))
                        local.commit()
                        present = 0
                        print(table + ': content requires refresh from current snapshot', flush=True)
                if present == count:
                    report['tables'][table] = count
                    print(f'{table}: verified {count} (retained)', flush=True)
                    continue
                saved = dict(local.execute('SELECT page_offset,row_count FROM _transfer_pages WHERE table_name=?', (table,)))
                if present != sum(saved.values()):
                    raise ValueError('Resume table has untracked rows: ' + table)
                order = [r[1] for r in sorted(existing, key=lambda r: r[5]) if r[5]] or columns[:1]
                insert = 'INSERT INTO ' + quoted(table) + '(' + ','.join(map(quoted, columns)) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
                size = batch_sizes.get(table, 100)
                if saved and local.execute('SELECT count(*) FROM _transfer_pages WHERE table_name=? AND page_size<>?', (table, size)).fetchone()[0]:
                    raise ValueError('Resume page size changed: ' + table)
                offsets = iter(offset for offset in range(0, count, size) if offset not in saved)
                pending = {}
                def fill():
                    while len(pending) < args.workers * 2:
                        offset = next(offsets, None)
                        if offset is None:
                            break
                        pending[pool.submit(page, table, columns, order, offset, min(size, count-offset))] = offset
                fill()
                done = present
                print(f'{table}: copying {count} rows', flush=True)
                last = time.monotonic()
                while pending:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        offset = pending.pop(future)
                        rows = future.result()
                        if len(rows) != min(size, count-offset):
                            raise ValueError('Page count mismatch: ' + table)
                        with local:
                            local.executemany(insert, rows)
                            local.execute('INSERT INTO _transfer_pages VALUES (?,?,?,?)', (table, offset, size, len(rows)))
                        done += len(rows)
                        if time.monotonic() - last > 20:
                            print(f'{table}: {done}/{count}', flush=True)
                            last = time.monotonic()
                    fill()
                if done != count:
                    raise ValueError('Count mismatch: ' + table)
                local.commit()
                report['tables'][table] = done
                print(f'{table}: verified {done}', flush=True)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            for c in connections:
                c.close()
        if local.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Foreign key check failed')
        if local.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        local.execute('DROP TABLE _transfer_pages')
        local.commit()
    staging.rename(destination)
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    report['integrity_check'] = report['foreign_key_check'] = 'ok'
    destination.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    active = prepare_working_copy(destination, working)
    print(json.dumps({'result': 'ok', 'paused_local_runs': active}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import traceback
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        print('Export failed: ' + type(exc).__name__ + ' sqlstate=' + str(getattr(exc, 'sqlstate', None)) +
              ' at ' + Path(frame.filename).name + ':' + str(frame.lineno), file=sys.stderr, flush=True)
        raise SystemExit(1)
