"""Killable source workers: a blocked DNS/socket/parser cannot pin the router.

Secrets travel through multiprocessing's private spawn channel, not argv, disk
or logs. Children reuse the router lease and never initialize database schema.
"""
import faulthandler
import logging
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time
import uuid

from .runtime import RuntimeProgress
from .sensoren_supervisor import restart_reason

log=logging.getLogger('price_monitor')
HEALTH_KEYS=('run_id','phase','url','phase_started','activity_at','completed_at','recoveries','detail')


def _source_entry(settings, path, run_id, source, owner, mode, jobs, shutdown, status_path):
    from .storage import Store
    from .worker import Worker
    finished=threading.Event()
    startup=RuntimeProgress()
    worker=None

    def report():
        dumped=False
        while not finished.is_set():
            tracker=startup
            if worker is not None:
                with worker.progress_lock:tracker=worker.progress.get(source,startup)
            state={**tracker.watchdog_snapshot(),'pid':os.getpid()}
            try:
                temporary=status_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
                temporary.replace(status_path)
            except OSError:pass
            stalled=state['inactive_seconds']>=150 or state['task_seconds']>=570
            if stalled and not dumped:faulthandler.dump_traceback(all_threads=True)
            dumped=stalled
            finished.wait(1)

    threading.Thread(target=report,name='source-progress',daemon=True).start()
    store=None
    try:
        store=Store(path=path,postgres=settings,initialize=False)
        worker=Worker(store)
        worker.owner=owner
        worker.shutdown=shutdown
        if mode=='catalog':worker.process_catalog_source(run_id,source)
        else:
            jobs=[job for job in store.pending(run_id) if job[1].source==source]
            worker.process_source(run_id,source,jobs)
    except BaseException as exc:
        # Driver messages may include credentials; log the type and code location.
        import traceback
        trace=traceback.extract_tb(exc.__traceback__)
        location=f'{Path(trace[-1].filename).name}:{trace[-1].lineno}' if trace else 'source'
        log.error('run=%s source=%s child failed: %s (%s)',run_id,source,type(exc).__name__,location)
        raise SystemExit(1) from None
    finally:
        finished.set()
        if store is not None:store.close()


def stop_process(process):
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
    if process.is_alive():raise RuntimeError('Не удалось завершить процесс источника')
    process.join(timeout=1)


def run_source_process(worker, run_id, source, mode, jobs=None, *, stall_seconds=180, page_seconds=600):
    ctx=multiprocessing.get_context('spawn')
    progress=RuntimeProgress()
    progress.task(run_id,'')
    with worker.progress_lock:worker.progress[source]=progress
    settings=worker.store.pg.settings if worker.store.pg is not None else None
    path=str(worker.store.path) if settings is None else None
    try:
        for attempt in range(3):
            if worker.shutdown.is_set():return
            if mode=='jobs':
                repo=worker.store.catalog
                repo.batch("""UPDATE jobs SET state='pending' WHERE run_id=%(run)s AND state='processing'
                    AND rule_id IN (SELECT id FROM rules WHERE source=%(source)s)
                    AND NOT EXISTS(SELECT 1 FROM observations o WHERE o.job_id=jobs.id) AND """+repo.allowed(),
                    repo.params(run_id,source,worker.owner))
            child_stop=ctx.Event()
            directory=Path(__file__).resolve().parents[1]/'data'/'collector_processes'
            directory.mkdir(parents=True,exist_ok=True)
            status_path=directory/(uuid.uuid4().hex+'.json')
            process=ctx.Process(target=_source_entry,
                args=(settings,path,run_id,source,worker.owner,mode,None,child_stop,status_path),
                name='price-source-'+source,daemon=True)
            started=time.monotonic()
            prior_recoveries=progress.snapshot()['recoveries']
            state=None
            reason=''
            try:
                process.start()
                while process.is_alive():
                    if worker.shutdown.is_set():
                        child_stop.set()
                        process.join(timeout=3)
                        return
                    try:
                        state=json.loads(status_path.read_text(encoding='utf-8'))
                        health={key:state[key] for key in HEALTH_KEYS}
                        health['recoveries']+=prior_recoveries
                        progress.update(**health)
                    except (OSError,ValueError,KeyError,TypeError):state=None
                    reason=restart_reason(state,process.pid,started,time.monotonic(),stall_seconds,page_seconds)
                    if reason:break
                    worker.shutdown.wait(1)
                if not reason and process.exitcode==0:return
                reason=reason or f'Процесс источника завершился с кодом {process.exitcode}'
            finally:
                if process.pid is not None:
                    stop_process(process)
                    process.close()
                status_path.unlink(missing_ok=True)
                status_path.with_suffix('.tmp').unlink(missing_ok=True)
            progress.recover(RuntimeError(reason))
            note=f'{reason}; перезапуск источника {attempt+1}/3'
            progress.update(detail=note)
            log.warning('run=%s source=%s %s',run_id,source,note)
            if mode=='catalog':worker.store.catalog.recovery_note(run_id,source,worker.owner,note)
            if attempt==2:
                if mode=='catalog':worker.store.catalog.block_source(run_id,source,worker.owner,note)
                else:worker.store.pause_source(run_id,source,note)
                return
            if worker.shutdown.wait(5*(attempt+1)):return
    finally:
        progress.update(phase='idle',phase_started=time.time())
