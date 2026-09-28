"""Create a source-only archive suitable for a separate GitHub repository."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "dist" / "price-monitor-streamlit.zip"


def package():
    files = [ROOT/name for name in ("streamlit_app.py", "app.py", "requirements.txt", "README.md", ".gitignore", "run.ps1", "run_sensoren.cmd", "Dockerfile", ".dockerignore", "compose.yaml", ".streamlit/config.toml", ".streamlit/secrets.example.toml")]
    files += sorted((ROOT/"price_monitor").glob("*.py"))
    files += sorted((ROOT/"examples").glob("*.csv"))
    files += sorted((ROOT/"examples").glob("*.xlsx"))
    files += sorted((ROOT/"docs").glob("*.md"))
    files += sorted((ROOT/"docs"/"audit").glob("*.json"))
    files += sorted((ROOT/"tools").glob("*.py"))
    files += sorted((ROOT/"tests").glob("*.py"))
    files += sorted(p for p in (ROOT/"tests"/"fixtures").iterdir() if p.is_file())
    DEST.parent.mkdir(exist_ok=True)
    with ZipFile(DEST,"w",ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path,path.relative_to(ROOT).as_posix())
    print(f"Created {DEST.name}: {len(files)} files")


if __name__ == "__main__":
    package()
