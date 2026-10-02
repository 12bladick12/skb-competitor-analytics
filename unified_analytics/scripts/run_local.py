"""Windows-friendly local launcher with dependency checks and a single instance."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/local'
REQUIRED = ('streamlit', 'pandas', 'requests', 'docx', 'bs4', 'lxml', 'pydantic',
            'yaml', 'feedparser', 'playwright', 'openpyxl')


def missing_dependencies():
    return [name for name in REQUIRED if importlib.util.find_spec(name) is None]


def ensure_dependencies():
    missing = missing_dependencies()
    if not missing:
        return None
    print('Установка библиотек для локального приложения: ' + ', '.join(missing), flush=True)
    environment = ROOT / '.venv-local'
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if Path(sys.prefix).resolve() != environment.resolve():
        subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements-local.txt')], check=True)
    return subprocess.call([str(python), str(Path(__file__).resolve()), *sys.argv[1:]])


def ready(url):
    try:
        with build_opener(ProxyHandler({})).open(url.rstrip('/') + '/_stcore/health', timeout=1) as response:
            return response.status == 200 and response.read().strip() == b'ok'
    except (URLError, OSError):
        return False


def acquire_lock(handle):
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def available_port(first=8530):
    for port in range(first, first + 20):
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise OSError('Нет свободного локального порта')


def stop_server(process):
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGTERM)
        process.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def launch(no_browser=False):
    DATA.mkdir(parents=True, exist_ok=True)
    if (DATA / 'migration_pending').exists():
        print('Перенос рабочих данных ещё не завершён. Повторите запуск после завершения переноса.', flush=True)
        return 1
    state_file = DATA / 'server.json'
    lock = (DATA / 'launcher.lock').open('a+b')
    if lock.tell() == 0:
        lock.write(b'0')
        lock.flush()
    try:
        acquire_lock(lock)
    except OSError:
        lock.close()
        # A second double-click can occur while the first instance is starting.
        for _ in range(30):
            try:
                state = json.loads(state_file.read_text(encoding='utf-8'))
                url = state['url']
                if url.startswith('http://127.0.0.1:') and ready(url):
                    print('Приложение уже работает: ' + url, flush=True)
                    if not no_browser:
                        webbrowser.open(url)
                    return 0
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(1)
        print('Приложение запускается в другом окне. Проверьте его сообщения.', flush=True)
        return 1

    process = None
    try:
        state_file.unlink(missing_ok=True)
        port = available_port()
        url = f'http://127.0.0.1:{port}/'
        environment = dict(os.environ, PRICE_MONITOR_DB=str(DATA / 'prices.sqlite3'),
                           PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        print('Локальная база: ' + environment['PRICE_MONITOR_DB'], flush=True)
        print('Запускаем приложение…', flush=True)
        with (DATA / 'server.stdout.log').open('ab') as output, (DATA / 'server.stderr.log').open('ab') as errors:
            process = subprocess.Popen([
                sys.executable, '-m', 'streamlit', 'run', str(ROOT / 'local_app.py'),
                '--server.address', '127.0.0.1', '--server.port', str(port),
                '--server.headless', 'true', '--server.fileWatcherType', 'none',
                '--browser.gatherUsageStats', 'false',
            ], cwd=ROOT, env=environment, stdout=output, stderr=errors,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and process.poll() is None:
                if ready(url):
                    break
                time.sleep(0.3)
            else:
                print('Не удалось запустить приложение. Журнал: ' + str(DATA / 'server.stderr.log'), flush=True)
                return 1
            state_file.write_text(json.dumps({'pid': process.pid, 'url': url}, indent=2), encoding='utf-8')
            print('Приложение доступно: ' + url, flush=True)
            print('Оставьте это окно открытым. Для остановки нажмите Ctrl+C.', flush=True)
            if not no_browser:
                webbrowser.open(url)
            # A short interruptible sleep keeps Ctrl+C responsive on Windows;
            # an infinite native child-process wait can delay Python's handler.
            while process.poll() is None:
                time.sleep(0.3)
            return process.returncode
    except KeyboardInterrupt:
        print('\nОстанавливаем приложение…', flush=True)
        return 0
    finally:
        if process is not None:
            stop_server(process)
        state_file.unlink(missing_ok=True)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--check', action='store_true', help='Проверить запуск без открытия сервера')
    args = parser.parse_args()
    os.chdir(ROOT)
    if sys.version_info < (3, 11):
        print('Нужен Python 3.11 или новее. В проекте используется Python 3.13.')
        return 1
    if args.check:
        missing = missing_dependencies()
        print(json.dumps({'python': sys.version.split()[0], 'missing_dependencies': missing,
                          'database_exists': (DATA / 'prices.sqlite3').exists()}, ensure_ascii=False))
        return int(bool(missing))
    try:
        installed = ensure_dependencies()
        return launch(args.no_browser) if installed is None else installed
    except (OSError, subprocess.SubprocessError) as exc:
        print('Ошибка запуска: ' + type(exc).__name__ + '. Проверьте Python и журнал в data/local.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
