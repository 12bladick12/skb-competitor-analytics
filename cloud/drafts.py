"""Shared drafts: atomic compare-and-swap plus append-only revision history."""
import json
import re
from uuid import uuid4

from .access import authorize
from .draft_rules import DraftConflict, create_payload, issues, refresh_payload, save_payload, snapshot
from .drive_store import StorageError
from .library import Repository
from .readiness import section


DDL = """DO $draft_schema$ BEGIN
    PERFORM pg_advisory_xact_lock(847263916);
    CREATE TABLE IF NOT EXISTS skb_analytics.draft_heads (
        import_id text NOT NULL REFERENCES skb_analytics.imports(id),
        id text NOT NULL, period text NOT NULL, revision integer NOT NULL CHECK (revision > 0),
        payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(), updated_by text NOT NULL,
        PRIMARY KEY(import_id,id), UNIQUE(import_id,period));
    CREATE TABLE IF NOT EXISTS skb_analytics.draft_revisions (
        import_id text NOT NULL, draft_id text NOT NULL, revision integer NOT NULL,
        payload jsonb NOT NULL, updated_at timestamptz NOT NULL, updated_by text NOT NULL,
        PRIMARY KEY(import_id,draft_id,revision),
        FOREIGN KEY(import_id,draft_id) REFERENCES skb_analytics.draft_heads(import_id,id));
    INSERT INTO skb_analytics.draft_heads(import_id,id,period,revision,payload,updated_at,updated_by)
        SELECT r.import_id,r.payload->>'id',r.payload->>'period',(r.payload->>'revision')::integer,
            jsonb_build_object('conclusions',r.payload->'conclusions','items',r.payload->'items'),
            (r.payload->>'updated_at')::timestamptz,'Перенесённый черновик'
        FROM skb_analytics.records r JOIN skb_analytics.state s ON s.import_id=r.import_id AND s.key='active'
        WHERE r.kind='draft' ON CONFLICT DO NOTHING;
    INSERT INTO skb_analytics.draft_revisions
        SELECT import_id,id,revision,payload,updated_at,updated_by FROM skb_analytics.draft_heads
        ON CONFLICT DO NOTHING;
END $draft_schema$;"""


def document(row):
    if not row:
        return None
    return {**row['payload'], 'id': row['id'], 'period': row['period'], 'revision': int(row['revision']),
            'updated_at': str(row['updated_at']), 'updated_by': row['updated_by']}


class DraftStore:
    def __init__(self, config, repository=None):
        self.repository = repository or Repository(config)

    def initialize(self):
        # Explicit deployment migration only. Page loads never create tables.
        try:
            with self.repository.write_connect(**self.repository.params) as conn:
                conn.execute(DDL)
        except Exception:
            raise StorageError('Не удалось подготовить хранилище черновиков. Действующие материалы не изменены.') from None

    def query(self, statement, params=(), write=False):
        if self.repository.http is not None:
            return self.repository.http.query(statement, params)
        ordered = []
        def placeholder(match):
            ordered.append(params[int(match[1])-1])
            return '%s'
        statement = re.sub(r'\$(\d+)', placeholder, statement)
        with self.repository.transaction(write=write) as conn:
            cursor = conn.execute(statement, ordered)
            return [dict(zip([c.name for c in cursor.description], row)) for row in cursor.fetchall()]

    def read(self, import_id, period):
        rows = self.query('''SELECT d.* FROM skb_analytics.draft_heads d
            JOIN skb_analytics.state s ON s.import_id=d.import_id AND s.key='active'
            WHERE d.import_id=$1 AND d.period=$2''', [import_id, period])
        return document(rows[0]) if rows else None

    def asset_ids(self, import_id):
        return {r['id'] for r in self.query('SELECT id FROM skb_analytics.assets WHERE import_id=$1', [import_id])}

    def create(self, import_id, period, payload, email):
        rows = self.query('''WITH made AS (
            INSERT INTO skb_analytics.draft_heads(import_id,id,period,revision,payload,updated_by)
            SELECT $1,$2,$3,1,$4::jsonb,$5 WHERE EXISTS (
                SELECT 1 FROM skb_analytics.state s JOIN skb_analytics.records r ON r.import_id=s.import_id
                WHERE s.key='active' AND s.import_id=$1 AND r.kind='period' AND r.key=$3)
            ON CONFLICT(import_id,period) DO NOTHING RETURNING *
        ), history AS (
            INSERT INTO skb_analytics.draft_revisions
            SELECT import_id,id,revision,payload,updated_at,updated_by FROM made RETURNING draft_id
        ) SELECT made.* FROM made JOIN history ON history.draft_id=made.id''',
            [import_id, uuid4().hex, period, json.dumps(payload, ensure_ascii=False), email], write=True)
        result = document(rows[0]) if rows else self.read(import_id, period)
        if not result:
            raise DraftConflict('Набор материалов изменился. Обновите страницу.')
        return result

    def commit(self, import_id, draft, expected_revision, payload, email):
        rows = self.query('''WITH changed AS (
            UPDATE skb_analytics.draft_heads SET revision=revision+1,payload=$4::jsonb,
                updated_at=clock_timestamp(),updated_by=$5
            WHERE import_id=$1 AND id=$2 AND revision=$3
                AND EXISTS (SELECT 1 FROM skb_analytics.state WHERE key='active' AND import_id=$1)
            RETURNING *
        ), history AS (
            INSERT INTO skb_analytics.draft_revisions
            SELECT import_id,id,revision,payload,updated_at,updated_by FROM changed RETURNING draft_id
        ) SELECT changed.* FROM changed JOIN history ON history.draft_id=changed.id''',
            [import_id, draft['id'], expected_revision, json.dumps(payload, ensure_ascii=False), email], write=True)
        if not rows:
            raise DraftConflict('Другой сотрудник уже сохранил новую редакцию. Ваши правки остались в этой вкладке; сначала сравните версии.')
        return document(rows[0])

    def history(self, import_id, draft_id):
        return self.query('''SELECT revision,updated_at::text,updated_by FROM skb_analytics.draft_revisions
            WHERE import_id=$1 AND draft_id=$2 ORDER BY revision DESC LIMIT 100''', [import_id, draft_id])

    def version(self, import_id, draft_id, revision):
        rows = self.query('''SELECT h.draft_id AS id,d.period,h.revision,h.payload,h.updated_at,h.updated_by
            FROM skb_analytics.draft_revisions h JOIN skb_analytics.draft_heads d
                ON d.import_id=h.import_id AND d.id=h.draft_id
            WHERE h.import_id=$1 AND h.draft_id=$2 AND h.revision=$3''', [import_id, draft_id, revision])
        if not rows:
            raise ValueError('Редакция не найдена.')
        return document(rows[0])


class DraftService:
    def __init__(self, settings, identity, store_factory=DraftStore):
        self.settings, self.identity, self.store_factory = settings, identity, store_factory

    def context(self, import_id, period, *, write=False):
        config = self.settings()
        access = authorize(self.identity(), section(config, 'access'))
        if not access.allowed or (write and access.role not in ('admin', 'editor')):
            raise PermissionError('Изменять записки могут только редактор и администратор.' if write else 'Доступ отозван. Обновите страницу.')
        store = self.store_factory(config)
        library = store.repository.load()  # Never use the shared UI cache for editorial decisions.
        if not library or library['id'] != import_id or period not in library['period']:
            raise DraftConflict('Набор материалов изменился. Обновите страницу.')
        return store, library, access

    def open(self, import_id, period):
        store, library, access = self.context(import_id, period)
        draft = store.read(import_id, period)
        return {'draft': draft, 'library': library, 'issues': issues(draft, library, store.asset_ids(import_id)) if draft else [],
                'role': access.role}

    def create(self, import_id, period):
        store, library, access = self.context(import_id, period, write=True)
        return store.create(import_id, period, create_payload(library, period, store.asset_ids(import_id)), access.email)

    def _draft(self, store, import_id, period, revision):
        draft = store.read(import_id, period)
        if not draft or type(revision) is not int or draft['revision'] != revision:
            raise DraftConflict('Другой сотрудник уже сохранил новую редакцию. Ваши правки остались в этой вкладке; сначала сравните версии.')
        return draft

    def save(self, import_id, period, revision, conclusions, changes):
        store, library, access = self.context(import_id, period, write=True)
        draft = self._draft(store, import_id, period, revision)
        assets = store.asset_ids(import_id)
        payload = save_payload(draft, library, conclusions, changes, assets)
        saved = store.commit(import_id, draft, revision, payload, access.email)
        self.saved_view = {'draft': saved, 'library': library, 'issues': issues(saved, library, assets), 'role': access.role}
        return saved

    def refresh(self, import_id, period, revision):
        store, library, access = self.context(import_id, period, write=True)
        draft = self._draft(store, import_id, period, revision)
        payload = refresh_payload(draft, library, store.asset_ids(import_id))
        return store.commit(import_id, draft, revision, payload, access.email)

    def preview(self, import_id, period, revision):
        store, library, _ = self.context(import_id, period)
        draft = self._draft(store, import_id, period, revision)
        return snapshot(draft, library, store.asset_ids(import_id))

    def history(self, import_id, period, revision=None):
        store, _, _ = self.context(import_id, period)
        draft = store.read(import_id, period)
        if not draft:
            return [] if revision is None else None
        return (store.history(import_id, draft['id']) if revision is None
                else store.version(import_id, draft['id'], revision))
