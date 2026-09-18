"""Owner-only local check; never serve this diagnostics script as a public page."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud.readiness import check_all, load_secrets


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверка Neon и места Google Drive без изменения данных")
    parser.add_argument("--secrets", type=Path, default=ROOT / ".streamlit/secrets.toml")
    parser.add_argument("--offline", action="store_true", help="Проверить настройки без обращения к сервисам")
    args = parser.parse_args(argv)
    try:
        config = load_secrets(args.secrets)
    except ValueError as exc:
        print(json.dumps({"status": "pending", "message": str(exc)}, ensure_ascii=False))
        return 1
    results = check_all(config, offline=args.offline)
    print(json.dumps({
        "checks": [item.to_dict() for item in results],
        "note": "Это проверка подключений. Приложение ещё не опубликовано; запись и миграции не проверялись.",
    }, ensure_ascii=False, indent=2))
    return 1 if any(item.status in {"pending", "error", "warning"} for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
