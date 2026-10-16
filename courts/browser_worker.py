"""A hard deadline also bounds DNS, browser protocol and page cleanup hangs.

Only public-source observations cross the pipe. Database/Drive clients and
checkpoint writes stay in the parent; browser descendants receive no court config.
"""
import multiprocessing
import os
import signal
import subprocess
import time
from urllib.parse import urlsplit
from .model import Page
from .sources import CATALOG, PublicBrowser


def _serve(connection, options):
    os.environ.pop('SKB_COURT_CONFIG',None)
    if os.name!='nt':os.setsid()
    try:
        with PublicBrowser(**options) as browser:
            while True:
                request=connection.recv()
                if request is None:break
                connection.send(browser.fetch(*request))
    except (EOFError,BrokenPipeError):pass
    except Exception as exc:
        try:connection.send({'error':type(exc).__name__})
        except (EOFError,BrokenPipeError,OSError):pass
    finally:connection.close()


def _stop(process):
    if process.is_alive():
        if os.name=='nt':
            # This PID is exclusively our spawned child, never a user's browser.
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                           timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                if os.getpgid(process.pid)==process.pid:os.killpg(process.pid,signal.SIGKILL)
                else:process.kill()
            except ProcessLookupError:pass
        process.join(timeout=2)
        if process.is_alive():process.kill();process.join(timeout=2)
    if process.is_alive():raise RuntimeError('Не удалось остановить процесс браузера')
    process.join(timeout=1);process.close()


class BoundedBrowser:
    def __init__(self, *, timeout=90, worker_target=_serve, **options):
        if timeout<=0:raise ValueError('Deadline must be positive')
        self.timeout=timeout;self.options=options;self.target=worker_target
        self.process=None;self.connection=None;self.blocked_hosts={}

    def __enter__(self):return self

    def _close(self,grace=0):
        if self.process is None:return
        try:
            if grace:
                try:self.connection.send(None)
                except (EOFError,BrokenPipeError,OSError):pass
                self.process.join(timeout=grace)
            _stop(self.process)
        finally:
            self.connection.close();self.connection=None;self.process=None

    def __exit__(self,*_):self._close(grace=2)

    def fetch(self,source,entity,cursor=None):
        domain,url=CATALOG[source];host=urlsplit(url).hostname
        result=Page(source,domain,entity['id'],url)
        if host in self.blocked_hosts:
            result.status=self.blocked_hosts[host]
            result.reason='Повторные запросы остановлены после ограничения доступа.'
            return result
        started=time.monotonic()
        try:
            if self.process is None:
                context=multiprocessing.get_context('spawn')
                self.connection,child=context.Pipe()
                self.process=context.Process(target=self.target,args=(child,self.options),daemon=True)
                self.process.start();child.close()
            self.connection.send((source,entity,cursor))
            if not self.connection.poll(max(0,self.timeout-(time.monotonic()-started))):
                result.status='error';result.next_cursor=cursor
                result.reason='Превышено время проверки источника; позиция сохранена для повторного сбора.'
                result.diagnostics={'stage':'worker_deadline','error':'SOURCE_DEADLINE'}
                self._close()
            else:
                received=self.connection.recv()
                if not isinstance(received,Page):raise RuntimeError('Source worker failed')
                result=received
                if result.status in ('challenge','access_denied','rate_limited'):
                    self.blocked_hosts[host]=result.status
        except (EOFError,BrokenPipeError,OSError,RuntimeError) as exc:
            self._close()
            result.status='error';result.next_cursor=cursor
            result.reason='Процесс источника завершился без результата; позиция сохранена.'
            result.diagnostics={'stage':'worker_error','error':type(exc).__name__}
        result.diagnostics['worker_elapsed_seconds']=round(time.monotonic()-started,1)
        return result
