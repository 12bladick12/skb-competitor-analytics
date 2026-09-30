"""Explicit model download; no document is sent to the model registry."""
import json
import time
import requests

session=requests.Session();session.trust_env=False
for attempt in range(20):
    try:
        session.get('http://127.0.0.1:11434/api/tags',timeout=2).raise_for_status();break
    except requests.RequestException:time.sleep(1)
last=0
with session.post('http://127.0.0.1:11434/api/pull',json={'model':'qwen3-vl:4b','stream':True},stream=True,timeout=(5,120)) as response:
    response.raise_for_status()
    for line in response.iter_lines():
        if not line:continue
        info=json.loads(line)
        if info.get('error'):raise RuntimeError(info['error'])
        if time.monotonic()-last>20 or info.get('status')=='success':
            print(json.dumps({k:info[k] for k in ('status','completed','total') if k in info}),flush=True);last=time.monotonic()
