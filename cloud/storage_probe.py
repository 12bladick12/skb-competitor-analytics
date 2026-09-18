"""Explicit, disposable persistence checks. Never touch monitoring tables/files."""

import hashlib
import json
import re
from uuid import uuid4

from .readiness import Check, check_drive, neon_parameters, section


def check_neon_write(config, *, connect=None):
    try:
        params = neon_parameters(section(config, "cloud").get("database_url"))
    except ValueError as exc:
        return Check("neon_write", "pending", str(exc))
    if connect is None:
        import psycopg
        connect = psycopg.connect
    # Identifiers are generated here, never accepted from configuration/UI.
    name = "skb_storage_check_" + uuid4().hex
    table = 'public."' + name + '"'
    value = "storage-check-" + uuid4().hex
    attempted = False
    verified = False
    cleaned = True
    try:
        with connect(**params) as connection:
            with connection.transaction():
                connection.execute("SET LOCAL statement_timeout = '10s'")
                attempted = True
                connection.execute(f"CREATE TABLE {table} (value TEXT NOT NULL)")
                connection.execute(f"INSERT INTO {table} (value) VALUES (%s)", (value,))
        # A new connection proves this was committed to persistent storage.
        with connect(**params) as connection:
            connection.read_only = True
            with connection.transaction():
                connection.execute("SET LOCAL statement_timeout = '10s'")
                verified = connection.execute(f"SELECT value FROM {table}").fetchone() == (value,)
    except Exception:
        verified = False
    finally:
        if attempted:
            try:
                with connect(**params) as connection:
                    with connection.transaction():
                        connection.execute("SET LOCAL statement_timeout = '10s'")
                        connection.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception:
                cleaned = False
    details = {"readback_verified": verified, "cleanup_verified": cleaned}
    if not cleaned:
        return Check("neon_write", "warning", "Проверка Neon не завершена: не удалось удалить тестовую таблицу "
                     + name + ". Рабочие таблицы не затрагивались.", details)
    if not verified:
        return Check("neon_write", "error", "Запись и повторное чтение Neon не подтверждены. Проверьте права базы и соединение.", details)
    return Check("neon_write", "ok", "Neon: запись сохранена и прочитана через новое подключение. Тестовая таблица удалена.", details)


def check_drive_write(config, *, session=None):
    pending = check_drive(config, offline=True)
    if pending.status != "unchecked":
        return Check("drive_write", pending.status, pending.message, pending.details)
    settings = section(config, "drive")
    owns_session = session is None
    if owns_session:
        import requests
        session = requests.Session()
    name = "skb-storage-check-" + uuid4().hex + ".txt"
    content = ("SKB Analytics storage check\n" + uuid4().hex + "\n").encode("ascii")
    file_id = None
    attempted = False
    verified = False
    cleaned = True
    headers = {}
    api = "https://www.googleapis.com/drive/v3/files"
    options = {"timeout": (5, 15), "allow_redirects": False}
    try:
        response = session.post("https://oauth2.googleapis.com/token", data={
            key: settings[key] for key in ("client_id", "client_secret", "refresh_token")
        } | {"grant_type": "refresh_token"}, **options)
        if response.status_code != 200:
            raise ValueError
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError
        headers = {"Authorization": "Bearer " + token}
        # Allocate the ID before upload: even an ambiguous upload timeout can
        # be cleaned up without searching or deleting unrelated owner files.
        response = session.get(api + "/generateIds", params={"count": 1, "space": "drive", "type": "files"}, headers=headers, **options)
        if response.status_code != 200:
            raise ValueError
        ids = response.json().get("ids")
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", ids[0]):
            raise ValueError
        file_id = ids[0]
        boundary = "skb_" + uuid4().hex
        metadata = json.dumps({"id": file_id, "name": name, "mimeType": "text/plain",
                               "appProperties": {"purpose": "skb-storage-check"}}).encode("utf-8")
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + metadata + f"\r\n--{boundary}\r\nContent-Type: text/plain\r\n\r\n".encode()
                + content + f"\r\n--{boundary}--\r\n".encode())
        attempted = True
        response = session.post("https://www.googleapis.com/upload/drive/v3/files",
                                params={"uploadType": "multipart", "fields": "id"}, data=body,
                                headers=headers | {"Content-Type": "multipart/related; boundary=" + boundary}, **options)
        if response.status_code != 200 or response.json().get("id") != file_id:
            raise ValueError
        response = session.get(api + "/" + file_id, params={"alt": "media"}, headers=headers, **options)
        verified = response.status_code == 200 and hashlib.sha256(response.content).digest() == hashlib.sha256(content).digest()
    except Exception:
        # Never reveal Google responses, credentials or requests exceptions.
        verified = False
    finally:
        if attempted:
            try:
                response = session.delete(api + "/" + file_id, headers=headers, **options)
                if response.status_code not in (204, 404):
                    raise ValueError
                response = session.get(api + "/" + file_id, params={"fields": "id"}, headers=headers, **options)
                cleaned = response.status_code == 404
            except Exception:
                cleaned = False
        if owns_session:
            session.close()
    details = {"readback_verified": verified, "cleanup_verified": cleaned}
    if not cleaned:
        return Check("drive_write", "warning", "Проверка Drive не завершена: удаление тестового файла "
                     + name + " не подтверждено. Удалите только этот файл из своего Диска.", details)
    if not verified:
        return Check("drive_write", "error", "Запись и повторное чтение Drive не подтверждены. Проверьте соединение и разрешение drive.file.", details)
    return Check("drive_write", "ok", "Drive: тестовый файл записан, скачан и сверен по SHA-256. Файл удалён.", details)
