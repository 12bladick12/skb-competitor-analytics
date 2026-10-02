"""Regression checks for stalled processes, retries and resumable finalization."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from price_monitor.models import utcnow
from price_monitor.product_state import ProductState
from price_monitor.runtime import activate, completed
from price_monitor.sensoren_supervisor import restart_reason
from price_monitor.source_process import run_source_process
from price_monitor.storage import Store
from price_monitor.worker import Worker

ROOT=Path(__file__).resolve().parents[1]


def _hung_source(settings,path,run_id,source,owner,mode,jobs,shutdown,status_path):
    # No network or production storage. This process deliberately ignores stop.
    Path(path).with_suffix('.child-started').write_text('started',encoding='utf-8')
    while True:time.sleep(60)


def _successful_source(settings,path,run_id,source,owner,mode,jobs,shutdown,status_path):
    store=Store(path)
    store.catalog.batch("UPDATE catalog_sources SET state='completed' WHERE run_id=%(run)s AND source=%(source)s",
                        {'run':run_id,'source':source})
    store.close()


def _offline_real_source(*args):
    from price_monitor.source_process import _source_entry
    from price_monitor.transport import SourceClient
    def fetch(url,html_only=False):
        return url,200,'<urlset/>' if '.xml' in url else '<html></html>'
    with patch.object(SourceClient,'fetch_document',side_effect=fetch):
        _source_entry(*args)


class ParserReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'data')
        self.path=Path(self.tmp.name)/'test.sqlite3'
        self.store=Store(self.path)
        self.run=self.store.enqueue_catalog({'megak':['МЕГА-К']})
        self.worker=Worker(self.store)
        self.owner=self.worker.owner
        self.store.acquire(self.owner)
        self.store.claim_run(self.owner)
        self.repo=self.store.catalog

    def tearDown(self):
        activate(None)
        self.store.close()
        self.tmp.cleanup()

    def test_corrupt_local_status_cannot_crash_the_supervisor(self):
        for state in ([],42,{'pid':42},{'pid':42,'emitted_at':'bad'},
                      {'pid':42,'emitted_at':float('nan'),'inactive_seconds':0,'task_seconds':0}):
            with self.subTest(state=state):
                self.assertEqual(restart_reason(state,42,0,10),'')
                self.assertTrue(restart_reason(state,42,0,181))

    def test_hot_deployment_fences_old_cached_controller_only(self):
        from price_monitor.cloud import retire_legacy_workers,WORKER_VERSION
        class Controller:
            def __init__(self,version):
                self.version=version
                self.stopping=threading.Event()
                self.guard=threading.Lock()
                self.store=Mock()
                self.worker=Mock(owner='old-owner',shutdown=threading.Event())
            def loop(self):pass
        old=Controller('old')
        current=Controller(WORKER_VERSION)
        old_thread=Mock();old_thread.name='price-monitor-cloud';old_thread._target=old.loop
        current_thread=Mock();current_thread.name='price-monitor-cloud';current_thread._target=current.loop
        with patch('price_monitor.cloud.threading.enumerate',return_value=[old_thread,current_thread]):
            retire_legacy_workers()
        self.assertTrue(old.stopping.is_set())
        self.assertTrue(old.worker.shutdown.is_set())
        old.store.release.assert_called_once_with('old-owner')
        current.store.release.assert_not_called()
        self.assertFalse(current.stopping.is_set())

    def test_hung_cloud_source_is_killed_retried_and_reported_blocked(self):
        started=time.monotonic()
        original_wait=self.worker.shutdown.wait
        # Windows spawn imports the test suite before entering the child target.
        # Leave time for that import while still verifying three bounded retries.
        stall_budget=6 if os.name=='nt' else 2
        with patch('price_monitor.source_process._source_entry',_hung_source), \
             patch.object(self.worker.shutdown,'wait',side_effect=lambda n:original_wait(min(n,.05))):
            run_source_process(self.worker,self.run,'megak','catalog',stall_seconds=stall_budget,page_seconds=stall_budget+2)
        self.assertLess(time.monotonic()-started,35 if os.name=='nt' else 20)
        source=self.repo.source(self.run,'megak')
        self.assertEqual(source['state'],'blocked')
        self.assertIn('3/3',source['detail'])
        self.assertFalse(any(p.name=='price-source-megak' for p in multiprocessing.active_children()))
        self.assertTrue(self.path.with_suffix('.child-started').exists())

    def test_normal_child_finishes_without_restarting(self):
        with patch('price_monitor.source_process._source_entry',_successful_source):
            run_source_process(self.worker,self.run,'megak','catalog',stall_seconds=10)
        self.assertEqual(self.repo.source(self.run,'megak')['state'],'completed')
        self.assertEqual(self.worker.progress['megak'].snapshot()['recoveries'],0)

    def test_real_child_runs_catalog_without_reinitializing_or_network(self):
        with patch('price_monitor.source_process._source_entry',_offline_real_source):
            run_source_process(self.worker,self.run,'megak','catalog',stall_seconds=15)
        self.assertEqual(self.repo.source(self.run,'megak')['state'],'completed')
        self.assertGreater(self.repo.batch('SELECT count(*) n FROM catalog_pages')[0]['n'],0)

    def test_router_error_stops_other_monitors_before_executor_join(self):
        self.repo.batch("INSERT INTO catalog_sources(run_id,source,brands_json) VALUES(%(run)s,'sensor','[]')",{'run':self.run})
        def failing(worker,run_id,source,*args):
            if source=='megak':raise RuntimeError('fixture monitor failed')
            worker.shutdown.wait(10)
        started=time.monotonic()
        with patch('price_monitor.source_process.run_source_process',side_effect=failing):
            with self.assertRaisesRegex(RuntimeError,'fixture monitor failed'):
                self.worker.process_run(self.run)
        self.assertTrue(self.worker.shutdown.is_set())
        self.assertLess(time.monotonic()-started,3)

    def test_failed_manual_source_stops_retrying_and_remaining_jobs_finish_with_reason(self):
        from price_monitor.models import Rule
        rule=Rule('megak','МЕГА-К','TEST-1','https://mega-k.com/products/test-1')
        with self.store.connect() as conn:
            cur=conn.execute("INSERT INTO rules(rule_key,source,manufacturer,article,product_url,url_template,created_at) VALUES(?,?,?,?,?,?,?)",
                             (rule.key,rule.source,rule.manufacturer,rule.article,rule.url,'',utcnow()))
            conn.execute("INSERT INTO jobs(run_id,rule_id,state) VALUES(?,?,'processing')",(self.run,cur.lastrowid))
        process=Mock(pid=42,exitcode=1)
        process.is_alive.return_value=False
        ctx=Mock();ctx.Process.return_value=process
        with patch('price_monitor.source_process.multiprocessing.get_context',return_value=ctx), \
             patch.object(self.worker.shutdown,'wait',return_value=False):
            run_source_process(self.worker,self.run,'megak','jobs')
        self.assertEqual(process.start.call_count,3)
        self.assertIn('3/3',self.store.pause_reason(self.run,'megak'))
        client=Mock();self.worker.client_factory=Mock(return_value=client)
        self.worker.process_source(self.run,'megak',self.store.pending(self.run))
        client.fetch.assert_not_called()
        self.assertEqual(self.store.results(run_id=self.run)[0]['status'],'source_stopped')
        self.assertEqual(self.store.pending(self.run),[])

    def test_shutdown_does_not_wait_for_an_unresponsive_child(self):
        timer=threading.Timer(2,self.worker.shutdown.set)
        timer.start()
        started=time.monotonic()
        try:
            with patch('price_monitor.source_process._source_entry',_hung_source):
                run_source_process(self.worker,self.run,'megak','catalog')
        finally:timer.cancel()
        self.assertLess(time.monotonic()-started,10)
        self.assertFalse(any(p.name=='price-source-megak' for p in multiprocessing.active_children()))

    def test_separate_failures_after_success_do_not_exhaust_lifetime_budget(self):
        calls=[]
        def work(*args,**kwargs):
            calls.append(1)
            if len(calls)<=5:
                completed()
                raise ConnectionError('must not leak credentials')
        with patch('price_monitor.catalog.process_catalog',side_effect=work), \
             patch.object(self.worker.shutdown,'wait',return_value=False):
            self.worker.process_catalog_source(self.run,'megak')
        self.assertEqual(len(calls),6)
        self.assertNotEqual(self.repo.source(self.run,'megak')['state'],'blocked')

    def test_old_recovery_note_is_cleared_when_source_resumes(self):
        self.repo.recovery_note(self.run,'megak',self.owner,'old lock failure')
        self.assertTrue(self.repo.start(self.run,'megak',self.owner))
        self.assertEqual(self.repo.source(self.run,'megak')['detail'],'')

    def test_health_from_previous_owner_cannot_look_like_current_stall(self):
        self.repo.batch("""INSERT INTO collector_health(source,owner,run_id,phase,url,
            phase_started,activity_at,completed_at,recoveries,detail)
            VALUES('megak','previous-router',%(run)s,'download','old-url',1,1,1,0,'')""",{'run':self.run})
        self.assertIsNone(self.repo.progress(self.run)[0]['work_phase'])

    def test_manifest_resumes_after_interruption_without_large_result_sets(self):
        self.repo.start(self.run,'megak',self.owner)
        self.repo.add_pages(self.run,'megak',[
            ('listing','https://mega-k.com/categories/all'),('product','https://mega-k.com/products/known')],self.owner)
        self.repo.batch("UPDATE catalog_pages SET state='done',http_status=200")
        with self.store.connect() as conn:
            for i in range(35):
                cur=conn.execute("INSERT INTO rules(rule_key,source,manufacturer,article,product_url,url_template,created_at) VALUES(?,'megak',?,?,?,?,?)",
                    (f'key-{i}','МЕГА-К',f'TEST-{i}',f'https://mega-k.com/products/missing-{i}','',utcnow()))
                conn.execute("INSERT INTO product_lifecycle(rule_id,first_seen,last_seen,checked_at,current_url) VALUES(?,?,?,?,?)",
                    (cur.lastrowid,utcnow(),utcnow(),utcnow(),f'https://mega-k.com/products/missing-{i}'))
        original_add=self.repo.add_pages
        calls=[]
        def interrupted(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:raise ConnectionError('network interrupted')
            return original_add(*args,**kwargs)
        with patch.object(self.repo,'add_pages',side_effect=interrupted):
            with self.assertRaises(ConnectionError):
                ProductState(self.repo).manifest(self.run,'megak',self.owner,['МЕГА-К'])
        self.assertTrue(self.repo.batch('SELECT note FROM catalog_integrity')[0]['note'])
        original_batch=self.repo.batch
        sizes=[]
        def measured(sql,params=None):
            rows=original_batch(sql,params)
            if isinstance(sql,str) and sql.lstrip().startswith('SELECT'):sizes.append(len(rows))
            return rows
        with patch.object(self.repo,'batch',side_effect=measured):
            self.assertTrue(ProductState(self.repo).manifest(self.run,'megak',self.owner,['МЕГА-К']))
        self.assertLessEqual(max(sizes),16)
        self.assertEqual(self.repo.batch("SELECT count(*) n FROM catalog_pages WHERE kind='verify'")[0]['n'],35)
        self.assertEqual(self.repo.batch('SELECT note FROM catalog_integrity')[0]['note'],'')
        self.assertFalse(ProductState(self.repo).manifest(self.run,'megak',self.owner,['МЕГА-К']))


@unittest.skipUnless(os.getenv('SKB_TEST_POSTGRES_URL'),'Dedicated PostgreSQL test service absent')
class HeartbeatLockTests(unittest.TestCase):
    def test_telemetry_works_while_catalog_lock_is_held_and_writes_stay_serialized(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from concurrent.futures import ThreadPoolExecutor,TimeoutError
        from price_monitor.db_batches import batch
        # Only the dedicated CI/test URL is accepted: never hold the production lock.
        dsn=os.environ['SKB_TEST_POSTGRES_URL']
        schema='collector_lock_test_'+uuid.uuid4().hex
        settings={**conninfo_to_dict(dsn),'reuse_connections':False}
        with psycopg.connect(dsn,autocommit=True) as setup:
            setup.execute(f'CREATE SCHEMA {schema}')
            try:
                setup.execute(f'CREATE TABLE {schema}.health(value INTEGER)')
                setup.execute(f'INSERT INTO {schema}.health VALUES(0)')
                with psycopg.connect(dsn) as locker:
                    locker.execute('SELECT pg_advisory_xact_lock(6743928101)')
                    rows=batch(settings,f'UPDATE {schema}.health SET value=1 RETURNING value',serialize=False,timeout=3)
                    self.assertEqual(rows[0]['value'],1)
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future=pool.submit(batch,settings,f'UPDATE {schema}.health SET value=2 RETURNING value')
                        try:
                            with self.assertRaises(TimeoutError):future.result(timeout=.2)
                        finally:locker.rollback()
                        self.assertEqual(future.result(timeout=5)[0]['value'],2)
            finally:
                self.assertRegex(schema,r'^collector_lock_test_[0-9a-f]{32}$')
                setup.execute(f'DROP SCHEMA {schema} CASCADE')


if __name__=='__main__':unittest.main()
