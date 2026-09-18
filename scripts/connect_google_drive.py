"""Run locally to authorize the Drive owner; credentials are never printed."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud.drive_auth import (
    DriveAuthError, authorize_owner, read_desktop_client, read_settings_snapshot, save_drive_settings,
)
from cloud.readiness import check_drive, load_secrets


def main(argv=None):
    parser = argparse.ArgumentParser(description="Одноразовое подключение Google Drive владельцем")
    parser.add_argument("--credentials", type=Path, default=ROOT / "credentials-drive.json")
    parser.add_argument("--secrets", type=Path, default=ROOT / ".streamlit/secrets.toml")
    parser.add_argument("--check", action="store_true", help="Проверить файлы, не открывая браузер")
    args = parser.parse_args(argv)
    try:
        client = read_desktop_client(args.credentials)
        original, _config = read_settings_snapshot(args.secrets)
        if args.check:
            print("Desktop app и TOML готовы. Запустите без --check для подключения Drive.")
            return 0
        print("Откроется Google. Выберите аккаунт владельца хранилища и подтвердите доступ.", flush=True)
        print("Ожидание — до пяти минут. Оставьте это окно открытым.", flush=True)
        credentials = authorize_owner(client)
        save_drive_settings(args.secrets, credentials, original)
        print("Параметры Drive сохранены в локальном secrets.toml. Настройки входа и базы сохранены.")
        result = check_drive(load_secrets(args.secrets))
        print(result.message)
        if "free_bytes" in result.details:
            print(f"Свободно в Google Drive: {result.details['free_bytes'] / 1e9:.2f} ГБ")
        print("Скопируйте весь secrets.toml в Streamlit → Настройки → Секреты и сохраните.")
        print("Затем нажмите в приложении «Проверить Neon и Google Drive».")
        return 0 if result.status == "ok" else 1
    except DriveAuthError as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("Подключение отменено.")
        return 1
    except Exception:
        print("Подключение не завершено. Повторите запуск; секреты в журнал не выводятся.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
