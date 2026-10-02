"""Restart an isolated collector on exit or stalled work; preserve its outbox.

No database access from the supervisor. A replacement uses the existing lease
and queue reconciliation, including the 120-second fencing period when needed.
"""
from contextlib import contextmanager
import argparse
import faulthandler
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


class ProgressPublisher:
    """Publish main-thread progress even if the database heartbeat is blocked."""
    def __init__(self, progress, path):
        self.progress = progress
        self.path = path
        self.finished = threading.Event()
        self.thread = None

    def publish(self):
        state = {**self.progress.watchdog_snapshot(), 'pid': os.getpid()}
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.path)
        return state

    def _loop(self):
        dumped = False
        while not self.finished.wait(5):
            try:
                state = self.publish()
                stalled = state['inactive_seconds'] >= 150 or state['task_seconds'] >= 570
                if stalled and not dumped:
                    with self.path.with_suffix('.stacks.log').open('a', encoding='utf-8') as log:
                        log.write(json.dumps(state, ensure_ascii=False) + '\n')
                        log.flush()
                        faulthandler.dump_traceback(file=log, all_threads=True)
                dumped = stalled
            except Exception as exc:
                print('Локальная диагностика: ' + type(exc).__name__, flush=True)

    def start(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.publish()
            self.thread = threading.Thread(target=self._loop, name='collector-progress', daemon=True)
            self.thread.start()

    def close(self):
        self.finished.set()
        if self.thread is not None:
            self.thread.join(timeout=6)


def restart_reason(state, pid, started, now, stall_seconds=180, page_seconds=600):
    valid=isinstance(state,dict) and state.get('pid')==pid and all(
        isinstance(state.get(key),(int,float)) and 0<=state[key]<1e15 and math.isfinite(state[key])
        for key in ('emitted_at','inactive_seconds','task_seconds'))
    if not valid:
        return 'Нет локального сигнала сборщика' if now-started >= stall_seconds else ''
    if now-state['emitted_at'] >= stall_seconds:
        return 'Процесс перестал отвечать'
    if state['inactive_seconds'] >= stall_seconds:
        return 'Основной поток не продвигается'
    if state['task_seconds'] >= page_seconds:
        return 'Превышено время обработки одной страницы'
    return ''


@contextmanager
def instance_lock(path):
    """The OS releases this lock after a crash; stale PID files cannot block it."""
    with path.open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def stop_tree(process):
    """Terminate only the process tree created by this supervisor."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=20, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def supervise(arguments, directory, stopping, stall_seconds=180, page_seconds=600):
    status_path = directory / 'progress.json'
    failures = 0
    while not stopping.is_set():
        started = time.monotonic()
        command = [sys.executable, '-u', '-m', 'price_monitor.sensoren_agent',
                   *arguments, '--progress-file', str(status_path)]
        options = {'start_new_session': True} if os.name != 'nt' else {
            'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=sys.stdout, stderr=sys.stderr, **options)
        print(time.strftime('%Y-%m-%d %H:%M:%S')+f' Сборщик запущен: PID {process.pid}', flush=True)
        reason = ''
        try:
            while not stopping.wait(5) and process.poll() is None:
                try:
                    state = json.loads(status_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    state = None
                reason = restart_reason(state, process.pid, started, time.monotonic(),
                                        stall_seconds, page_seconds)
                if reason:
                    print(time.strftime('%Y-%m-%d %H:%M:%S')+' '+reason + '; перезапуск с сохранением очереди', flush=True)
                    break
        finally:
            stop_tree(process)
        if stopping.is_set():
            return 0
        failures = failures + 1 if reason or time.monotonic()-started < 300 else 1
        delay = min(120, 10 * 2 ** min(failures-1, 4))
        print(time.strftime('%Y-%m-%d %H:%M:%S')+f' Сборщик завершён: код {process.returncode}; повтор через {delay} с', flush=True)
        stopping.wait(delay)
    return 0


def main():
    parser = argparse.ArgumentParser(description='Sensoren: контроль процесса и автоматическое восстановление')
    parser.add_argument('--stall-seconds', type=float, default=180)
    parser.add_argument('--page-seconds', type=float, default=600)
    args, arguments = parser.parse_known_args()
    if args.stall_seconds < 60 or args.page_seconds < args.stall_seconds:
        parser.error('stall-seconds >= 60; page-seconds >= stall-seconds')
    if '--disable' in arguments:
        return subprocess.call([sys.executable, '-m', 'price_monitor.sensoren_agent', *arguments], cwd=ROOT)
    directory = ROOT / 'data' / 'sensoren_agent'
    directory.mkdir(parents=True, exist_ok=True)
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    try:
        with instance_lock(directory / 'supervisor.lock'):
            return supervise(arguments, directory, stopping, args.stall_seconds, args.page_seconds)
    except OSError as exc:
        print('Контроль сборщика не запущен: проверьте другой экземпляр и доступ к папке ('
              + type(exc).__name__ + ')', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
