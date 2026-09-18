"""Durable sequential queue. A lease token fences every final database write."""
import hashlib
import json
from uuid import uuid4

from .access import authorize
from .drafts import DraftStore, DraftService
from .drive_store import StorageError
from .library import canonical
from .readiness import section


DDL = """DO $jobs$ BEGIN
 PERFORM pg_advisory_xact_lock(847263916);
 CREATE TABLE IF NOT EXISTS skb_analytics.jobs (
  id text PRIMARY KEY, import_id text NOT NULL REFERENCES skb_analytics.imports(id),
  kind text NOT NULL CHECK(kind IN ('export','collect')), period text NOT NULL,
  request_key text NOT NULL, payload jsonb NOT NULL, created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz,
  status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','success','partial','error','interrupted')),
  stage text NOT NULL DEFAULT 'Ожидание', source text NOT NULL DEFAULT '', completed integer NOT NULL DEFAULT 0,
  token text, lease_until timestamptz, result_id text, error text NOT NULL DEFAULT '',
  UNIQUE(import_id,kind,request_key));
 CREATE UNIQUE INDEX IF NOT EXISTS single_worker ON skb_analytics.jobs((1)) WHERE status='running';
 CREATE UNIQUE INDEX IF NOT EXISTS single_collection ON skb_analytics.jobs(import_id,period)
  WHERE kind='collect' AND status IN ('queued','running');
 CREATE TABLE IF NOT EXISTS skb_analytics.job_uploads (
  job_id text REFERENCES skb_analytics.jobs(id), digest text NOT NULL, drive_id text NOT NULL,
  PRIMARY KEY(job_id,digest));
END $jobs$;"""

CLAIM = """CREATE OR REPLACE FUNCTION skb_analytics.claim_job(worker_token text)
RETURNS SETOF skb_analytics.jobs LANGUAGE plpgsql AS $claim$
BEGIN
 PERFORM pg_advisory_xact_lock(847263917);
 UPDATE skb_analytics.jobs SET status='interrupted',finished_at=now(),
  stage='Рабочий процесс остановлен. Повторите запуск явно.',token=NULL
  WHERE status='running' AND lease_until < now();
 IF EXISTS(SELECT 1 FROM skb_analytics.jobs WHERE status='running') THEN RETURN; END IF;
 RETURN QUERY UPDATE skb_analytics.jobs j SET status='running',token=worker_token,
  lease_until=now()+interval '3 minutes',started_at=now(),stage='Подготовка'
  WHERE j.id=(SELECT q.id FROM skb_analytics.jobs q WHERE q.status='queued'
              ORDER BY q.created_at,q.id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING j.*;
END $claim$;"""


class LeaseLost(StorageError):
    pass


class JobStore(DraftStore):
    def initialize(self):
        # Explicit deployment only, never migrate from a page load.
        with self.repository.write_connect(**self.repository.params) as conn:
            conn.execute(DDL)
            # HTTPS migration adapter accepts DO blocks.
            from psycopg import sql
            conn.execute(sql.SQL('DO {}').format(sql.Literal('BEGIN EXECUTE ' + sql.Literal(CLAIM).as_string(conn) + '; END;')))

    def enqueue(self, import_id, kind, period, payload, email, request_key):
        rows = self.query('''INSERT INTO skb_analytics.jobs(id,import_id,kind,period,payload,created_by,request_key)
          SELECT $1,$2,$3,$4,$5::jsonb,$6,$7 WHERE EXISTS(
            SELECT 1 FROM skb_analytics.state WHERE key='active' AND import_id=$2)
          ON CONFLICT DO NOTHING RETURNING *''',
          [uuid4().hex, import_id, kind, period, canonical(payload), email, request_key], write=True)
        if not rows:
            rows = self.query('''SELECT * FROM skb_analytics.jobs WHERE import_id=$1 AND kind=$2
               AND (request_key=$3 OR (kind='collect' AND period=$4 AND status IN ('queued','running')))
               ORDER BY created_at DESC LIMIT 1''', [import_id, kind, request_key, period])
        if not rows:
            raise StorageError('Набор данных изменился. Обновите страницу.')
        return rows[0]

    def list(self, import_id):
        return self.query('''SELECT id,kind,period,status,stage,source,completed,created_at,started_at,
           finished_at,result_id,error FROM skb_analytics.jobs WHERE import_id=$1
           ORDER BY (status IN ('queued','running')) DESC,created_at DESC LIMIT 40''',[import_id])

    def claim(self, token):
        rows = self.query('SELECT * FROM skb_analytics.claim_job($1)', [token], write=True)
        return rows[0] if rows else None

    def pulse(self, job, stage=None, source=None, completed=None):
        rows = self.query('''UPDATE skb_analytics.jobs SET lease_until=now()+interval '3 minutes',
           stage=COALESCE($3,stage),source=COALESCE($4,source),completed=COALESCE($5::integer,completed)
           WHERE id=$1 AND token=$2 AND status='running' AND lease_until>now() RETURNING id''',
           [job['id'],job['token'],stage,source,completed], write=True)
        if not rows:
            raise LeaseLost('Рабочий процесс утратил право завершить задание. Результат не опубликован.')

    def fail(self, job, message, status='error'):
        self.query('''UPDATE skb_analytics.jobs SET status=$3,error=$4,stage='Задание не завершено',finished_at=now(),token=NULL
           WHERE id=$1 AND token=$2 AND status='running' RETURNING id''',
           [job['id'],job['token'],status,message], write=True)

    def upload_id(self, job, digest, allocate):
        found = self.query('SELECT drive_id FROM skb_analytics.job_uploads WHERE job_id=$1 AND digest=$2',[job['id'],digest])
        if found:
            return found[0]['drive_id']
        rows = self.query('''INSERT INTO skb_analytics.job_uploads(job_id,digest,drive_id)
           VALUES($1,$2,$3) ON CONFLICT(job_id,digest) DO UPDATE SET digest=excluded.digest RETURNING drive_id''',
           [job['id'],digest,allocate()], write=True)
        return rows[0]['drive_id']

    def publish(self, job, records, assets, status='success', result_id=None):
        # One transaction locks the lease, validates facts for exports, and commits
        # files, records and success together. A fenced worker can only leave orphan packs.
        from psycopg import sql
        payload = {'records':records,'assets':assets}
        body = sql.SQL('''DECLARE j skb_analytics.jobs; data jsonb := {data}::jsonb; expected jsonb;
        BEGIN
          SELECT * INTO j FROM skb_analytics.jobs WHERE id={id} AND token={token}
             AND status='running' AND lease_until>now() FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'Lease lost'; END IF;
          IF j.kind='export' THEN
            FOR expected IN SELECT value FROM jsonb_array_elements(j.payload->'items') LOOP
              IF NOT EXISTS(SELECT 1 FROM skb_analytics.records r WHERE r.import_id=j.import_id
                AND r.kind='event' AND r.key=expected->>'event_id'
                AND r.payload->>'version'=expected->>'version' AND r.payload->>'status'='confirmed')
              THEN RAISE EXCEPTION 'Source changed before release'; END IF;
            END LOOP;
          END IF;
          INSERT INTO skb_analytics.assets(import_id,id,payload)
             SELECT j.import_id,key,value FROM jsonb_each(data->'assets') ON CONFLICT DO NOTHING;
          INSERT INTO skb_analytics.records(import_id,kind,key,payload)
             SELECT j.import_id,r.kind,r.key,r.payload FROM jsonb_to_recordset(data->'records')
                 AS r(kind text,key text,payload jsonb)
             ON CONFLICT(import_id,kind,key) DO UPDATE SET payload=excluded.payload
             WHERE skb_analytics.records.kind NOT IN ('report','draft');
          IF j.kind='collect' THEN
            UPDATE skb_analytics.imports SET manifest=manifest || jsonb_build_object(
              'source_created_at',now(),'mode','cloud') WHERE id=j.import_id;
          END IF;
          UPDATE skb_analytics.jobs SET status={status},result_id={result},stage='Готово',source='',
             finished_at=now(),token=NULL WHERE id=j.id;
        END;''').format(data=sql.Literal(canonical(payload)), id=sql.Literal(job['id']),token=sql.Literal(job['token']),
                       status=sql.Literal(status),result=sql.Literal(result_id))
        with self.repository.write_connect(**self.repository.params) as conn:
            conn.execute(sql.SQL('DO {}').format(sql.Literal(body.as_string(conn))))


class JobService:
    def __init__(self, settings, identity, store_factory=JobStore):
        self.settings,self.identity,self.store_factory=settings,identity,store_factory

    def context(self, write=False):
        config=self.settings()
        access=authorize(self.identity(),section(config,'access'))
        if not access.allowed or (write and access.role not in ('admin','editor')):
            raise PermissionError('Запуск доступен только приглашённому редактору или администратору.')
        return self.store_factory(config),access

    def export(self, import_id, period, revision):
        store,access=self.context(write=True)
        value=DraftService(self.settings,self.identity).preview(import_id,period,revision)
        key=hashlib.sha256(canonical(value).encode()).hexdigest()
        return store.enqueue(import_id,'export',period,value,access.email,key)

    def collect(self, import_id, period):
        store,access=self.context(write=True)
        from web.facts import month
        period,_=month(period)
        return store.enqueue(import_id,'collect',period,{},access.email,uuid4().hex)

    def list(self, import_id):
        store,_=self.context()
        return store.list(import_id)

    def retry(self, import_id, job_id):
        store,access=self.context(write=True)
        rows=store.query("SELECT * FROM skb_analytics.jobs WHERE import_id=$1 AND id=$2 AND status IN ('error','interrupted')",[import_id,job_id])
        if not rows:
            raise ValueError('Повтор доступен только для прерванного задания или ошибки.')
        old=rows[0]
        if old['kind']=='export':
            # Return to the latest saved revision instead of silently using stale facts.
            view=DraftService(self.settings,self.identity).open(import_id,old['period'])
            value=DraftService(self.settings,self.identity).preview(import_id,old['period'],view['draft']['revision'])
        else:
            value={}
        return store.enqueue(import_id,old['kind'],old['period'],value,access.email,uuid4().hex)
