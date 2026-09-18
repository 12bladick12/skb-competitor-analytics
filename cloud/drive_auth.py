"""One-time owner authorization on the local PC, never a public web endpoint."""

import base64
from collections.abc import Mapping
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import tomllib
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import requests


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class DriveAuthError(ValueError):
    """Only fixed, credential-free messages may reach the console."""


def read_desktop_client(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise DriveAuthError("Не удалось прочитать credentials-drive.json. Скачайте JSON клиента Google.") from None
    if not isinstance(data, Mapping):
        raise DriveAuthError("Ожидается JSON OAuth-клиента Google.")
    if "web" in data:
        raise DriveAuthError("В файле клиент Web application. Создайте отдельный Desktop app для Drive и скачайте его JSON.")
    client = data.get("installed")
    if not isinstance(client, Mapping):
        raise DriveAuthError("В файле нет клиента Desktop app (раздел installed).")
    client_id, client_secret = client.get("client_id"), client.get("client_secret")
    if (not isinstance(client_id, str)
            or not re.fullmatch(r"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id)
            or not isinstance(client_secret, str) or not client_secret.strip()
            or client.get("auth_uri") not in {AUTH_URL, "https://accounts.google.com/o/oauth2/auth"}
            or client.get("token_uri") != TOKEN_URL):
        raise DriveAuthError("Параметры OAuth-клиента некорректны. Скачайте исходный JSON из Google Cloud.")
    return {"client_id": client_id, "client_secret": client_secret}


def authorization_url(client, redirect_uri, state, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return AUTH_URL + "?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect_uri,
        "response_type": "code", "scope": DRIVE_SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent select_account",
    })


class _Callback(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, *_args):
        # The query string includes a one-time code; never log requests.
        pass

    def reply(self, status, message):
        content = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(content)
        except OSError:
            pass

    def do_GET(self):
        target = urlsplit(self.path)
        if (self.headers.get("Host") != self.server.expected_host
                or target.path != "/oauth2callback" or target.scheme or target.netloc):
            self.reply(404, "Страница не найдена.")
            return
        try:
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=20)
            states = query.get("state", [])
            valid_state = len(states) == 1 and secrets.compare_digest(states[0], self.server.state)
        except (ValueError, TypeError):
            valid_state = False
        if not valid_state:
            self.reply(400, "Ответ не относится к текущему подключению. Вернитесь в окно Google.")
            return
        if query.get("error"):
            self.server.result = {"denied": True}
            self.reply(200, "Разрешение не получено. Можно закрыть эту вкладку.")
            return
        codes = query.get("code", [])
        if len(codes) != 1 or not codes[0] or len(codes[0]) > 4096:
            self.reply(400, "Google не передал корректный код подключения.")
            return
        self.server.result = {"code": codes[0]}
        self.reply(200, "Ответ Google получен. Вернитесь в окно запуска: там появится результат сохранения.")


def exchange_code(client, code, redirect_uri, verifier, *, session):
    try:
        response = session.post(TOKEN_URL, data={
            **client, "code": code, "redirect_uri": redirect_uri,
            "code_verifier": verifier, "grant_type": "authorization_code",
        }, timeout=(5, 20), allow_redirects=False)
        if response.status_code != 200:
            raise DriveAuthError("Google не завершил подключение. Проверьте Desktop app и повторите запуск.")
        payload = response.json()
        refresh = payload.get("refresh_token") if isinstance(payload, Mapping) else None
        if not isinstance(refresh, str) or not refresh:
            raise DriveAuthError("Google не выдал refresh token. Повторите запуск и подтвердите доступ к Drive.")
        granted = payload.get("scope", DRIVE_SCOPE)
        if not isinstance(granted, str) or DRIVE_SCOPE not in granted.split():
            raise DriveAuthError("Разрешение drive.file не предоставлено. Настройки не изменены.")
        return {**client, "refresh_token": refresh}
    except DriveAuthError:
        raise
    except Exception:
        raise DriveAuthError("Не удалось обменять ответ Google на доступ. Проверьте сеть и повторите запуск.") from None


def authorize_owner(client, *, timeout=300, open_browser=None):
    open_browser = open_browser or webbrowser.open
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    with HTTPServer(("127.0.0.1", 0), _Callback) as server:
        server.timeout = 1
        server.state = state
        server.result = None
        server.expected_host = f"127.0.0.1:{server.server_port}"
        redirect_uri = f"http://{server.expected_host}/oauth2callback"
        url = authorization_url(client, redirect_uri, state, verifier)
        if not open_browser(url):
            raise DriveAuthError("Не удалось открыть браузер. Назначьте браузер по умолчанию и повторите запуск.")
        deadline = time.monotonic() + timeout
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
        if server.result is None:
            raise DriveAuthError("Время ожидания истекло. Запустите подключение снова.")
        if server.result.get("denied"):
            raise DriveAuthError("Доступ к Drive не предоставлен. Настройки не изменены.")
        code = server.result["code"]
    with requests.Session() as session:
        return exchange_code(client, code, redirect_uri, verifier, session=session)


def read_settings_snapshot(path):
    try:
        original = Path(path).read_bytes()
        config = tomllib.loads(original.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise DriveAuthError("Не удалось прочитать secrets.toml. Проверьте TOML перед подключением Drive.") from None
    return original, config


def save_drive_settings(path, credentials, original):
    """Replace only Drive values; preserve login/DB settings and detect edits."""
    path = Path(path)
    if set(credentials) != {"client_id", "client_secret", "refresh_token"} or any(
            not isinstance(value, str) or not value for value in credentials.values()):
        raise DriveAuthError("Получены неполные параметры Drive. Настройки не изменены.")
    try:
        content = original.decode("utf-8-sig")
        before = tomllib.loads(content)
        if not isinstance(before.get("drive", {}), dict):
            raise ValueError
        newline = "\r\n" if "\r\n" in content else "\n"
        header = re.search(r"(?m)^\[drive\][ \t]*(?:#[^\r\n]*)?\r?$", content)
        if header:
            following = re.search(r"(?m)^\[", content[header.end():])
            end = header.end() + following.start() if following else len(content)
            block = content[header.end():end]
            for key, value in credentials.items():
                assignment = key + " = " + json.dumps(value, ensure_ascii=False)
                pattern = r"(?m)^[ \t]*" + key + r"[ \t]*=[^\r\n]*"
                if re.search(pattern, block):
                    block = re.sub(pattern, lambda _match: assignment, block)
                else:
                    block = block.rstrip("\r\n") + newline + assignment + newline
            result = content[:header.end()] + block + content[end:]
        else:
            result = content.rstrip("\r\n") + newline * 2 + "[drive]" + newline
            result += "".join(k + " = " + json.dumps(v, ensure_ascii=False) + newline for k, v in credentials.items())
        expected = {**before, "drive": {**before.get("drive", {}), **credentials}}
        if tomllib.loads(result) != expected:
            raise ValueError
    except (UnicodeError, ValueError):
        raise DriveAuthError("Не удалось обновить раздел drive. Настройки не изменены.") from None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", prefix="secrets.oauth-",
                                         suffix=".toml", dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(result)
            target.flush()
            os.fsync(target.fileno())
        if path.read_bytes() != original:
            raise DriveAuthError("secrets.toml изменился во время подключения. Повторите запуск; правки сохранены.")
        os.replace(temporary, path)
    except OSError:
        raise DriveAuthError("Не удалось сохранить параметры Drive. Проверьте доступ к secrets.toml.") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
