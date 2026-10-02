"""Local entry point: prices always use SQLite, irrespective of cloud secrets."""
import os
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent
os.environ.setdefault('PRICE_MONITOR_DB', str(ROOT / 'data/local/prices.sqlite3'))

runpy.run_path(str(ROOT / 'streamlit_app.py'), run_name='__main__',
               init_globals={'PRICE_CLOUD_MODE': False})
