"""Private Drive objects. IDs are server-owned; no sharing permissions are created."""

from contextlib import contextmanager
import hashlib
import json
import re
import time
from uuid import uuid4

from .readiness import section


class StorageError(RuntimeError):
    pass


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", value):
        raise StorageError("Некорректный идентификатор файла хранилища.")
    return value


class DriveStore:
    API = "https://www.googleapis.com/drive/v3/files"
    MAX_PACK = 5 * 1024 * 1024

    def __init__(self, config, session=None):
        import requests
        self.settings = section(config, "drive")
        self.session = session or requests.Session()
        self.token = ""
        self.expires_at = 0

    def close(self):
        self.session.close()

    def _headers(self):
        if time.monotonic() < self.expires_at:
            return {"Authorization": "Bearer " + self.token}
        try:
            fields = ("client_id", "client_secret", "refresh_token")
            if any(not isinstance(self.settings.get(k), str) or not self.settings[k].strip() for k in fields):
                raise ValueError
            with self.session.post("https://oauth2.googleapis.com/token", data={
                k: self.settings[k] for k in fields} | {"grant_type": "refresh_token"},
                timeout=(10, 30), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ValueError
                token = response.json().get("access_token")
                if not isinstance(token, str) or not token:
                    raise ValueError
            self.token, self.expires_at = token, time.monotonic() + 1800
            return {"Authorization": "Bearer " + token}
        except Exception:
            raise StorageError("Google Drive не подтвердил доступ. Администратору нужно проверить подключение хранилища.") from None

    @contextmanager
    def request(self, method, url, **kwargs):
        try:
            response = self.session.request(method, url, headers=self._headers() | kwargs.pop("headers", {}),
                                            timeout=(10, 45), allow_redirects=False, **kwargs)
        except StorageError:
            raise
        except Exception:
            raise StorageError("Не удалось связаться с Google Drive. Повторите действие.") from None
        try:
            yield response
        finally:
            response.close()

    def allocate(self):
        with self.request("GET", self.API + "/generateIds", params={"count": 1, "space": "drive", "type": "files"}) as response:
            if response.status_code != 200:
                raise StorageError("Drive не выдал идентификатор для загрузки.")
            try:
                return valid_id(response.json()["ids"][0])
            except (KeyError, IndexError, ValueError, TypeError):
                raise StorageError("Некорректный ответ Google Drive.") from None

    def metadata(self, file_id):
        with self.request("GET", self.API + "/" + valid_id(file_id), params={"fields": "id,name,mimeType,trashed,size"}) as response:
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise StorageError("Не удалось прочитать сведения о файле Drive.")
            return response.json()

    def ensure_folder(self, file_id):
        existing = self.metadata(file_id)
        if existing:
            if existing.get("trashed") or existing.get("mimeType") != "application/vnd.google-apps.folder":
                raise StorageError("Папка приложения удалена или недоступна.")
            return
        with self.request("POST", self.API, params={"fields": "id"}, json={
            "id": file_id, "name": "SKB Competitor Analytics", "mimeType": "application/vnd.google-apps.folder",
            "appProperties": {"purpose": "skb-competitor-analytics"}}) as response:
            if response.status_code != 200 or response.json().get("id") != file_id:
                raise StorageError("Не удалось создать закрытую папку приложения.")

    def download(self, file_id, expected_sha, maximum=None):
        maximum = maximum or self.MAX_PACK
        with self.request("GET", self.API + "/" + valid_id(file_id), params={"alt": "media"}, stream=True) as response:
            if response.status_code != 200:
                raise StorageError("Файл в Google Drive недоступен.")
            content = bytearray()
            try:
                for part in response.iter_content(64 * 1024):
                    content.extend(part)
                    if len(content) > maximum:
                        raise StorageError("Размер файла превышает допустимый.")
            except StorageError:
                raise
            except Exception:
                raise StorageError("Загрузка файла прервана. Повторите действие.") from None
        if hashlib.sha256(content).hexdigest() != expected_sha:
            raise StorageError("Контрольная сумма файла не совпадает. Файл не выдан.")
        return bytes(content)

    def ensure_pack(self, file_id, folder_id, content, digest):
        if len(content) > self.MAX_PACK or hashlib.sha256(content).hexdigest() != digest:
            raise StorageError("Некорректный пакет загрузки.")
        if self.metadata(file_id) is None:
            boundary = "skb_" + uuid4().hex
            metadata = json.dumps({"id": valid_id(file_id), "parents": [valid_id(folder_id)],
                "name": digest + ".zip", "mimeType": "application/zip", "appProperties": {"sha256": digest}}).encode()
            body = (f"--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode() + metadata
                    + f"\r\n--{boundary}\r\nContent-Type: application/zip\r\n\r\n".encode()
                    + content + f"\r\n--{boundary}--\r\n".encode())
            with self.request("POST", "https://www.googleapis.com/upload/drive/v3/files", params={"uploadType": "multipart", "fields": "id"},
                              data=body, headers={"Content-Type": "multipart/related; boundary=" + boundary}) as response:
                if response.status_code != 200 or response.json().get("id") != file_id:
                    raise StorageError("Загрузка пакета не подтверждена. Повторный запуск продолжит перенос.")
        # Verify persisted bytes even when resuming an ambiguous upload.
        self.download(file_id, digest)
