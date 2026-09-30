from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import logging
import signal
import threading
import uuid
from pathlib import Path
import traceback
import time

from .adapters import ADAPTERS
from .models import Observation
from .storage import Store
from .transport import FetchError, SourceClient

log = logging.getLogger("price_monitor")


class Worker:
    def __init__(self, store: Store, client_factory=SourceClient):
        self.store, self.client_factory = store, client_factory
        self.owner = "router2-monthly1-sites4-" + str(uuid.uuid4())
        self.shutdown = threading.Event()
        self.progress = {}
        self.progress_lock = threading.Lock()

    def heartbeat_loop(self, finished):
        while not finished.wait(10):
            try:
                if not self.store.heartbeat(self.owner):
                    log.error("Worker lease lost")
                    self.shutdown.set()
                    return
                with self.progress_lock:progress=list(self.progress.items())
                for source,tracker in progress:
                    try:self.store.catalog.report_health(source,self.owner,tracker.snapshot())
                    except Exception as exc:log.warning('Source progress update failed: %s',type(exc).__name__)
            except Exception:
                log.exception("Worker heartbeat failed")
                self.shutdown.set()
                return

    def process_source(self, run_id, source, jobs):
        from .cloud import CancellationProbe
        cancelled = CancellationProbe(lambda:self.store.cancelled(run_id),self.shutdown)
        client = self.client_factory(source, cancelled=cancelled)
        stop_reason = self.store.pause_reason(run_id, source)
        failures = 0
        try:
            for job_id, rule in jobs:
                if self.shutdown.is_set():
                    break
                if not self.store.processing(job_id, self.owner):
                    continue
                if self.store.cancelled(run_id):
                    result = Observation("cancelled",rule.url,detail="Остановлено пользователем")
                elif self.store.reuse_monthly(job_id,self.owner) is True:
                    log.info("run=%s job=%s source=%s status=already_collected",run_id,job_id,source)
                    continue
                elif stop_reason:
                    result = Observation("source_stopped",rule.url,detail=stop_reason)
                else:
                    try:
                        url, status, html = client.fetch(rule.url)
                        result = ADAPTERS[source].parse(rule,html,url,status)
                        failures = 0
                    except FetchError as e:
                        if self.shutdown.is_set():
                            break
                        result = Observation(e.status,rule.url,detail=str(e),http_status=e.http_status)
                        failures = failures + 1 if e.source_failure else 0
                        if e.stop_source or failures >= 3:
                            stop_reason = str(e)
                            self.store.pause_source(run_id,source,stop_reason)
                    except Exception:
                        log.exception("Adapter failure: source=%s job=%s",source,job_id)
                        result = Observation("parse_error",rule.url,detail="Ошибка адаптера; подробности в журнале сборщика")
                self.store.record(job_id,result,self.owner)
                log.info("run=%s job=%s source=%s status=%s",run_id,job_id,source,result.status)
        finally:
            client.close()

    def process_catalog_source(self,run_id,source):
        from .catalog import process_catalog
        from .catalog_outbox import CatalogOutbox
        from .runtime import RuntimeProgress,activate,context
        progress=RuntimeProgress();activate(progress);context(run_id)
        with self.progress_lock:self.progress[source]=progress
        root=Path(__file__).resolve().parents[1]/'data'/'catalog_outbox'
        outbox=CatalogOutbox(root)
        try:
            for attempt in range(3):
                if self.shutdown.is_set():return
                try:
                    process_catalog(self.store.catalog,run_id,source,self.owner,self.shutdown,self.client_factory,outbox)
                    return
                except Exception as exc:
                    progress.recover(exc)
                    trace=traceback.extract_tb(exc.__traceback__)
                    location=next((f'{Path(f.filename).name}:{f.lineno}' for f in reversed(trace) if 'price_monitor' in f.filename),'worker')
                    # Exception messages may contain connection credentials.
                    note=f'Сбой сборщика: {type(exc).__name__} ({location}); восстановление {attempt+1}/3'
                    progress.update(detail=note)
                    log.warning('run=%s source=%s %s',run_id,source,note)
                    try:
                        self.store.catalog.recovery_note(run_id,source,self.owner,note)
                        self.store.catalog.report_health(source,self.owner,progress.snapshot())
                    except Exception:log.warning('Could not persist source recovery status')
                    if attempt==2:
                        try:self.store.catalog.block_source(run_id,source,self.owner,note)
                        except Exception:log.warning('Could not persist stopped source status')
                        return
                    if self.shutdown.wait(5*(attempt+1)):return
        finally:
            progress.update(phase='idle',phase_started=time.time())
            try:self.store.catalog.report_health(source,self.owner,progress.snapshot())
            except Exception:pass
            activate(None)

    def process_run(self, run_id):
        groups = defaultdict(list)
        for job in self.store.pending(run_id):
            groups[job[1].source].append(job)
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="source") as pool:
            futures = [pool.submit(self.process_source,run_id,source,jobs) for source,jobs in groups.items()]
            external={r['source'] for r in self.store.external_sources() if r['enabled']}
            for row in self.store.catalog.sources(run_id):
                if row['source'] not in external and row['state'] in ('pending','running'):
                    futures.append(pool.submit(self.process_catalog_source,run_id,row['source']))
            for future in futures:
                future.result()
        if not self.shutdown.is_set():
            self.store.finish(run_id)

    def run(self, once=False):
        if not self.store.acquire(self.owner):
            raise RuntimeError("Уже работает другой сборщик. После аварии блокировка освобождается через 120 секунд")
        finished = threading.Event()
        heartbeat = threading.Thread(target=self.heartbeat_loop,args=(finished,),daemon=True)
        heartbeat.start()
        try:
            while not self.shutdown.is_set():
                run_id = self.store.claim_run(self.owner)
                if run_id:
                    self.process_run(run_id)
                if once:
                    break
                self.shutdown.wait(1)
        finally:
            finished.set()
            heartbeat.join(timeout=15)
            self.store.release(self.owner)


def main():
    parser = argparse.ArgumentParser(description="Фоновый сборщик цен")
    parser.add_argument("--once", action="store_true", help="Выполнить один запуск из очереди и завершиться")
    parser.add_argument("--db", help="Путь к SQLite")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    worker = Worker(Store(args.db))
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig, lambda *_:worker.shutdown.set())
    worker.run(once=args.once)


if __name__ == "__main__":
    main()
