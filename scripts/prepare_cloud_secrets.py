"""Create a local ignored settings file without printing generated credentials."""

from pathlib import Path
import secrets
import sys


def prepare(root):
    root = Path(root)
    target = root / ".streamlit/secrets.toml"
    template = root / ".streamlit/secrets.example.toml"
    content = template.read_text(encoding="utf-8-sig")
    content = content.replace('cookie_secret = ""', 'cookie_secret = "' + secrets.token_urlsafe(48) + '"', 1)
    try:
        with target.open("x", encoding="utf-8") as destination:
            destination.write(content)
    except FileExistsError:
        return False
    return True


if __name__ == "__main__":
    created = prepare(Path(__file__).resolve().parents[1])
    print("Файл .streamlit/secrets.toml подготовлен; секрет в вывод не включён." if created else
          "Файл .streamlit/secrets.toml уже существует и оставлен без изменений.")
    sys.exit(0)
