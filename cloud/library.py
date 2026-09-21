"""Versioned import/read model in Neon; imports become visible atomically."""

from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import re
from zipfile import ZipFile, BadZipFile

from .drive_store import DriveStore, StorageError
from .readiness import neon_parameters, section
from .neon_http import MigrationConnection, NeonError, NeonHTTP


VISIBLE_KINDS = ("event", "period", "competitor", "run", "report", "draft")


def canonical(value):
    # JSON changes numeric mapping keys into strings; normalize before sorting
    # so preparation and a later reload produce the same import identifier.
    normalized=json.loads(json.dumps(value,ensure_ascii=False))
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def catalog_id(catalog):
    return hashlib.sha256(canonical(catalog).encode()).hexdigest()


class Repository:
    def __init__(self, config, connect=None, *, migration=False):
        import psycopg
        self.params = neon_parameters(section(config, "cloud").get("database_url"))
        self.params.update(connect_timeout=30, prepare_threshold=None)
        if migration:
            # Neon recommends the direct endpoint for schema migrations.
            # Preserve the exact endpoint/account, removing only pooler routing.
            endpoint, suffix = self.params['host'].split('.', 1)
            self.params['host'] = endpoint.removesuffix('-pooler') + '.' + suffix
        self.params['application_name'] = 'skb-analytics-migration' if migration else 'skb-analytics-library'
        self.connect = connect or psycopg.connect
        self.http = None if connect else NeonHTTP(config)
        self.write_connect = connect or (lambda **_: MigrationConnection(self.http))

    @contextmanager
    def transaction(self, *, write=False):
        try:
            with self.connect(**self.params) as conn:
                conn.read_only = not write
                with conn.transaction():
                    conn.execute("SET LOCAL statement_timeout = '30s'")
                    conn.execute("SET LOCAL idle_in_transaction_session_timeout = '120s'")
                    yield conn
        except (StorageError, ValueError):
            raise
        except Exception:
            raise StorageError("Общее хранилище временно недоступно. Повторите действие; данные не сбрасываются.") from None

    def initialize(self):
        # Only the migration command can create schema, never a page request.
        # Submit the complete DDL transaction in one round trip. A dropped client
        # connection cannot leave a successfully created schema waiting for the
        # next client command/COMMIT. Repeating this transaction is safe.
        ddl = """DO $migration$ BEGIN
                IF NOT pg_try_advisory_xact_lock(847263915) THEN
                    RAISE EXCEPTION 'Another import is in progress';
                END IF;
            CREATE SCHEMA IF NOT EXISTS skb_analytics;
            CREATE TABLE IF NOT EXISTS skb_analytics.imports (
                id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), manifest JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS skb_analytics.records (
                import_id TEXT NOT NULL REFERENCES skb_analytics.imports(id), kind TEXT NOT NULL,
                key TEXT NOT NULL, payload JSONB NOT NULL, PRIMARY KEY(import_id,kind,key));
            CREATE TABLE IF NOT EXISTS skb_analytics.assets (
                import_id TEXT NOT NULL REFERENCES skb_analytics.imports(id), id TEXT NOT NULL,
                payload JSONB NOT NULL, PRIMARY KEY(import_id,id));
            CREATE TABLE IF NOT EXISTS skb_analytics.state (
                key TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES skb_analytics.imports(id));
            END $migration$;
        """
        try:
            with self.write_connect(**self.params) as conn:
                conn.execute(ddl)
        except Exception:
            raise StorageError("Не удалось подтвердить создание облачных таблиц. Повторите перенос.") from None

    def activate(self, catalog, packs):
        from psycopg import sql
        from psycopg.types.json import Jsonb
        ident = catalog_id(catalog)
        if set(packs) != {a["pack"] for a in catalog["assets"].values()}:
            raise ValueError("Не все пакеты подтверждены в Drive.")
        records = catalog["records"]
        if len({(r['kind'],r['key']) for r in records}) != len(records):
            raise ValueError("Повтор идентификатора при переносе.")
        # One server-side transaction commits independently of a client losing
        # the reply. All literals, including the anonymous block itself, are
        # escaped by Psycopg: source text cannot close a dollar-quoted block.
        body = sql.SQL("""
            DECLARE import_key text := {ident}; catalog_json jsonb := {catalog};
                    packs_json jsonb := {packs}; existing text; affected bigint;
            BEGIN
                IF NOT pg_try_advisory_xact_lock(847263915) THEN
                    RAISE EXCEPTION 'Another import is in progress' USING ERRCODE='55P03';
                END IF;
                SELECT import_id INTO existing FROM skb_analytics.state WHERE key='active';
                IF existing IS NOT NULL THEN
                    IF existing <> import_key THEN
                        RAISE EXCEPTION 'Active import differs' USING ERRCODE='23505';
                    END IF;
                    RETURN;
                END IF;
                INSERT INTO skb_analytics.imports(id,manifest) VALUES(import_key,catalog_json->'manifest');
                INSERT INTO skb_analytics.records(import_id,kind,key,payload)
                    SELECT import_key,r.kind,r.key,r.payload
                    FROM jsonb_to_recordset(catalog_json->'records') AS r(kind text,key text,payload jsonb);
                GET DIAGNOSTICS affected = ROW_COUNT;
                IF affected <> jsonb_array_length(catalog_json->'records') THEN
                    RAISE EXCEPTION 'Record count mismatch';
                END IF;
                INSERT INTO skb_analytics.assets(import_id,id,payload)
                    SELECT import_key,a.key,a.value || jsonb_build_object('drive_id',packs_json->>(a.value->>'pack'))
                    FROM jsonb_each(catalog_json->'assets') AS a;
                INSERT INTO skb_analytics.state(key,import_id) VALUES('active',import_key);
            END;
        """).format(ident=sql.Literal(ident),catalog=sql.Literal(Jsonb(catalog,dumps=canonical)),packs=sql.Literal(Jsonb(packs)))
        try:
            with self.write_connect(**self.params) as conn:
                statement = sql.SQL("DO {}").format(sql.Literal(body.as_string(conn)))
                conn.execute(statement)
        except Exception as exc:
            if getattr(exc, 'sqlstate', None) == '23505':
                raise ValueError("Облачные данные уже существуют. Другой набор не может их перезаписать.") from None
            if getattr(exc, 'sqlstate', None) == '55P03':
                raise StorageError("Предыдущее подключение ещё освобождает блокировку. Повторите перенос через две минуты.") from None
            raise StorageError("Не удалось подтвердить запись набора. Повторите перенос: дубликаты не создаются.") from None
        return ident

    def load(self):
        if self.http is not None:
            try:
                rows=self.http.query("""SELECT i.id,i.manifest,r.kind,r.key,r.payload
                    FROM skb_analytics.state s JOIN skb_analytics.imports i ON i.id=s.import_id
                    LEFT JOIN skb_analytics.records r ON r.import_id=i.id
                    AND r.kind IN ('event','period','competitor','run','report','draft')
                    WHERE s.key='active'""")
            except NeonError as exc:
                if exc.sqlstate == '42P01':
                    return None
                raise
            if not rows:
                return None
            result={kind:{} for kind in VISIBLE_KINDS}
            for row in rows:
                if row['kind'] in result:
                    result[row['kind']][row['key']]=row['payload']
            return {'id':rows[0]['id'],'manifest':rows[0]['manifest'],**result}
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
        if self.http is not None:
            rows=self.http.query("""SELECT a.payload FROM skb_analytics.assets a
                JOIN skb_analytics.state s ON s.import_id=a.import_id AND s.key='active'
                WHERE a.import_id=$1 AND a.id=$2""",[import_id,asset_id])
            if not rows:
                raise StorageError("Файл не найден.")
            return rows[0]['payload']
        with self.transaction() as conn:
            row = conn.execute("""SELECT a.payload FROM skb_analytics.assets a
                JOIN skb_analytics.state s ON s.import_id=a.import_id AND s.key='active'
                WHERE a.import_id=%s AND a.id=%s""", (import_id, asset_id)).fetchone()
        if not row:
            raise StorageError("Файл не найден.")
        return row[0]


class AssetIndex:
    """One immutable registry read per worker, instead of one SQL call per file."""
    def __init__(self, store, import_id):
        self.import_id=import_id
        self.records={row['id']:row['payload'] for row in store.query('''SELECT a.id,a.payload
            FROM skb_analytics.assets a JOIN skb_analytics.state s ON s.import_id=a.import_id AND s.key='active'
            WHERE a.import_id=$1''',[import_id])}

    def asset(self, import_id, asset_id):
        if import_id!=self.import_id or not re.fullmatch('[a-f0-9]{64}',str(asset_id)) or asset_id not in self.records:
            raise StorageError('Файл не найден.')
        return self.records[asset_id]


def read_asset(repository, drive, import_id, asset_id, *, _part=False):
    record = repository.asset(import_id, asset_id)
    if 'chunks' in record:
        chunks=record['chunks']
        size=record.get('bytes',0)
        if _part or not isinstance(chunks,list) or not 1<=len(chunks)<=8 or not isinstance(size,int) or not 0<size<=32*1024*1024:
            raise StorageError('Некорректный состав файла.')
        content=bytearray()
        for ident in chunks:
            content.extend(read_asset(repository,drive,import_id,ident,_part=True))
            if len(content)>size:
                raise StorageError('Размер вложения не совпадает с реестром.')
        if len(content)!=size or hashlib.sha256(content).hexdigest()!=asset_id:
            raise StorageError('Контрольная сумма вложения не совпадает.')
        return bytes(content)
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
