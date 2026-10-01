"""One shared background worker per running Streamlit process, without UI calls."""
import logging
import threading
import time

from .worker import Worker

log = logging.getLogger("price_monitor.cloud")


class EmbeddedWorker:
    def __init__(self, store, worker_factory=Worker):
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
