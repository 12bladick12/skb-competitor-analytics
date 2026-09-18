"""Read-only connectivity checks. Reports deliberately exclude credentials/errors."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
import tomllib
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass(frozen=True)
class Check:
    component: str
    status: str
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def load_secrets(path):
    # TOMLDecodeError can contain a fragment of the secret. Never surface it.
    try:
        with Path(path).open(encoding="utf-8-sig") as source:
            return tomllib.loads(source.read())
    except FileNotFoundError:
        raise ValueError("Файл настроек ещё не создан; используйте secrets.example.toml.") from None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("Не удалось прочитать настройки. Проверьте формат TOML и права файла.") from None


def section(config, key):
    value = config.get(key, {})
    return value if isinstance(value, Mapping) else {}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def neon_parameters(url):
    """Accept the Neon URL form only; do not forward arbitrary libpq options."""
    try:
        parsed = urlsplit(_text(url))
        host = parsed.hostname or ""
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or not host.endswith(".neon.tech")
            or not parsed.username or not parsed.password
            or not parsed.path.strip("/")
            or parsed.port not in {None, 5432}
            or parsed.fragment
            or set(query) - {"sslmode", "channel_binding"}
            or query.get("sslmode", ["require"]) not in [["require"], ["verify-full"]]
            or query.get("channel_binding", ["require"]) not in [["require"], ["prefer"]]
        ):
            raise ValueError
        return {
            "host": host, "port": 5432,
            "user": unquote(parsed.username), "password": unquote(parsed.password),
            "dbname": unquote(parsed.path[1:]),
            "sslmode": query.get("sslmode", ["require"])[0],
            "channel_binding": query.get("channel_binding", ["require"])[0],
            "connect_timeout": 10,
            "autocommit": True,
        }
    except (ValueError, TypeError):
        raise ValueError("Нужна строка PostgreSQL из Neon Connect с TLS; проверьте database_url.") from None


def check_neon(config, *, offline=False, connect=None):
    url = section(config, "cloud").get("database_url")
    if not _text(url):
        return Check("neon", "pending", "Заполните cloud.database_url из Neon Connect.")
    try:
        params = neon_parameters(url)
    except ValueError as exc:
        return Check("neon", "error", str(exc))
    if offline:
        return Check("neon", "unchecked", "Формат подключения корректен; связь с Neon не проверялась.")
    if connect is None:
        try:
            import psycopg
        except ImportError:
            return Check("neon", "pending", "Установите requirements-cloud-check.txt для проверки PostgreSQL.")
        connect = psycopg.connect
    try:
        with connect(**params) as connection:
            # Neon pooled connections reject statement_timeout in startup options.
            # Apply it inside a read-only transaction so it cannot leak to the
            # next client borrowing the same pooled server connection.
            connection.read_only = True
            with connection.transaction():
                connection.execute("SET LOCAL statement_timeout = '10s'")
                row = connection.execute("SELECT 1").fetchone()
            if row is None or row[0] != 1:
                return Check("neon", "error", "Neon не вернул ожидаемый ответ проверки.")
    except Exception:
        # Driver exceptions may repeat connection strings or password fragments.
        return Check("neon", "error", "Подключение Neon не удалось. Проверьте строку, сеть и состояние проекта.")
    return Check("neon", "ok", "Neon отвечает; выполнен только SELECT 1. Таблицы не изменялись.")


def quota_result(payload, *, minimum_free_bytes=2_000_000_000):
    quota = payload.get("storageQuota") if isinstance(payload, Mapping) else None
    if not isinstance(quota, Mapping) or "usage" not in quota:
        return Check("drive", "error", "Drive не вернул сведения об использовании хранилища.")
    try:
        usage = int(quota["usage"])
        limit = int(quota["limit"]) if "limit" in quota else None
        if usage < 0 or (limit is not None and limit < 0):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        return Check("drive", "error", "Drive вернул некорректный размер хранилища.")
    if limit is None:
        return Check("drive", "warning", "Drive доступен, но квота не указана; проверьте место в аккаунте.", {"used_bytes": usage})
    free = max(0, limit - usage)
    details = {"used_bytes": usage, "limit_bytes": limit, "free_bytes": free}
    if free < minimum_free_bytes:
        return Check("drive", "warning", "Свободно менее рекомендованных 2 ГБ для переноса и запаса.", details)
    return Check("drive", "ok", "Drive доступен; свободного места достаточно для первоначального переноса.", details)


def check_drive(config, *, offline=False, session=None):
    settings = section(config, "drive")
    fields = ("client_id", "client_secret", "refresh_token")
    missing = [key for key in fields if not _text(settings.get(key))]
    if missing:
        names = ", ".join(missing)
        return Check(
            "drive", "pending",
            f"В разделе [drive] не заполнены поля: {names}. "
            "Если подключение уже выполнено на компьютере, перенесите обновлённый "
            "secrets.toml в Streamlit → Settings → Secrets и сохраните изменения.",
            {"missing_fields": missing},
        )
    if offline:
        return Check("drive", "unchecked", "Параметры Drive заданы; доступ и свободное место не проверялись.")
    owns_session = session is None
    if owns_session:
        import requests
        session = requests.Session()
    try:
        # Explicit endpoints; no redirect may forward a token to another host.
        response = session.post(
            "https://oauth2.googleapis.com/token",
            data={key: settings[key] for key in fields} | {"grant_type": "refresh_token"},
            timeout=(5, 15), allow_redirects=False,
        )
        if response.status_code != 200:
            return Check("drive", "error", "Google не подтвердил доступ. Проверьте OAuth; при отзыве подключите аккаунт повторно.")
        token_payload = response.json()
        token = token_payload.get("access_token") if isinstance(token_payload, Mapping) else None
        if not isinstance(token, str) or not token:
            return Check("drive", "error", "Google не выдал токен доступа к Drive.")
        response = session.get(
            "https://www.googleapis.com/drive/v3/about",
            params={"fields": "storageQuota"}, headers={"Authorization": "Bearer " + token},
            timeout=(5, 15), allow_redirects=False,
        )
        if response.status_code != 200:
            return Check("drive", "error", "Не удалось прочитать квоту Drive. Проверьте включение Drive API и разрешение drive.file.")
        return quota_result(response.json())
    except Exception:
        return Check("drive", "error", "Проверка Drive не завершилась. Проверьте сеть и повторите попытку.")
    finally:
        if owns_session:
            session.close()


def check_login_config(config):
    settings = section(config, "auth")
    required = ("redirect_uri", "cookie_secret", "client_id", "client_secret")
    if any(not _text(settings.get(key)) for key in required):
        return Check("login", "pending", "Нужны настройки Google-входа и адрес нового приложения.")
    try:
        target = urlsplit(settings["redirect_uri"])
        valid_url = (
            target.scheme == "https" and (target.hostname or "").endswith(".streamlit.app")
            and target.port is None and not target.username and not target.password
            and target.path == "/oauth2callback" and not target.query and not target.fragment
        )
    except ValueError:
        valid_url = False
    if not valid_url or len(settings["cookie_secret"]) < 32:
        return Check("login", "error", "Проверьте HTTPS-адрес /oauth2callback и случайный cookie_secret длиной от 32 символов.")
    if settings.get("server_metadata_url") != "https://accounts.google.com/.well-known/openid-configuration":
        return Check("login", "error", "Для выбранной схемы входа требуется Google OIDC.")
    return Check("login", "unchecked", "Настройки входа заполнены; вход и ограничения доступа ещё нужно проверить в браузере.")


def check_all(config, *, offline=False):
    return [check_neon(config, offline=offline), check_drive(config, offline=offline), check_login_config(config)]
