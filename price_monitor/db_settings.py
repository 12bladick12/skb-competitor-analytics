"""Connection routing for short transactions on the shared Supabase pooler."""


def connection_settings(settings):
    values = dict(settings)
    host = str(values.get('host', '')).lower().rstrip('.')
    # The same Supavisor host/credentials expose transaction mode on 6543.
    # Our SQL uses SET LOCAL and transaction-scoped locks only; all callers
    # disable prepared statements. Other PostgreSQL endpoints are untouched.
    if host.endswith('.pooler.supabase.com') and int(values.get('port',5432)) == 5432:
        values['port'] = 6543
    return values
