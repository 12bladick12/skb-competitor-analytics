"""Download pinned official native runtimes into an ignored workspace directory."""
import hashlib
import json
from pathlib import Path
import time
import zipfile
import requests

ROOT=Path(__file__).resolve().parents[1]/'data'/'passport_runtime'
OLLAMA='https://github.com/ollama/ollama/releases/download/v0.35.0/ollama-windows-amd64.zip'
OLLAMA_SHA='d6f7d3dd4f5d013553a78c1e78b2521fcf41d43dd2863e4596cdc046fe6036db'
TESSERACT='https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe'


def download(url,target,checksum=''):
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists() and (not checksum or hashlib.file_digest(target.open('rb'),'sha256').hexdigest()==checksum):return
    part=target.with_suffix(target.suffix+'.part')
    size=part.stat().st_size if part.exists() else 0
    headers={'Range':f'bytes={size}-'} if size else {}
    # Fresh redirect avoids a cached, expired signed release URL.
    with requests.get(url+'?download='+str(int(time.time())),headers=headers,stream=True,timeout=(15,60)) as r:
        r.raise_for_status()
        append=size>0 and r.status_code==206
        if not append:size=0
        stamp=time.monotonic()
        with part.open('ab' if append else 'wb') as stream:
            for chunk in r.iter_content(1024*1024):
                stream.write(chunk);size+=len(chunk)
                if time.monotonic()-stamp>20:print(target.name,size//1048576,'MiB',flush=True);stamp=time.monotonic()
    if checksum:
        with part.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=checksum:raise RuntimeError('Runtime checksum mismatch')
    part.replace(target)


if __name__=='__main__':
    ROOT.mkdir(parents=True,exist_ok=True)
    download(TESSERACT,ROOT/'tesseract-setup.exe')
    download(OLLAMA,ROOT/'ollama.zip',OLLAMA_SHA)
    target=(ROOT/'ollama').resolve();target.mkdir(exist_ok=True)
    with zipfile.ZipFile(ROOT/'ollama.zip') as archive:
        # CPU deployment: omit CUDA/Vulkan payloads, retain CPU runtime libraries.
        for member in archive.infolist():
            if any(x in member.filename.lower() for x in ('cuda','vulkan','rocm')):continue
            destination=(target/member.filename).resolve()
            if not destination.is_relative_to(target):raise ValueError('Invalid archive path')
            archive.extract(member,target)
    print('Official runtime packages prepared',flush=True)
