"""One shared background worker per running Streamlit process, without UI calls."""
import logging
import threading
import time

from .worker import Worker

log = logging.getLogger("price_monitor.cloud")
WORKER_VERSION='bounded-load-2026-10-02'


def retire_legacy_workers():
    """Fence cached workers after a hot deployment before creating replacements.

    Streamlit may retain a cache_resource instance from an older module. These
    objects predate our registry, so locate only our named controller threads.
    Releasing their exact owner prevents late writes even if old I/O is stuck.
    """
    for thread in threading.enumerate():
        if thread.name!='price-monitor-cloud':continue
        controller=getattr(getattr(thread,'_target',None),'__self__',None)
        if controller is None or getattr(controller,'version',None)==WORKER_VERSION:continue
        if not all(hasattr(controller,key) for key in ('stopping','guard','store','worker')):continue
        controller.stopping.set()
        with controller.guard:
            worker=controller.worker
            if worker is not None:
                worker.shutdown.set()
                controller.store.release(worker.owner)
        thread.join(timeout=2)


class EmbeddedWorker:
    def __init__(self, store, worker_factory=Worker):
        self.version=WORKER_VERSION
        self.store = store
        self.worker_factory = worker_factory
        self.stopping = threading.Event()
        self.worker = None
        self.guard = threading.Lock()
        self.thread = threading.Thread(target=self._loop, name="price-monitor-cloud", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stopping.is_set():
            with self.guard:
                if self.stopping.is_set():
                    break
                self.worker = self.worker_factory(self.store)
            try:
                self.worker.run()
            except Exception as e:
                # Do not put credentials / a connection string in viewer output.
                log.warning("Background worker will retry (%s)", type(e).__name__)
            if not self.stopping.is_set():
                self.stopping.wait(10)

    def close(self):
        self.stopping.set()
        with self.guard:
            if self.worker:
                self.worker.shutdown.set()
        self.thread.join(timeout=60)


class CancellationProbe:
    """Avoid a database round-trip on every 250 ms wait or response chunk."""
    def __init__(self, check, stopping, ttl=1.0):
        self.check, self.stopping, self.ttl = check, stopping, ttl
        self.last = 0
        self.value = False

    def __call__(self):
        if self.stopping.is_set():
            return True
        now = time.monotonic()
        if now-self.last >= self.ttl:
            self.value, self.last = self.check(), now
        return self.value

    def cached(self):
        """Nonblocking check for browser callbacks; caller refreshes outside them."""
        return self.stopping.is_set() or self.value
