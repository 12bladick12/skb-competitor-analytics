"""Reusable PostgreSQL connections for atomic catalog and external-agent work."""
import atexit
import threading
import time

_pools = {}
_guard = threading.Lock()


def check_connection(connection):
    # Use an ordinary read over the transaction pooler, not an empty query.
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()


def pool_for(settings):
    from .db_settings import connection_settings
    settings = connection_settings(settings)
    key = tuple(sorted(settings.items()))
    with _guard:
        pool = _pools.get(key)
        if pool is None:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            from .db_connection import DeadlineConnection
            pool = ConnectionPool(
                connection_class=DeadlineConnection,
                kwargs={**settings, 'connect_timeout':10, 'autocommit':True, 'prepare_threshold':None,
                        'cursor_factory':psycopg.ClientCursor, 'row_factory':dict_row},
                min_size=0, max_size=2, timeout=15, max_idle=60, max_lifetime=600,
                check=check_connection, name='price-catalog', open=True)
            _pools[key] = pool
            atexit.register(pool.close)
        return pool


def read_statements(statements):
    # Only fixed SELECT statements qualify. CTEs may write and remain locked.
    return all(sql.lstrip().upper().startswith('SELECT ') for sql in statements)


def connection_for(settings):
    settings = dict(settings)
    reuse = settings.pop('reuse_connections', True)
    if reuse:
        return pool_for(settings).connection()
    # Optional external-agent mode for networks that break persistent sessions.
    # One atomic SQL batch uses one connection; closing rolls back any failure.
    import psycopg
    from psycopg.rows import dict_row
    from .db_settings import connection_settings
    from .db_connection import DeadlineConnection
    return DeadlineConnection.connect(**connection_settings(settings), connect_timeout=10,
        autocommit=True, prepare_threshold=None, cursor_factory=psycopg.ClientCursor,
        row_factory=dict_row)


def batch(settings, statements, params=None, *, serialize=True, timeout=None):
    from .runtime import phase
    with phase('database'):
        return _batch(settings,statements,params,serialize=serialize,timeout=timeout)


def _batch(settings, statements, params=None, *, serialize=True, timeout=None):
    if isinstance(statements, str):
        statements = [statements]
    read_only = read_statements(statements)
    # Catalog reads may use the full minute requested for the search page.
    # Leave time to receive the result after the server finishes or cancels it.
    statement_seconds = 60 if read_only else 30
    if timeout is None:timeout = 75 if read_only else 45
    query = ('BEGIN READ ONLY; ' if read_only else 'BEGIN; ')
    query += (f"SET LOCAL search_path TO price_monitor; SET LOCAL statement_timeout='{statement_seconds}s'; "
              "SET LOCAL lock_timeout='10s'; SET LOCAL idle_in_transaction_session_timeout='40s'; ")
    if not read_only and serialize:
        # A separate statement gives following writes a fresh committed snapshot.
        query += 'SELECT pg_advisory_xact_lock(6743928101); '
    query += '; '.join(statements) + '; COMMIT;'
    result = []
    with connection_for(settings) as connection:
        previous_io_timeout = connection.io_timeout
        connection.io_timeout = timeout
        connection.io_deadline = time.monotonic()+timeout
        try:
            cursor = connection.execute(query, params or {})
            while True:
                if cursor.description:
                    rows = cursor.fetchall()
                    if cursor.description[0].name != 'pg_advisory_xact_lock':
                        result = rows
                if not cursor.nextset():
                    break
        finally:
            connection.io_deadline = None
            connection.io_timeout = previous_io_timeout
    return result

