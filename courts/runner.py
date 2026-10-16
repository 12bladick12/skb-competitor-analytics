"""Private Actions entry point. Configuration arrives through an environment secret."""
import argparse
from datetime import datetime
import json
import os
import sys
import threading
import time
from uuid import uuid4
from cloud.drive_store import DriveStore
from cloud.jobs import LeaseLost
from .collector import collect
from .gate import LOCAL, release_reasons
from .model import now
from .registry import entities, fingerprint
from .sources import PublicBrowser, CATALOG
from .store import CourtStore


def daily_key(at=None):
    return (at or datetime.now(LOCAL)).astimezone(LOCAL).date().isoformat()


def schedule(store, import_id, *, at=None, recovery=False):
    today=daily_key(at)
    store.query("""UPDATE skb_analytics.jobs SET status='interrupted',finished_at=now(),token=NULL,
        stage='Предыдущий судебный сбор остановлен; сохранённые позиции будут продолжены'
        WHERE import_id=$1 AND kind='court_collect' AND status='running' AND lease_until<now() RETURNING id""",[import_id],write=True)
    runs=list(store.all(import_id,'court_run'))
    if any(r.get('status')=='success' and datetime.fromisoformat(r['finished_at']).astimezone(LOCAL).date().isoformat()==today for r in runs):
        return None
    # A queued manual task is reused by the import-wide unique constraint.
    existing=store.query("SELECT * FROM skb_analytics.jobs WHERE import_id=$1 AND kind='court_collect' AND status IN ('queued','running')",[import_id])
    if existing:return existing[0]
    attempts=store.query("SELECT count(*) n FROM skb_analytics.jobs WHERE import_id=$1 AND kind='court_collect' AND request_key LIKE $2",[import_id,'daily:'+today+':%'])
    if int(attempts[0]['n'])>=3:return None
    return store.enqueue(import_id,'court_collect',today[:7],{'day':today,'recovery':recovery},
                         'scheduler','daily:'+today+':'+str(attempts[0]['n']))


def run_job(store, config, job, *, channel=None, sources=None, max_pages=25, seconds=2400):
    stop=threading.Event();lost=threading.Event();start=time.monotonic()
    def heartbeat():
        while not stop.wait(25):
            try:
                if time.monotonic()-start>seconds+120:lost.set();return
                store.pulse(job)
            except Exception:lost.set();return
    def alive():
        if lost.is_set():raise LeaseLost('Связь с очередью потеряна; результат не опубликован.')
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    drive=DriveStore(config)
    try:
        with PublicBrowser(channel=channel) as browser:
            return collect(store,drive,job,browser,alive,sources=sources,max_pages=max_pages,seconds=seconds)
    except Exception as exc:
        # The exception may contain credentials from an upstream URL; only persist its class.
        store.fail(job,'Судебный сбор прерван: '+type(exc).__name__,
                   'interrupted' if isinstance(exc,LeaseLost) else 'error')
        raise
    finally:
        stop.set();thread.join(timeout=2);drive.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--migrate',action='store_true')
    parser.add_argument('--schedule',action='store_true')
    parser.add_argument('--recovery',action='store_true')
    parser.add_argument('--check-release',action='store_true')
    parser.add_argument('--activate',action='store_true',help='Открыть раздел только после прохождения контрольных условий')
    parser.add_argument('--channel',choices=['msedge','chrome'])
    parser.add_argument('--sources',nargs='+',choices=CATALOG)
    parser.add_argument('--max-pages',type=int,default=25)
    parser.add_argument('--seconds',type=int,default=2400)
    args=parser.parse_args()
    config=json.loads(os.environ['SKB_COURT_CONFIG'])
    store=CourtStore(config)
    if args.migrate:
        store.initialize();print('Court queue migration complete. Public release remains disabled.');return
    active=store.query("SELECT import_id FROM skb_analytics.state WHERE key='active'")
    if not active:raise ValueError('Нет активной библиотеки.')
    ident=active[0]['import_id']
    if args.activate:
        from .acceptance import certify
        certify(store,ident);print('Public court projection enabled after certification.');return
    if args.check_release:
        from .acceptance import audit
        reasons=release_reasons(list(store.all(ident,'court_entity')),
                                list(store.all(ident,'court_run')),audit(store,ident))
        print(json.dumps({'ready':not reasons,'reasons':reasons},ensure_ascii=False));return
    if args.schedule:schedule(store,ident,recovery=args.recovery)
    # Parent process lifetime is irrelevant: this worker runs in Actions, not Streamlit.
    # Global queue fencing keeps exports/news publications and court commits serialized.
    job=None
    for _ in range(12):
        job=store.claim(uuid4().hex)
        if job:break
        if not any(j['kind']=='court_collect' and j['status']=='queued' for j in store.list(ident)):break
        time.sleep(5)
    if not job:print('No claimable court task; queued work will be retried by the next window.');return
    run=run_job(store,config,job,channel=args.channel,sources=args.sources,
                max_pages=max(1,min(args.max_pages,100)),seconds=max(30,min(args.seconds,2400)))
    print(json.dumps({'job':run['id'],'status':run['status'],'checks':len(run['checks'])}))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Court worker failed: '+type(exc).__name__,file=sys.stderr);sys.exit(1)
