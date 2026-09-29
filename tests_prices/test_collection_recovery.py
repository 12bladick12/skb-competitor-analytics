import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from price_monitor.catalog import process_catalog
from price_monitor.catalog_outbox import CatalogOutbox
from price_monitor.models import Rule, Observation
from price_monitor.runtime import RuntimeProgress, activate, context, phase, completed
from price_monitor.storage import Store


ROOT=Path(__file__).resolve().parents[1]
URL='https://sensoren.ru/product/datchik_ifm_si5000/'
HTML='''<h1>Датчик ifm SI5000</h1><div class="product-info">
<div class="product-info__brand-name">ifm</div>
<div class="product-info__all-order-price"><span class="price">29530 руб.</span></div>
<ul class="characteristics-all"><li>Напряжение:<span>24 В</span></li></ul></div>'''


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):(ROOT/'data').mkdir(exist_ok=True)

    def tearDown(self):activate(None)

    def test_progress_is_separate_from_heartbeat_and_nested_stages_restore(self):
        progress=RuntimeProgress();activate(progress);context(12,URL)
        with phase('download'):
            with phase('database'):
                self.assertEqual(progress.snapshot()['phase'],'database')
            self.assertEqual(progress.snapshot()['phase'],'download')
        completed();progress.recover(ConnectionError('private diagnostic'))
        row=progress.snapshot()
        self.assertEqual(row['run_id'],12)
        self.assertEqual(row['recoveries'],1)
        self.assertEqual(row['detail'],'ConnectionError')
        self.assertGreater(row['completed_at'],0)

    def test_outbox_preserves_date_details_and_identity(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as directory:
            outbox=CatalogOutbox(directory)
            page=dict(id=hashlib.sha256(b'page').hexdigest(),run_id=3,source='sensoren',url=URL)
            rule=Rule('sensoren','ifm','SI5000',URL)
            obs=Observation('priced',URL,price='12.50',currency='RUB',checked_at='2026-09-01T00:00:00+00:00',
                            details_json=json.dumps({'attributes':[{'name':'Тест','value':'24 В'}]}))
            outbox.save(page,[(rule,obs)])
            self.assertEqual(outbox.load(page),[(rule,obs)])
            with self.assertRaises(ValueError):outbox.load({**page,'run_id':4})
            outbox.acknowledge(page)
            self.assertIsNone(outbox.load(page))

    def test_large_sitemap_yields_to_products_without_refetching(self):
        from price_monitor.catalog import seeds
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as directory:
            store=Store(Path(directory)/'queue.db');run=store.enqueue_catalog({'sensoren':['ifm']})
            owner='router2-test';store.acquire(owner);store.claim_run(owner)
            repository=store.catalog;outbox=CatalogOutbox(Path(directory)/'outbox')
            client=Mock();first_product_queued=[]
            urls=[f'https://sensoren.ru/product/datchik_ifm_si5000_{n}/' for n in range(130)]
            sitemap='<urlset>'+''.join('<url><loc>'+url+'</loc></url>' for url in urls)+'</urlset>'
            def fetch(url,html_only=False):
                if url in urls:
                    if not first_product_queued:
                        first_product_queued.append(repository.batch("SELECT count(*) n FROM catalog_pages WHERE kind='product'")[0]['n'])
                    return url,200,HTML
                return url,200,sitemap if url.endswith('.xml') else '<html></html>'
            client.fetch_document.side_effect=fetch
            process_catalog(repository,run,'sensoren',owner,threading.Event(),lambda *a,**k:client,outbox)
            self.assertEqual(first_product_queued,[64])
            self.assertEqual(len(store.results(run_id=run)),130)
            self.assertEqual(sum(c.args[0].endswith('.xml') for c in client.fetch_document.call_args_list),1)
            self.assertEqual(repository.source(run,'sensoren')['state'],'completed')
            self.assertEqual(list(outbox.root.glob('*.links.json')),[])
            store.close()

    def test_navigation_checkpoint_survives_restart_and_cancel(self):
        from price_monitor.catalog import ingest_navigation
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as directory:
            page=dict(id=hashlib.sha256(b'sitemap').hexdigest(),run_id=3,source='sensoren',url='https://sensoren.ru/sitemap.xml')
            links=[('product',URL+str(n)) for n in range(80)]
            outbox=CatalogOutbox(directory);outbox.save_navigation(page,links)
            repository=Mock();repository.cancelled.return_value=False
            ingest_navigation(repository,page,'owner',outbox,outbox.load_navigation(page))
            resumed=CatalogOutbox(directory)
            self.assertEqual(resumed.load_navigation(page)['offset'],64)
            repository.cancelled.return_value=True
            ingest_navigation(repository,page,'owner',resumed,resumed.load_navigation(page))
            self.assertEqual(resumed.load_navigation(page)['offset'],64)
            repository.cancelled.return_value=False
            ingest_navigation(repository,page,'owner',resumed,resumed.load_navigation(page))
            self.assertIsNone(resumed.load_navigation(page))

    def test_known_other_brands_skip_without_http_and_selected_products_go_first(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as directory:
            store=Store(Path(directory)/'queue.db');run=store.enqueue_catalog({'sensoren':['LANBAO']})
            owner='router2-test';store.acquire(owner);store.claim_run(owner);repo=store.catalog
            repo.start(run,'sensoren',owner)
            foreign='https://sensoren.ru/product/a_datchik_datasensing_12/'
            unknown='https://sensoren.ru/product/a_unknown_12/'
            selected='https://sensoren.ru/product/z_datchik_lanbao_12/'
            repo.add_pages(run,'sensoren',[('product',url) for url in (foreign,unknown,selected)],owner)
            repo.skip_unselected_sensoren_pages(run,['LANBAO'],owner)
            self.assertEqual(repo.batch('SELECT state FROM catalog_pages WHERE url=%(url)s',{'url':foreign})[0]['state'],'skipped')
            self.assertEqual(repo.claim(run,'sensoren',owner)['url'],selected)
            self.assertEqual(repo.claim(run,'sensoren',owner)['url'],unknown)
            store.close()

    def test_resume_after_connection_loss_before_and_after_commit(self):
        for commit_reached in (False,True):
            with self.subTest(commit_reached=commit_reached),tempfile.TemporaryDirectory(dir=ROOT/'data') as directory:
                store=Store(Path(directory)/'queue.db')
                run=store.enqueue_catalog({'sensoren':['ifm']})
                owner='router2-test';self.assertTrue(store.acquire(owner));store.claim_run(owner)
                repository=store.catalog
                outbox=CatalogOutbox(Path(directory)/'outbox')
                client=Mock()
                def fetch(url,html_only=False):
                    if url==URL:return url,200,HTML
                    if url.endswith('.xml'):return url,200,'<urlset/>'
                    return url,200,'<a href="'+URL+'">product</a>'
                client.fetch_document.side_effect=fetch
                original=repository.record_products
                def disconnected(*args,**kwargs):
                    if commit_reached:original(*args,**kwargs)
                    raise ConnectionError('acknowledgement lost')
                with patch.object(repository,'record_products',side_effect=disconnected):
                    with self.assertRaises(ConnectionError):
                        process_catalog(repository,run,'sensoren',owner,threading.Event(),lambda *a,**k:client,outbox)
                # Use the same client instance only as a call counter; no new
                # HTTP request for the saved product is allowed on recovery.
                before=sum(c.args[0]==URL for c in client.fetch_document.call_args_list)
                process_catalog(repository,run,'sensoren',owner,threading.Event(),lambda *a,**k:client,outbox)
                after=sum(c.args[0]==URL for c in client.fetch_document.call_args_list)
                self.assertEqual(before,1);self.assertEqual(after,1)
                self.assertEqual(len(store.results(run_id=run)),1)
                self.assertEqual(repository.source(run,'sensoren')['state'],'completed')
                self.assertEqual(store.results(run_id=run)[0]['price'],'29530.00')
                store.close()


class DeadlineTests(unittest.TestCase):
    def test_wait_has_finite_budget_and_closes_unknown_outcome_connection(self):
        import psycopg
        from price_monitor.db_connection import DeadlineConnection,DatabaseIOTimeout
        connection=DeadlineConnection.__new__(DeadlineConnection)
        connection.io_deadline=105
        with patch('price_monitor.db_connection.time.monotonic',return_value=100),\
             patch.object(psycopg.Connection,'wait',side_effect=psycopg.errors._WaitTimeout()),\
             patch.object(DeadlineConnection,'close') as close:
            with self.assertRaises(DatabaseIOTimeout):DeadlineConnection.wait(connection,iter(()))
            close.assert_called_once_with()
        with patch('price_monitor.db_connection.time.monotonic',return_value=100),\
             patch.object(psycopg.Connection,'wait',return_value='done') as wait:
            self.assertEqual(DeadlineConnection.wait(connection,iter(())),'done')
            self.assertEqual(wait.call_args.kwargs['timeout'],5)


if __name__=='__main__':unittest.main()
