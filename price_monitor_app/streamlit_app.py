"""Select this file as the Main file path in Streamlit Community Cloud."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name("app.py")), init_globals={"CLOUD_MODE": True})
