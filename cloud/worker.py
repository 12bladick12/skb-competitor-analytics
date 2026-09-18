"""One disposable process; durable queue, leases and artifacts live in Neon/Drive."""
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4

from .artifacts import upload_objects
from .draft_rules import snapshot
from .drive_store import DriveStore, StorageError
from .jobs import JobStore, LeaseLost
from .library import read_asset


class CachedDrive:
    def __init__(self, drive):
        self.drive=drive
        self.cache=OrderedDict()

    def download(self, file_id, digest):
        if digest not in self.cache:
            self.cache[digest]=self.drive.download(file_id,digest)
            while sum(map(len,self.cache.values()))>48*1024*1024 and len(self.cache)>1:
                self.cache.popitem(last=False)
        self.cache.move_to_end(digest)
        return self.cache[digest]


def execute(store, drive, job, root, alive=lambda: None):
    library=store.repository.load()
    if not library or library['id']!=job['import_id']:
        raise ValueError('Набор данных изменился.')
    cached=CachedDrive(drive)
    def read(asset_id):
        alive()
        return read_asset(store.repository,cached,job['import_id'],asset_id)
    def progress(stage,source='',completed=None,run_id=None):
        alive()
        store.pulse(job,stage,source,completed)
    if job['kind']=='export':
        from .exports import render
        value=job['payload']
        frozen={'id':value['draft_id'],**value}
        # Recheck the source versions while retaining the saved snapshot and coverage.
        snapshot(frozen,library,store.asset_ids(job['import_id']))
        progress('Проверка сохранённых доказательств')
        ids={i for item in value['items'] for i in item['source']['evidence_ids']}
        evidence={i:read(i) for i in ids}
        progress('Формирование Word и ZIP')
        word,bundle=render(value,evidence,library['competitor'])
        word_id,bundle_id=(hashlib.sha256(v).hexdigest() for v in (word,bundle))
        assets=upload_objects(store,drive,job,{word_id:word,bundle_id:bundle})
        report_id=job['id']
        report={'id':report_id,'name':'Записка '+job['period']+' · редакция '+str(value['revision']),
            'legacy':False,'period':job['period'],'revision':value['revision'],'draft_id':value['draft_id'],
            'created_at':datetime.now(timezone.utc).isoformat(),'docx_id':word_id,'bundle_id':bundle_id,'snapshot':value}
        alive()
        store.publish(job,[{'kind':'report','key':report_id,'payload':report}],assets,result_id=report_id)
    else:
        from .collection import collect
        progress('Подготовка сохранённых материалов')
        baselines=[r['payload'] for r in store.query("SELECT payload FROM skb_analytics.records WHERE import_id=$1 AND kind='collector_baseline'",[job['import_id']])]
        records,objects,result=collect(library,job['period'],root,read,progress,baselines)
        assets=upload_objects(store,drive,job,objects)
        alive()
        store.publish(job,records,assets,status='partial' if result['status']=='partial' else 'success',result_id=str(result['run_id']))


def run(config):
    store=JobStore(config)
    parent=os.getppid()
    token=uuid4().hex
    idle=0
    while os.getppid()==parent and idle<6:
        try:
            job=store.claim(token)
        except StorageError:
            return
        if not job:
            idle+=1
            time.sleep(5)
            continue
        idle=0
        stop=threading.Event()
        lost=threading.Event()
        started=time.monotonic()
        def heartbeat():
            while not stop.wait(25):
                try:
                    if os.getppid()!=parent or time.monotonic()-started>3600:
                        lost.set()
                        return
                    store.pulse(job)
                except Exception:
                    lost.set()
                    return
        def alive():
            if lost.is_set():
                raise LeaseLost('Задание прервано: связь с очередью потеряна или превышено время выполнения.')
        pulse=threading.Thread(target=heartbeat,daemon=True)
        pulse.start()
        drive=DriveStore(config)
        try:
            with tempfile.TemporaryDirectory(prefix='skb-job-') as folder:
                execute(store,drive,job,Path(folder),alive)
        except Exception as exc:
            # Never persist exception strings containing request credentials/DSNs.
            message=(str(exc) if isinstance(exc,(ValueError,StorageError)) else
                     'Не удалось завершить обработку. Проверьте подключение и повторите запуск.')
            try:
                store.fail(job,message[:500],'interrupted' if isinstance(exc,LeaseLost) else 'error')
            except Exception:
                pass  # Expiry records an interrupted job; no implicit retry.
        finally:
            stop.set()
            pulse.join(timeout=2)
            drive.close()


if __name__=='__main__':
    run(json.loads(sys.stdin.read()))
