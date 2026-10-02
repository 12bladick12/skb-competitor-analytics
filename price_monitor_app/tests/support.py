"""Workspace-only temporary files; compatible with Windows restricted tokens."""
from pathlib import Path
import shutil
import uuid


class TestDirectory:
    def __init__(self):
        self.root = Path(__file__).resolve().parents[1]/"data"
        self.path = self.root/("test_"+uuid.uuid4().hex)
        self.path.mkdir(parents=True)
        self.name = str(self.path)

    def cleanup(self):
        assert self.path.resolve().parent == self.root.resolve()
        assert self.path.name.startswith("test_")
        shutil.rmtree(self.path)

    def __enter__(self):
        return self.name

    def __exit__(self,*args):
        self.cleanup()
