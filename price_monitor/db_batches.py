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
                min_size=1, max_size=4, timeout=15, max_idle=60, max_lifetime=600,
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


def batch(settings, statements, params=None):
    from .runtime import phase
    with phase('database'):
        return _batch(settings,statements,params)


def _batch(settings, statements, params=None):
    if isinstance(statements, str):
        statements = [statements]
    read_only = read_statements(statements)
    query = ('BEGIN READ ONLY; ' if read_only else 'BEGIN; ')
    query += ("SET LOCAL search_path TO price_monitor; SET LOCAL statement_timeout='30s'; "
              "SET LOCAL lock_timeout='10s'; SET LOCAL idle_in_transaction_session_timeout='40s'; ")
    if not read_only:
        # A separate statement gives following writes a fresh committed snapshot.
        query += 'SELECT pg_advisory_xact_lock(6743928101); '
    query += '; '.join(statements) + '; COMMIT;'
    result = []
    with connection_for(settings) as connection:
        connection.io_deadline = time.monotonic()+45
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
    return result

