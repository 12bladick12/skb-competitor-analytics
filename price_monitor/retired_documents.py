"""Retire legacy document jobs without deleting stored evidence or price history."""
import threading
from .models import utcnow


def stop_document_threads():
    for thread in threading.enumerate():
        if thread.name!='passport-downloads' or thread is threading.current_thread():continue
        worker=getattr(getattr(thread,'_target',None),'__self__',None)
        if (worker is not None and type(worker).__module__=='price_monitor.passport_worker'
                and type(worker).__name__=='PassportWorker'):
            worker.shutdown.set()
            thread.join(timeout=3)


def disable_document_jobs(repository):
    """Fence old application instances in the shared DB, including future inserts."""
    if repository.settings:
        statements=[
            """CREATE OR REPLACE FUNCTION retired_passport_job() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RETURN NULL; END; $$""",
            'DROP TRIGGER IF EXISTS retired_passport_insert ON passport_jobs',
            'CREATE TRIGGER retired_passport_insert BEFORE INSERT ON passport_jobs FOR EACH ROW EXECUTE FUNCTION retired_passport_job()',
            'DROP TRIGGER IF EXISTS retired_passport_claim ON passport_jobs',
            "CREATE TRIGGER retired_passport_claim BEFORE UPDATE ON passport_jobs FOR EACH ROW WHEN (NEW.state IN ('pending','retry','processing')) EXECUTE FUNCTION retired_passport_job()"]
    else:
        statements=[
            'CREATE TRIGGER IF NOT EXISTS retired_passport_insert BEFORE INSERT ON passport_jobs BEGIN SELECT RAISE(IGNORE); END',
            "CREATE TRIGGER IF NOT EXISTS retired_passport_claim BEFORE UPDATE ON passport_jobs WHEN NEW.state IN ('pending','retry','processing') BEGIN SELECT RAISE(IGNORE); END"]
    statements += ["""UPDATE passport_jobs SET state='cancelled',owner='',lease_until=0,updated_at=%(now)s,
        detail='Загрузка и обработка паспортов отключены'
        WHERE state IN ('pending','retry','processing')"""]
    repository.batch(statements,{'now':utcnow()})
