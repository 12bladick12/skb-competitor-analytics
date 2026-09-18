"""Versioned import/read model in Neon; imports become visible atomically."""

from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import re
from zipfile import ZipFile, BadZipFile

from .drive_store import DriveStore, StorageError
from .readiness import neon_parameters, section


VISIBLE_KINDS = ("event", "period", "competitor", "run", "report", "draft")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def catalog_id(catalog):
    return hashlib.sha256(canonical(catalog).encode()).hexdigest()


class Repository:
    def __init__(self, config, connect=None):
        import psycopg
        self.params = neon_parameters(section(config, "cloud").get("database_url"))
        self.params.update(connect_timeout=30, prepare_threshold=None)
        self.connect = connect or psycopg.connect

    @contextmanager
    def transaction(self, *, write=False):
        try:
            with self.connect(**self.params) as conn:
                conn.read_only = not write
                with conn.transaction():
                    conn.execute("SET LOCAL statement_timeout = '30s'")
                    yield conn
        except (StorageError, ValueError):
            raise
        except Exception:
            raise StorageError("Общее хранилище временно недоступно. Повторите действие; данные не сбрасываются.") from None

    def initialize(self):
        # Only the migration command can create schema, never a page request.
        with self.transaction(write=True) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(847263915)")
            conn.execute("CREATE SCHEMA IF NOT EXISTS skb_analytics")
            conn.execute("""CREATE TABLE IF NOT EXISTS skb_analytics.imports (
                id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), manifest JSONB NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS skb_analytics.records (
                import_id TEXT NOT NULL REFERENCES skb_analytics.imports(id), kind TEXT NOT NULL,
                key TEXT NOT NULL, payload JSONB NOT NULL, PRIMARY KEY(import_id,kind,key))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS skb_analytics.assets (
                import_id TEXT NOT NULL REFERENCES skb_analytics.imports(id), id TEXT NOT NULL,
                payload JSONB NOT NULL, PRIMARY KEY(import_id,id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS skb_analytics.state (
                key TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES skb_analytics.imports(id))""")

    def activate(self, catalog, packs):
        from psycopg.types.json import Jsonb
        ident = catalog_id(catalog)
        if set(packs) != {a["pack"] for a in catalog["assets"].values()}:
            raise ValueError("Не все пакеты подтверждены в Drive.")
        records = catalog["records"]
        if len({(r['kind'],r['key']) for r in records}) != len(records):
            raise ValueError("Повтор идентификатора при переносе.")
        with self.transaction(write=True) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(847263915)")
            active = conn.execute("SELECT import_id FROM skb_analytics.state WHERE key='active'").fetchone()
            if active:
                if active[0] == ident:
                    return ident
                raise ValueError("Облачные данные уже существуют. Повторный импорт другого набора не разрешён.")
            conn.execute("INSERT INTO skb_analytics.imports(id,manifest) VALUES(%s,%s)",
                         (ident, Jsonb(catalog["manifest"])))
            with conn.cursor() as cursor:
                cursor.executemany("INSERT INTO skb_analytics.records(import_id,kind,key,payload) VALUES(%s,%s,%s,%s)",
                                   [(ident,r['kind'],r['key'],Jsonb(r['payload'])) for r in records])
                cursor.executemany("INSERT INTO skb_analytics.assets(import_id,id,payload) VALUES(%s,%s,%s)",
                                   [(ident,digest,Jsonb(a | {"drive_id": packs[a['pack']]})) for digest,a in catalog['assets'].items()])
            if conn.execute("SELECT count(*) FROM skb_analytics.records WHERE import_id=%s", (ident,)).fetchone()[0] != len(records):
                raise ValueError("Число перенесённых записей не совпало.")
            conn.execute("INSERT INTO skb_analytics.state(key,import_id) VALUES('active',%s)", (ident,))
        return ident

    def load(self):
        with self.transaction() as conn:
            if not conn.execute("SELECT to_regclass('skb_analytics.state')").fetchone()[0]:
                return None
            active = conn.execute("""SELECT i.id,i.manifest FROM skb_analytics.state s
                JOIN skb_analytics.imports i ON i.id=s.import_id WHERE s.key='active'""").fetchone()
            if not active:
                return None
            rows = conn.execute("SELECT kind,key,payload FROM skb_analytics.records WHERE import_id=%s AND kind=ANY(%s)",
                                (active[0], list(VISIBLE_KINDS))).fetchall()
        result = {kind: {} for kind in VISIBLE_KINDS}
        for kind, key, payload in rows:
            result[kind][key] = payload
        return {"id": active[0], "manifest": active[1], **result}

    def asset(self, import_id, asset_id):
        if not re.fullmatch(r"[a-f0-9]{64}", str(asset_id)):
            raise StorageError("Файл не найден.")
        with self.transaction() as conn:
            row = conn.execute("""SELECT a.payload FROM skb_analytics.assets a
                JOIN skb_analytics.state s ON s.import_id=a.import_id AND s.key='active'
                WHERE a.import_id=%s AND a.id=%s""", (import_id, asset_id)).fetchone()
        if not row:
            raise StorageError("Файл не найден.")
        return row[0]


def read_asset(repository, drive, import_id, asset_id):
    record = repository.asset(import_id, asset_id)
    data = drive.download(record["drive_id"], record["pack"])
    try:
        with ZipFile(BytesIO(data)) as archive:
            info = archive.getinfo(asset_id)
            if info.file_size != record["bytes"] or info.file_size > 32 * 1024 * 1024:
                raise StorageError("Размер вложения не совпадает с реестром.")
            content = archive.read(info)
    except (BadZipFile, KeyError, OSError):
        raise StorageError("Пакет доказательств повреждён.") from None
    if hashlib.sha256(content).hexdigest() != asset_id:
        raise StorageError("Контрольная сумма вложения не совпадает.")
    return content


def period_events(library, period, *, competitor="", kind="", query=""):
    ids = library["period"].get(period, {}).get("event_ids", [])
    query = query.strip().casefold()
    events = [library["event"][str(i)] for i in ids]
    return [e for e in events if e['status'] == 'confirmed' and e.get('evidence_ids')
            and (not competitor or e['competitor_code'] == competitor)
            and (not kind or e['kind'] == kind)
            and (not query or query in (e['title'] + ' ' + e['original_text']).casefold())]
