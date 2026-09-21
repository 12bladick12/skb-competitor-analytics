"""Persistent registration and administrative access control. Never cache roles."""
from .access import ROLE_LABELS, current_access, normalized_email
from .drafts import DraftStore
from .library import canonical


DDL="""DO $members$ BEGIN
 PERFORM pg_advisory_xact_lock(847263916);
 CREATE TABLE IF NOT EXISTS skb_analytics.members (
   email text PRIMARY KEY, subject text UNIQUE,
   role text NOT NULL CHECK(role IN ('admin','editor','viewer')),
   status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','blocked')),
   protected boolean NOT NULL DEFAULT false,
   revision integer NOT NULL DEFAULT 1,
   created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
 CREATE TABLE IF NOT EXISTS skb_analytics.member_audit (
   id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
   actor text NOT NULL, target text NOT NULL, action text NOT NULL,
   previous jsonb NOT NULL, current jsonb NOT NULL,
   created_at timestamptz NOT NULL DEFAULT now());
END $members$;"""


class MemberStore(DraftStore):
    def initialize(self, invitations):
        from psycopg import sql
        seeds={}
        for role in ROLE_LABELS:
            values=invitations.get(role+'_emails',[])
            if not isinstance(values,(list,tuple)):
                continue
            for raw in values:
                email=normalized_email(raw)
                if email and email not in seeds:
                    seeds[email]={'email':email,'role':role,'protected':role=='admin'}
        if not any(row['protected'] for row in seeds.values()):
            raise ValueError('Для запуска нужен хотя бы один администратор в настройках.')
        # Idempotent migration cannot reactivate blocked members or reset roles.
        seed=sql.SQL('''BEGIN
          INSERT INTO skb_analytics.members(email,role,protected)
          SELECT email,role,protected FROM jsonb_to_recordset({data}::jsonb)
             AS r(email text,role text,protected boolean) ON CONFLICT DO NOTHING;
        END;''').format(data=sql.Literal(canonical(list(seeds.values()))))
        with self.repository.write_connect(**self.repository.params) as conn:
            conn.execute(DDL)
            conn.execute(sql.SQL('DO {}').format(sql.Literal(seed.as_string(conn))))

    def find(self, email, subject):
        rows=self.query('SELECT * FROM skb_analytics.members WHERE email=$1 OR subject=$2',[email,subject])
        # Ambiguous identity (another subject already owns the email) is denied.
        return rows[0] if len(rows)==1 else None

    def register(self, email, subject):
        # One transaction; the same account/email cannot race into a second role.
        from psycopg import sql
        body=sql.SQL('''BEGIN
          PERFORM pg_advisory_xact_lock(847263918);
          IF NOT EXISTS(SELECT 1 FROM skb_analytics.members WHERE subject={subject}) THEN
            INSERT INTO skb_analytics.members(email,subject,role) VALUES({email},{subject},'viewer')
              ON CONFLICT(email) DO UPDATE SET subject=excluded.subject
              WHERE skb_analytics.members.subject IS NULL;
          END IF;
        END;''').format(email=sql.Literal(email),subject=sql.Literal(subject))
        with self.repository.write_connect(**self.repository.params) as conn:
            conn.execute(sql.SQL('DO {}').format(sql.Literal(body.as_string(conn))))
        return self.find(email,subject)

    def list(self):
        return self.query('''SELECT email,role,status,protected,revision,created_at::text,updated_at::text
            FROM skb_analytics.members ORDER BY (status='active') DESC,created_at DESC,email''')

    def change(self, actor, target, revision, role, status):
        if role not in ROLE_LABELS or status not in ('active','blocked'):
            raise ValueError('Некорректные права пользователя.')
        from psycopg import sql
        body=sql.SQL('''DECLARE previous skb_analytics.members; changed skb_analytics.members;
        BEGIN
          PERFORM pg_advisory_xact_lock(847263918);
          IF NOT EXISTS(SELECT 1 FROM skb_analytics.members WHERE email={actor}
              AND status='active' AND role='admin') THEN
            RAISE EXCEPTION 'Admin access revoked';
          END IF;
          SELECT * INTO previous FROM skb_analytics.members WHERE email={target} FOR UPDATE;
          IF NOT FOUND OR previous.revision<>{revision} THEN RAISE EXCEPTION 'Member changed'; END IF;
          IF previous.protected OR previous.email={actor} THEN RAISE EXCEPTION 'Protected member'; END IF;
          IF previous.role='admin' AND previous.status='active' AND ({role}<>'admin' OR {status}<>'active')
              AND (SELECT count(*) FROM skb_analytics.members WHERE role='admin' AND status='active')<=1
          THEN RAISE EXCEPTION 'Last administrator'; END IF;
          UPDATE skb_analytics.members SET role={role},status={status},revision=revision+1,updated_at=now()
             WHERE email={target} RETURNING * INTO changed;
          INSERT INTO skb_analytics.member_audit(actor,target,action,previous,current)
            VALUES({actor},{target},'access_changed',to_jsonb(previous)-'subject',to_jsonb(changed)-'subject');
        END;''').format(actor=sql.Literal(actor),target=sql.Literal(target),revision=sql.Literal(int(revision)),
                       role=sql.Literal(role),status=sql.Literal(status))
        try:
            with self.repository.write_connect(**self.repository.params) as conn:
                conn.execute(sql.SQL('DO {}').format(sql.Literal(body.as_string(conn))))
        except Exception:
            raise ValueError('Права не изменены. Обновите список: пользователь или ваши полномочия могли измениться.') from None


class MemberService:
    def __init__(self, settings, identity, store_factory=MemberStore):
        self.settings,self.identity,self.store_factory=settings,identity,store_factory

    def context(self):
        config=self.settings()
        access=current_access(self.identity(),config,store_factory=self.store_factory)
        if access.role!='admin':
            raise PermissionError('Управление пользователями доступно администратору.')
        return self.store_factory(config),access

    def list(self):
        store,_=self.context()
        return store.list()

    def change(self, target, revision, role, status):
        store,access=self.context()
        store.change(access.email,target,revision,role,status)
