"""Independent download/recognition worker; only the latter needs a local Ollama."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
import tomllib
import uuid
from pathlib import Path

from .models import utcnow
from .passports import Passports
from .passport_processing import download_job
from .passport_recognition import LocalRecognizer, VERSION, validate
from .passport_transport import LocalFiles, SupabaseFiles

log=logging.getLogger('price_monitor.passports')


class PassportWorker:
    def __init__(self, repository, files, kind='download', recognizer=None):
        if kind not in ('download','recognize'):raise ValueError('Unknown worker queue')
        self.passports=Passports(repository);self.files=files;self.kind=kind
        self.recognizer=recognizer;self.owner='passports1-'+kind+'-'+str(uuid.uuid4())
        self.shutdown=threading.Event()

    def work(self, job):
        if self.kind=='download':
            download_job(self.passports,self.files,job,self.owner)
            return
        payload=json.loads(job['payload_json']);fp=payload['fingerprint']
        raw=self.files.get(fp)
        result=self.recognizer(raw)
        pages=self.passports.repo.batch('SELECT page_count FROM passport_files WHERE fingerprint=%(fp)s',{'fp':fp})
        result=validate(result,pages[0]['page_count'])
        job['_metrics']={'requests':0,'bytes':0,'pages':pages[0]['page_count']}
        self.passports.repo.batch('''INSERT INTO passport_geometry(fingerprint,version,result_json,updated_at)
            SELECT %(fp)s,%(version)s,%(result)s,%(now)s WHERE '''+self.passports.guard()+'''
            ON CONFLICT(fingerprint,version) DO UPDATE SET result_json=excluded.result_json,
            state='review',updated_at=excluded.updated_at''',
            {'fp':fp,'version':VERSION,'result':json.dumps(result,ensure_ascii=False),'now':utcnow(),
             'job':job['id'],'owner':self.owner,'time':int(time.time())})
        # Existing reviewed values remain tied to the same file and their reviewer;
        # a repeat proposal cannot silently overwrite them.
        self.passports.finish(job['id'],self.owner)

    def run(self, once=False, limit=None):
        while not self.shutdown.is_set():
            try:
                self._run(once,limit)
                return
            except Exception as exc:
                log.warning('Passport queue connection interrupted: %s',type(exc).__name__)
                if once:raise
                self.shutdown.wait(15)

    def _run(self, once=False, limit=None):
        count=0
        while not self.shutdown.is_set():
            if self.kind=='download':self.passports.schedule(limit=200)
            self.passports.heartbeat(self.owner,'idle')
            job=self.passports.claim(self.kind,self.owner)
            if job:
                done=threading.Event()
                def heartbeat():
                    while not done.wait(20):
                        try:
                            if not self.passports.renew(job['id'],self.owner):self.shutdown.set();return
                            self.passports.heartbeat(self.owner,'processing',job['id'])
                        except Exception:
                            log.warning('Passport lease heartbeat failed');self.shutdown.set();return
                thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
                started=time.monotonic()
                try:self.work(job)
                except Exception as exc:
                    # Never include secret-bearing requests or connection strings.
                    detail=type(exc).__name__
                    self.passports.finish(job['id'],self.owner,'retry',detail,delay=min(86400,300*2**min(job['attempts'],8)))
                    log.warning('%s task failed: %s',self.kind,detail)
                finally:
                    done.set();thread.join(timeout=2)
                    metrics=job.get('_metrics',{'requests':0,'bytes':0,'pages':0})
                    try:
                        self.passports.repo.batch('''INSERT INTO passport_job_metrics(job_id,attempt,requests,received_bytes,pages,seconds,created_at)
                            VALUES(%(id)s,%(attempt)s,%(requests)s,%(bytes)s,%(pages)s,%(seconds)s,%(now)s)
                            ON CONFLICT(job_id,attempt) DO NOTHING''',
                            {**metrics,'id':job['id'],'attempt':job['attempts'],'seconds':time.monotonic()-started,'now':utcnow()})
                    except Exception:log.warning('Document metrics could not be persisted')
                count+=1
                log.info('%s job=%s seconds=%.1f',self.kind,job['id'],time.monotonic()-started)
            if once or (limit and count>=limit):break
            if not job:self.shutdown.wait(15)
        self.passports.heartbeat(self.owner,'stopped')


class EmbeddedDownloads:
    def __init__(self, repository, settings):
        self.files=SupabaseFiles(settings);self.files.ensure()
        self.worker=PassportWorker(repository,self.files)
        self.thread=threading.Thread(target=self.worker.run,name='passport-downloads',daemon=True)
        self.thread.start()

    def close(self):
        self.worker.shutdown.set();self.thread.join(timeout=3)


def main():
    parser=argparse.ArgumentParser(description='Паспорта: независимая очередь загрузки или локального распознавания')
    parser.add_argument('--settings',default='.streamlit/secrets.toml')
    parser.add_argument('--db',help='Локальная SQLite для испытаний')
    parser.add_argument('--queue',choices=['download','recognize'],default='recognize')
    parser.add_argument('--once',action='store_true');parser.add_argument('--limit',type=int)
    parser.add_argument('--offline-files',help='Локальные файлы: только испытания без общей публикации')
    args=parser.parse_args()
    config=tomllib.loads(Path(args.settings).read_text(encoding='utf-8-sig')) if Path(args.settings).exists() else {}
    from .storage import Store
    if args.db:repository=Store(args.db).catalog
    else:
        # Cloud deployment owns additive schema migration. Local workers must
        # not repeat DDL and hold catalog locks each time the PC reconnects.
        from .catalog_storage import CatalogRepository
        repository=CatalogRepository(settings={**config['database'],'reuse_connections':False})
        repository.batch('SELECT count(*) FROM passport_jobs')
    cache=LocalFiles(Path(__file__).resolve().parents[1]/'data'/'passports')
    if args.offline_files:files=LocalFiles(args.offline_files)
    else:
        files=SupabaseFiles(config.get('passports',{}),cache);files.ensure()
    worker=PassportWorker(repository,files,args.queue)
    if args.queue=='recognize':worker.recognizer=LocalRecognizer(cancelled=worker.shutdown.is_set)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:worker.shutdown.set())
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    worker.run(args.once,args.limit)


if __name__=='__main__':main()
