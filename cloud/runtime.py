"""Start a worker independently of the browser session. No runtime secrets on disk."""
import json
from pathlib import Path
import subprocess
import sys
import threading

import streamlit as st


class Process:
    def __init__(self):
        self.process=None
        self.lock=threading.Lock()

    def ensure(self,config):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            options={}
            if sys.platform=='win32':
                options['creationflags']=subprocess.CREATE_NO_WINDOW
            self.process=subprocess.Popen([sys.executable,'-m','cloud.worker'],cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
            self.process.stdin.write(json.dumps(config).encode())
            self.process.stdin.close()


@st.cache_resource
def manager():
    return Process()


def ensure_worker(config):
    # Plain serializable settings only; never pass the OIDC browser identity.
    manager().ensure({key:dict(config.get(key,{})) for key in ('cloud','drive')})
