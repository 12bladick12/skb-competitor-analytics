"""Checkpointed court records in Neon; all writes are fenced by the existing lease."""
from cloud.jobs import JobStore
from cloud.library import canonical
from .model import digest


class CourtStore(JobStore):
    def claim(self, token):
        rows=self.query('SELECT * FROM skb_analytics.claim_court_job($1)',[token],write=True)
        return rows[0] if rows else None

    def records(self, import_id, kind, *, case_id=None, limit=100, offset=0):
        if not kind.startswith('court_'):
            raise ValueError('Неизвестный раздел судебного реестра.')
        return [r['payload'] for r in self.query('''SELECT r.payload FROM skb_analytics.records r
            JOIN skb_analytics.state s ON s.import_id=r.import_id AND s.key='active'
            WHERE r.import_id=$1 AND r.kind=$2 AND ($3::text IS NULL OR r.payload->>'case_id'=$3)
            ORDER BY COALESCE(r.payload->>'occurred_on',r.payload->>'date',r.payload->>'registered_on',''),r.key
            LIMIT $4 OFFSET $5''',[import_id,kind,case_id,min(limit,1000),offset])]

    def all(self, import_id, kind):
        offset=0
        while True:
            rows=self.records(import_id,kind,limit=1000,offset=offset)
            yield from rows
            if len(rows)<1000:return
            offset+=len(rows)

    def get(self, import_id, kind, key):
        rows=self.query('SELECT payload FROM skb_analytics.records WHERE import_id=$1 AND kind=$2 AND key=$3',
                        [import_id,kind,key])
        return rows[0]['payload'] if rows else None

    def checkpoint(self, job, records, assets):
        from psycopg import sql
        if any(not r['kind'].startswith('court_') for r in records):
            raise ValueError('Судебный сборщик может изменять только свой реестр.')
        body=sql.SQL('''DECLARE j skb_analytics.jobs; data jsonb := {data}::jsonb; r record;
        BEGIN
          SELECT * INTO j FROM skb_analytics.jobs WHERE id={id} AND token={token}
            AND kind='court_collect' AND status='running' AND lease_until>now() FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'Lease lost'; END IF;
          IF NOT EXISTS(SELECT 1 FROM skb_analytics.state WHERE key='active' AND import_id=j.import_id)
            THEN RAISE EXCEPTION 'Library changed'; END IF;
          INSERT INTO skb_analytics.assets(import_id,id,payload)
            SELECT j.import_id,key,value FROM jsonb_each(data->'assets') ON CONFLICT DO NOTHING;
          FOR r IN SELECT * FROM jsonb_to_recordset(data->'records') AS x(kind text,key text,payload jsonb) LOOP
            INSERT INTO skb_analytics.records(import_id,kind,key,payload)
              SELECT import_id,'court_version',kind || ':' || key || ':' || md5(payload::text),
                jsonb_build_object('record_kind',kind,'record_id',key,'payload',payload,'superseded_at',now())
              FROM skb_analytics.records WHERE import_id=j.import_id AND kind=r.kind AND key=r.key
                AND payload IS DISTINCT FROM r.payload ON CONFLICT DO NOTHING;
            INSERT INTO skb_analytics.records VALUES(j.import_id,r.kind,r.key,r.payload)
              ON CONFLICT(import_id,kind,key) DO UPDATE SET payload=excluded.payload;
          END LOOP;
        END;''').format(data=sql.Literal(canonical({'records':records,'assets':assets})),
                       id=sql.Literal(job['id']),token=sql.Literal(job['token']))
        with self.repository.write_connect(**self.repository.params) as conn:
            conn.execute(sql.SQL('DO {}').format(sql.Literal(body.as_string(conn))))

    def enqueue_pair(self, import_id, period, email, request_id):
        """Commit both queues in one transaction; distinct periods share the court job."""
        from psycopg import sql
        body=sql.SQL('''BEGIN
          PERFORM pg_advisory_xact_lock(847263918);
          IF NOT EXISTS(SELECT 1 FROM skb_analytics.state WHERE key='active' AND import_id={import_id})
            THEN RAISE EXCEPTION 'Library changed'; END IF;
          INSERT INTO skb_analytics.jobs(id,import_id,kind,period,payload,created_by,request_key)
            VALUES({news_id},{import_id},'collect',{period},{payload}::jsonb,{email},{request_id}) ON CONFLICT DO NOTHING;
          INSERT INTO skb_analytics.jobs(id,import_id,kind,period,payload,created_by,request_key)
            VALUES({court_id},{import_id},'court_collect',{period},{payload}::jsonb,{email},{request_id}) ON CONFLICT DO NOTHING;
        END;''').format(import_id=sql.Literal(import_id),period=sql.Literal(period),email=sql.Literal(email),
                      request_id=sql.Literal(request_id),news_id=sql.Literal(digest(['news',request_id])),
                      court_id=sql.Literal(digest(['court',request_id])),payload=sql.Literal(canonical({'group_id':request_id})))
        with self.repository.write_connect(**self.repository.params) as conn:
            conn.execute(sql.SQL('DO {}').format(sql.Literal(body.as_string(conn))))
        return self.list(import_id)
