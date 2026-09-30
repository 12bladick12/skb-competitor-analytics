"""Calendar-month collection memory. Reuse never creates a price observation."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import utcnow
from .scope import VISIBLE
from .sources import SOURCES

REVISION = 1
COLLECTED = "('priced','on_request','no_price')"

SCHEMA = """
CREATE TABLE IF NOT EXISTS monthly_page_memory (
 source TEXT NOT NULL, url TEXT NOT NULL, checked_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(source,url)
);
CREATE TABLE IF NOT EXISTS monthly_job_reuse (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
 observation_id BIGINT NOT NULL REFERENCES observations(id),
 reused_at TEXT NOT NULL, period TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_monthly ON observations(checked_at,status,job_id);
CREATE INDEX IF NOT EXISTS rules_product_url ON rules(source,product_url);
"""


def month_window(now=None):
    value = datetime.fromisoformat((now or utcnow()).replace('Z', '+00:00')).astimezone(timezone.utc)
    start = value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year+1, month=1) if start.month == 12 else start.replace(month=start.month+1)
    return {'month': start.strftime('%Y-%m'), 'month_start': start.isoformat(timespec='seconds'),
            'month_end': end.isoformat(timespec='seconds'), 'memory_revision': REVISION}


def page_key(source, url):
    """Normalize known aliases/tracking only; preserve meaningful URL parameters."""
    parts = urlsplit(url)
    if parts.hostname not in SOURCES[source].hosts:
        return url
    pairs = [(k,v) for k,v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('ysclid','gclid','yclid')]
    return urlunsplit(('https', SOURCES[source].host, parts.path or '/', urlencode(sorted(pairs)), ''))


def valid_observation(alias='o'):
    return (f"{alias}.status IN {COLLECTED} AND {alias}.http_status=200 "
            f"AND {alias}.details_json<>'{{}}' AND {alias}.checked_at>=%(month_start)s "
            f"AND {alias}.checked_at<%(month_end)s")


def page_memory_statement(source, url, checked_at, prefix, guard):
    params = {prefix+'source':source, prefix+'url':page_key(source,url), prefix+'checked':checked_at,
              prefix+'revision':REVISION}
    sql = f'''INSERT INTO monthly_page_memory(source,url,checked_at,revision)
        SELECT %({prefix}source)s,%({prefix}url)s,%({prefix}checked)s,%({prefix}revision)s WHERE {guard}
        ON CONFLICT(source,url) DO UPDATE SET checked_at=excluded.checked_at,revision=excluded.revision
        WHERE excluded.checked_at>=monthly_page_memory.checked_at OR excluded.revision<>monthly_page_memory.revision'''
    return sql,params


class MonthlyMemory:
    def __init__(self, repository):
        self.repo = repository

    def require_workers(self, sources):
        """Do not let a still-running old cloud process ignore monthly memory."""
        import time
        rows=self.repo.batch("SELECT owner,heartbeat FROM worker_lease WHERE id=1")
        if rows and rows[0]['heartbeat']>time.time()-120 and not rows[0]['owner'].startswith('router2-monthly1-sites5-'):
            raise ValueError('Сборщик ещё работает по прежним правилам каталога. В Streamlit откройте Manage app → ⋮ → Reboot app, затем повторите запуск.')
        if 'sensoren' in sources:
            rows=self.repo.batch("SELECT owner,heartbeat FROM external_sources WHERE source='sensoren' AND enabled=1")
            if rows and rows[0]['heartbeat']>time.time()-120 and not rows[0]['owner'].startswith('sensoren-monthly1-doc1-'):
                raise ValueError('Обновите и перезапустите внешний сборщик Sensoren: работающая версия ещё не поддерживает состояния товаров.')

    def backfill_pages(self, source):
        """Use existing successful observations; BESKONTA requires a complete page."""
        params = {**month_window(), 'source':source}
        # A manual BESKONTA job can collect just one execution out of hundreds.
        # Only its successfully completed catalog page proves full coverage.
        sql = f'''INSERT INTO monthly_page_memory(source,url,checked_at,revision)
            SELECT p.source,p.url,max(p.checked_at),%(memory_revision)s FROM catalog_pages p
            WHERE p.source=%(source)s AND p.kind='product' AND p.state='done' AND p.http_status=200
              AND p.checked_at>=%(month_start)s AND p.checked_at<%(month_end)s
              AND EXISTS(SELECT 1 FROM jobs j JOIN rules q ON q.id=j.rule_id
                JOIN observations o ON o.job_id=j.id WHERE j.run_id=p.run_id AND q.source=p.source
                AND (q.product_url=p.url OR o.url=p.url) AND {valid_observation()} AND {VISIBLE})
            GROUP BY p.source,p.url
            ON CONFLICT(source,url) DO UPDATE SET checked_at=excluded.checked_at,revision=excluded.revision
            WHERE excluded.checked_at>monthly_page_memory.checked_at'''
        statements = [sql]
        if source != 'beskonta':
            statements.append(f'''INSERT INTO monthly_page_memory(source,url,checked_at,revision)
                SELECT q.source,q.product_url,max(o.checked_at),%(memory_revision)s FROM rules q
                JOIN jobs j ON j.rule_id=q.id JOIN observations o ON o.job_id=j.id
                WHERE q.source=%(source)s AND q.product_url<>'' AND {valid_observation()} AND {VISIBLE}
                GROUP BY q.source,q.product_url
                ON CONFLICT(source,url) DO UPDATE SET checked_at=excluded.checked_at,revision=excluded.revision
                WHERE excluded.checked_at>monthly_page_memory.checked_at''')
        if self.repo.settings and not self.repo.settings.get('reuse_connections',True):
            for statement in statements:self.repo.batch(statement,params)
        else:self.repo.batch(statements,params)

    def recent_rule_keys(self, rules):
        found=set()
        for offset in range(0,len(rules),100):
            keys=list(dict.fromkeys(r.key for r in rules[offset:offset+100]))
            params={**month_window(), **{f'key{i}':key for i,key in enumerate(keys)}}
            if not keys:continue
            rows=self.repo.batch(f'''SELECT DISTINCT q.rule_key FROM rules q
                JOIN jobs j ON j.rule_id=q.id JOIN observations o ON o.job_id=j.id
                WHERE q.rule_key IN ({','.join(f'%(key{i})s' for i in range(len(keys)))})
                AND {valid_observation()} AND {VISIBLE}''',params)
            found.update(r['rule_key'] for r in rows)
        return found

    def reuse_job(self, job_id, owner, external=False):
        params={**month_window(), 'job':job_id, 'owner':owner,'now':utcnow()}
        import time
        params['lease_after']=time.time()-120
        if external:
            authority="""EXISTS(SELECT 1 FROM external_sources e WHERE e.source=q.source AND e.enabled=1
                AND e.owner=%(owner)s AND e.heartbeat>%(lease_after)s)"""
        else:
            authority="""EXISTS(SELECT 1 FROM worker_lease w WHERE w.owner=%(owner)s AND w.heartbeat>%(lease_after)s)
                AND NOT EXISTS(SELECT 1 FROM external_sources e WHERE e.source=q.source AND e.enabled=1)"""
        eligible=f"""j.id=%(job)s AND j.state='processing' AND r.state='running' AND r.cancel_requested=0
            AND {authority} AND NOT EXISTS(SELECT 1 FROM observations actual WHERE actual.job_id=j.id)"""
        rows=self.repo.batch([
            f'''INSERT INTO monthly_job_reuse(job_id,observation_id,reused_at,period)
                SELECT j.id,(SELECT o.id FROM observations o JOIN jobs past ON past.id=o.job_id
                    WHERE past.rule_id=j.rule_id AND {valid_observation()}
                    ORDER BY o.checked_at DESC,o.id DESC LIMIT 1),%(now)s,%(month)s
                FROM jobs j JOIN rules q ON q.id=j.rule_id JOIN runs r ON r.id=j.run_id
                WHERE {eligible} AND {VISIBLE} AND EXISTS(SELECT 1 FROM observations o JOIN jobs past ON past.id=o.job_id
                    WHERE past.rule_id=j.rule_id AND {valid_observation()})
                ON CONFLICT(job_id) DO NOTHING''',
            f'''UPDATE jobs SET state='done' WHERE id IN (SELECT j.id FROM jobs j
                JOIN rules q ON q.id=j.rule_id JOIN runs r ON r.id=j.run_id
                WHERE {eligible} AND EXISTS(SELECT 1 FROM monthly_job_reuse m
                    WHERE m.job_id=j.id AND m.period=%(month)s)) RETURNING id'''
        ],params)
        return bool(rows)


RESULT_FROM = '''FROM jobs j JOIN rules q ON q.id=j.rule_id
    LEFT JOIN observations actual ON actual.job_id=j.id
    LEFT JOIN monthly_job_reuse reuse ON reuse.job_id=j.id
    LEFT JOIN observations o ON o.id=COALESCE(actual.id,reuse.observation_id)'''
RESULT_STATUS = "CASE WHEN actual.id IS NULL AND reuse.observation_id IS NOT NULL THEN 'already_collected' ELSE COALESCE(o.status,j.state) END"
RESULT_DETAIL = "CASE WHEN actual.id IS NULL AND reuse.observation_id IS NOT NULL THEN 'Уже собрано в месяце '||reuse.period||'; дата проверки сохранена' ELSE o.detail END"
