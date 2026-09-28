from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import logging
import signal
import threading
import uuid

from .adapters import ADAPTERS
from .models import Observation
from .storage import Store
from .transport import FetchError, SourceClient

log = logging.getLogger("price_monitor")


class Worker:
    def __init__(self, store: Store, client_factory=SourceClient):
        self.store, self.client_factory = store, client_factory
        self.owner = str(uuid.uuid4())
        self.shutdown = threading.Event()

    def heartbeat_loop(self, finished):
        while not finished.wait(10):
            try:
                if not self.store.heartbeat(self.owner):
                    log.error("Worker lease lost")
                    self.shutdown.set()
                    return
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
                        failures = failures + 1 if e.status in {"network_error","http_error"} else 0
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

    def process_run(self, run_id):
        groups = defaultdict(list)
        for job in self.store.pending(run_id):
            groups[job[1].source].append(job)
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="source") as pool:
            futures = [pool.submit(self.process_source,run_id,source,jobs) for source,jobs in groups.items()]
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
