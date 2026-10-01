"""Main-thread work progress, independent from the connection heartbeat."""
from contextlib import contextmanager
import threading
import time

_local = threading.local()

SCHEMA = '''CREATE TABLE IF NOT EXISTS collector_health (
 source TEXT PRIMARY KEY, owner TEXT NOT NULL, run_id INTEGER,
 phase TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
 phase_started DOUBLE PRECISION NOT NULL, activity_at DOUBLE PRECISION NOT NULL,
 completed_at DOUBLE PRECISION NOT NULL, recoveries INTEGER NOT NULL DEFAULT 0,
 detail TEXT NOT NULL DEFAULT ''
);'''

PHASES = {'idle':'Ожидание заданий', 'database':'Обмен с базой',
          'download':'Загрузка страницы', 'parse':'Разбор карточки',
          'discover':'Поиск ссылок', 'save':'Сохранение результата',
          'reconnect':'Восстановление соединения', 'reconcile':'Сверка полноты каталога'}


class RuntimeProgress:
    def __init__(self):
        self.lock=threading.Lock()
        self.last_activity=time.monotonic()
        self.task_started=None
        self.values=dict(run_id=None,phase='idle',url='',phase_started=time.time(),
                         activity_at=time.time(),completed_at=0.0,recoveries=0,detail='')

    def update(self, **values):
        with self.lock:
            self.values.update(values)
            if 'activity_at' in values:self.last_activity=time.monotonic()

    def task(self, run_id, url):
        with self.lock:
            self.values.update(run_id=run_id,url=url,phase='idle',phase_started=time.time())
            self.last_activity=time.monotonic()
            self.task_started=self.last_activity if url else None

    def complete(self):
        with self.lock:
            self.values.update(completed_at=time.time(),activity_at=time.time(),detail='')
            self.last_activity=time.monotonic()
            self.task_started=None

    def watchdog_snapshot(self):
        # Local monotonic deadlines are independent of DB heartbeat and clocks.
        with self.lock:
            now=time.monotonic()
            return {**self.values,'emitted_at':now,
                    'inactive_seconds':now-self.last_activity,
                    'task_seconds':now-self.task_started if self.task_started is not None else 0}

    def snapshot(self):
        with self.lock:return dict(self.values)

    def recover(self, error):
        with self.lock:
            self.values.update(phase='reconnect',phase_started=time.time(),detail=type(error).__name__)
            self.values['recoveries']+=1
            self.last_activity=time.monotonic()
            self.task_started=None


def activate(progress):
    _local.progress=progress


def context(run_id, url=''):
    tracker=getattr(_local,'progress',None)
    if tracker:tracker.task(run_id,url)


def completed():
    tracker=getattr(_local,'progress',None)
    if tracker:tracker.complete()


@contextmanager
def phase(name):
    tracker=getattr(_local,'progress',None)
    if tracker is None:
        yield
        return
    previous=tracker.snapshot()
    tracker.update(phase=name,phase_started=time.time(),activity_at=time.time())
    try:yield
    finally:
        tracker.update(phase=previous['phase'],phase_started=previous['phase_started'],activity_at=time.time())
