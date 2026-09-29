from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import os
from pathlib import Path
import sqlite3
import time

from .models import Observation, Rule, SUCCESS_STATUSES, utcnow
from .catalog_schema import SCHEMA as CATALOG_SCHEMA

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
 id INTEGER PRIMARY KEY, rule_key TEXT NOT NULL UNIQUE,
 source TEXT NOT NULL, manufacturer TEXT NOT NULL, article TEXT NOT NULL,
 product_url TEXT NOT NULL, url_template TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
 finished_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs((1)) WHERE state IN ('queued','running');
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), rule_id INTEGER NOT NULL REFERENCES rules(id),
 state TEXT NOT NULL DEFAULT 'pending', UNIQUE(run_id,rule_id)
);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
 status TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, price TEXT, currency TEXT,
 availability TEXT NOT NULL, price_text TEXT NOT NULL, availability_text TEXT NOT NULL,
 detail TEXT NOT NULL, checked_at TEXT NOT NULL, http_status INTEGER, response_hash TEXT NOT NULL,
 details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id,state);
CREATE INDEX IF NOT EXISTS jobs_rule ON jobs(rule_id);
CREATE TABLE IF NOT EXISTS source_pauses (
 run_id INTEGER NOT NULL REFERENCES runs(id), source TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(run_id,source)
);
CREATE TABLE IF NOT EXISTS worker_lease (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, heartbeat REAL NOT NULL);
CREATE TABLE IF NOT EXISTS external_sources (
 source TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
 owner TEXT NOT NULL DEFAULT '', heartbeat REAL NOT NULL DEFAULT 0,
 protocol INTEGER NOT NULL DEFAULT 1
);
PRAGMA user_version=1;
"""
SCHEMA += CATALOG_SCHEMA


class Store:
    def __init__(self, path=None, postgres=None):
        self.pg = None
        if postgres is not None:
            from .postgres import Postgres
            self.pg = Postgres(dict(postgres), SCHEMA)
            self.path = None
            return
        self.path = Path(path or os.getenv("PRICE_MONITOR_DB", "data/prices.sqlite3")).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0,1):
                raise RuntimeError(f"Версия базы {version} не поддерживается")
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            if 'details_json' not in {r['name'] for r in c.execute('PRAGMA table_info(observations)')}:
                c.execute("ALTER TABLE observations ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
            if 'protocol' not in {r['name'] for r in c.execute('PRAGMA table_info(external_sources)')}:
                c.execute('ALTER TABLE external_sources ADD COLUMN protocol INTEGER NOT NULL DEFAULT 1')

    @contextmanager
    def connect(self):
        if self.pg is not None:
            with self.pg.connect() as c:
                yield c
            return
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.create_function('lower',1,lambda value: str(value).casefold() if value is not None else None,deterministic=True)
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        try:
            with c:
                yield c
        finally:
            c.close()

    def write_lock(self, c):
        if self.pg is not None:
            self.pg.lock(c)
        else:
            c.execute("BEGIN IMMEDIATE")

    def close(self):
        if self.pg is not None:
            self.pg.close()

    @property
    def catalog(self):
        from .catalog_storage import CatalogRepository
        return CatalogRepository(settings=self.pg.settings if self.pg else None,path=self.path)

    def enqueue_catalog(self, selections):
        from .sources import SOURCES
        import json
        valid={s:[b for b in brands if b in SOURCES[s].brands] for s,brands in selections.items() if s in SOURCES}
        valid={s:list(dict.fromkeys(brands)) for s,brands in valid.items() if brands}
        if not valid:raise ValueError('Выберите хотя бы одного производителя')
        if self.pg:
            from .monthly import MonthlyMemory
            MonthlyMemory(self.catalog).require_workers(valid)
        with self.connect() as c:
            self.write_lock(c)
            lease=c.execute('SELECT owner,heartbeat FROM worker_lease WHERE id=1').fetchone()
            if lease and lease['heartbeat']>time.time()-120 and not lease['owner'].startswith('router2-'):
                raise ValueError('Обновите процесс сборщика: в Streamlit откройте Manage app → ⋮ → Reboot app. После перезапуска полный каталог станет доступен.')
            if c.execute("SELECT 1 FROM runs WHERE state IN ('queued','running')").fetchone():
                raise ValueError('Уже есть активный запуск. Дождитесь завершения или остановите его')
            insert="INSERT INTO runs(state,created_at,note) VALUES('queued',?,'Полный обход выбранных каталогов')"
            if self.pg:
                run_id=c.execute(insert+' RETURNING id',(utcnow(),)).fetchone()[0]
            else:run_id=c.execute(insert,(utcnow(),)).lastrowid
            for source,brands in valid.items():
                c.execute('INSERT INTO catalog_sources(run_id,source,brands_json) VALUES(?,?,?)',(run_id,source,json.dumps(brands,ensure_ascii=False)))
            return run_id

    def enqueue(self, rules: list[Rule]) -> int:
        if not rules or len({r.key for r in rules}) != len(rules):
            raise ValueError("Задания пусты или содержат дубли")
        if self.pg:
            from .monthly import MonthlyMemory
            MonthlyMemory(self.catalog).require_workers({r.source for r in rules})
        with self.connect() as c:
            self.write_lock(c)
            if c.execute("SELECT 1 FROM runs WHERE state IN ('queued','running')").fetchone():
                raise ValueError("Уже есть активный запуск. Дождитесь завершения или остановите его")
            insert = "INSERT INTO runs(state,created_at) VALUES('queued',?)"
            if self.pg is not None:
                run_id = c.execute(insert + " RETURNING id", (utcnow(),)).fetchone()[0]
            else:
                run_id = c.execute(insert, (utcnow(),)).lastrowid
            # Three round-trips per 100 rules, instead of three per rule.
            # Batches also stay below conservative SQLite placeholder limits.
            created_at = utcnow()
            for offset in range(0, len(rules), 100):
                batch = rules[offset:offset+100]
                params = [value for r in batch for value in (r.key,r.source,r.manufacturer,r.article,r.product_url,r.url_template,created_at)]
                c.execute("INSERT INTO rules(rule_key,source,manufacturer,article,product_url,url_template,created_at) VALUES " + ",".join("(?,?,?,?,?,?,?)" for _ in batch) + " ON CONFLICT(rule_key) DO NOTHING", params)
                rows = c.execute("SELECT id,rule_key FROM rules WHERE rule_key IN (" + ",".join("?" for _ in batch) + ")", [r.key for r in batch])
                ids = {row["rule_key"]:row["id"] for row in rows}
                c.execute("INSERT INTO jobs(run_id,rule_id) VALUES " + ",".join("(?,?)" for _ in batch), [value for r in batch for value in (run_id,ids[r.key])])
            return run_id

    def runs(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT r.*,count(j.id) total,sum(CASE WHEN j.state='done' THEN 1 ELSE 0 END) finished FROM runs r LEFT JOIN jobs j ON j.run_id=r.id GROUP BY r.id ORDER BY r.id DESC LIMIT 200")]

    def saved_rules(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT source,manufacturer,article,product_url,url_template FROM rules ORDER BY id")]

    def rule_options(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT id,source,manufacturer,article,product_url,url_template FROM rules ORDER BY source,article")]

    def results(self, run_id=None, rule_id=None):
        from .monthly import RESULT_FROM, RESULT_STATUS, RESULT_DETAIL
        query = f"""SELECT j.id job_id,j.run_id,q.id rule_id,q.source,q.manufacturer,q.article,
            {RESULT_STATUS} status,o.price,o.currency,COALESCE(o.availability,'unknown') availability,
            COALESCE(o.url,q.product_url) url,o.title,o.price_text,o.availability_text,{RESULT_DETAIL} detail,
            o.checked_at,o.http_status,o.response_hash,o.details_json,
            reuse.observation_id reused_observation_id,o.status original_status {RESULT_FROM}"""
        conditions, values = [], []
        if run_id is not None:
            conditions.append("j.run_id=?"); values.append(run_id)
        if rule_id is not None:
            conditions.append("j.rule_id=?"); values.append(rule_id)
        query += (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query += " ORDER BY j.id"
        with self.connect() as c:
            return [dict(r) for r in c.execute(query, values)]

    def cancel(self, run_id):
        with self.connect() as c:
            self.write_lock(c)
            if not c.execute("SELECT 1 FROM runs WHERE id=? AND state IN ('queued','running')",(run_id,)).fetchone():
                return
            now=utcnow()
            c.execute("UPDATE runs SET cancel_requested=1,state='cancelled',finished_at=? WHERE id=?",(now,run_id))
            c.execute("""INSERT INTO observations(job_id,status,url,title,availability,price_text,availability_text,detail,checked_at,response_hash)
                SELECT j.id,'cancelled',q.product_url,'','unknown','','','Остановлено пользователем',?,''
                FROM jobs j JOIN rules q ON q.id=j.rule_id WHERE j.run_id=? AND j.state!='done'
                ON CONFLICT(job_id) DO NOTHING""",(now,run_id))
            c.execute("UPDATE jobs SET state='done' WHERE run_id=?",(run_id,))
            c.execute("UPDATE catalog_pages SET state='cancelled' WHERE run_id=? AND state IN ('pending','processing')",(run_id,))
            c.execute("UPDATE catalog_sources SET state='cancelled',finished_at=? WHERE run_id=? AND state IN ('pending','running')",(now,run_id))

    def cancelled(self, run_id):
        with self.connect() as c:
            r = c.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
            return not r or bool(r[0])

    def acquire(self, owner, ttl=120):
        now = time.time()
        with self.connect() as c:
            self.write_lock(c)
            row = c.execute("SELECT * FROM worker_lease WHERE id=1").fetchone()
            if row and row["owner"] != owner and row["heartbeat"] > now-ttl:
                return False
            c.execute("INSERT INTO worker_lease VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,heartbeat=excluded.heartbeat", (owner,now))
            # Only lease acquisition may recover interrupted requests.
            c.execute("""UPDATE jobs SET state='pending' WHERE state='processing'
                AND id NOT IN (SELECT job_id FROM observations)
                AND rule_id NOT IN (SELECT q.id FROM rules q JOIN external_sources e
                    ON e.source=q.source AND e.enabled=1)""")
            return True

    def heartbeat(self, owner):
        with self.connect() as c:
            return c.execute("UPDATE worker_lease SET heartbeat=? WHERE owner=?", (time.time(),owner)).rowcount == 1

    def lease(self):
        with self.connect() as c:
            r = c.execute("SELECT heartbeat FROM worker_lease WHERE id=1").fetchone()
            return r[0] if r else None

    def external_sources(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT source,enabled,heartbeat,protocol FROM external_sources ORDER BY source")]

    def release(self, owner):
        with self.connect() as c:
            c.execute("DELETE FROM worker_lease WHERE owner=?", (owner,))

    def claim_run(self, owner):
        with self.connect() as c:
            self.write_lock(c)
            if not c.execute("SELECT 1 FROM worker_lease WHERE owner=? AND heartbeat>?", (owner,time.time()-120)).fetchone():
                return None
            row = c.execute("SELECT id FROM runs WHERE state IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            c.execute("UPDATE runs SET state='running',started_at=COALESCE(started_at,?) WHERE id=?", (utcnow(),row[0]))
            return row[0]

    def pending(self, run_id):
        with self.connect() as c:
            rows = c.execute("""SELECT j.id job_id,q.source,q.manufacturer,q.article,q.product_url,q.url_template
                FROM jobs j JOIN rules q ON q.id=j.rule_id
                WHERE j.run_id=? AND j.state!='done'
                AND NOT EXISTS(SELECT 1 FROM external_sources e WHERE e.source=q.source AND e.enabled=1)
                ORDER BY j.id""", (run_id,))
            return [(r["job_id"], Rule(**{k:r[k] for k in ("source","manufacturer","article","product_url","url_template")})) for r in rows]

    def pause_source(self, run_id, source, reason):
        with self.connect() as c:
            c.execute("INSERT INTO source_pauses VALUES(?,?,?) ON CONFLICT(run_id,source) DO UPDATE SET reason=excluded.reason", (run_id,source,reason))

    def pause_reason(self, run_id, source):
        with self.connect() as c:
            row = c.execute("SELECT reason FROM source_pauses WHERE run_id=? AND source=?", (run_id,source)).fetchone()
            return row[0] if row else ""

    def processing(self, job_id, owner):
        with self.connect() as c:
            return c.execute("""UPDATE jobs SET state='processing' WHERE id=? AND state='pending'
                AND EXISTS(SELECT 1 FROM worker_lease WHERE owner=? AND heartbeat>?)
                AND rule_id NOT IN (SELECT q.id FROM rules q JOIN external_sources e
                    ON e.source=q.source AND e.enabled=1)""", (job_id,owner,time.time()-120)).rowcount == 1

    def reuse_monthly(self,job_id,owner):
        from .monthly import MonthlyMemory
        return MonthlyMemory(self.catalog).reuse_job(job_id,owner)

    def record(self, job_id, observation: Observation, owner):
        values = asdict(observation)
        with self.connect() as c:
            self.write_lock(c)
            if not c.execute("SELECT 1 FROM worker_lease WHERE owner=? AND heartbeat>?", (owner,time.time()-120)).fetchone():
                raise RuntimeError("Потеряна блокировка сборщика")
            if c.execute("SELECT 1 FROM observations WHERE job_id=?",(job_id,)).fetchone():
                return
            from .catalog_schema import index_product
            values['details_json'] = index_product(c,job_id,observation) or '{}'
            columns = list(values)
            c.execute(f"INSERT INTO observations(job_id,{','.join(columns)}) VALUES({','.join('?' for _ in range(len(columns)+1))}) ON CONFLICT(job_id) DO NOTHING", [job_id]+[values[k] for k in columns])
            c.execute("UPDATE jobs SET state='done' WHERE id=?", (job_id,))

    def finish(self, run_id, note=""):
        with self.connect() as c:
            self.write_lock(c)
            if c.execute("SELECT 1 FROM catalog_sources WHERE run_id=? AND state IN ('pending','running')",(run_id,)).fetchone():
                return
            if c.execute("SELECT 1 FROM jobs WHERE run_id=? AND state!='done'", (run_id,)).fetchone():
                return
            statuses = [r[0] for r in c.execute("SELECT o.status FROM observations o JOIN jobs j ON j.id=o.job_id WHERE j.run_id=?", (run_id,))]
            cancel = c.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            catalog_error=c.execute("SELECT 1 FROM catalog_sources WHERE run_id=? AND state NOT IN ('completed','cancelled')",(run_id,)).fetchone()
            state = "cancelled" if cancel else ("completed_with_errors" if catalog_error or any(s not in SUCCESS_STATUSES for s in statuses) else "completed")
            c.execute("UPDATE runs SET state=?,finished_at=COALESCE(finished_at,?),note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?", (state,utcnow(),note,note,run_id))

    def backup(self, destination):
        if self.pg is not None:
            raise ValueError("Для PostgreSQL используйте резервное копирование Supabase или pg_dump")
        target = Path(destination).resolve()
        if target == self.path:
            raise ValueError("Резервная копия должна иметь другой путь")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            dest = sqlite3.connect(target)
            try:
                source.backup(dest)
            finally:
                dest.close()
