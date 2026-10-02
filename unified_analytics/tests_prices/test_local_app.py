import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from price_monitor.models import Observation, Rule
from price_monitor.storage import Store
from price_monitor.worker import Worker
from scripts.export_prices_to_sqlite import prepare_working_copy

ROOT = Path(__file__).resolve().parents[1]


class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / 'data')
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'prices.sqlite3'

    def test_migration_preserves_prices_and_raw_snapshot_but_stops_copied_run(self):
        source = Store(self.path)
        run = source.enqueue([Rule('sensoren', 'ifm', 'SI5000', 'https://sensoren.ru/product/datchik_ifm_si5000/')])
        owner = 'router2-test-migration'
        source.acquire(owner)
        source.claim_run(owner)
        job, rule = source.pending(run)[0]
        source.processing(job, owner)
        result = Observation('priced', rule.url, price='1234.50', currency='RUB',
                             checked_at='2026-09-30T09:00:00+00:00', response_hash='original')
        source.record(job, result, owner)
        with source.connect() as conn:
            conn.execute("INSERT INTO external_sources(source,enabled,owner,heartbeat,protocol) VALUES('sensoren',1,'cloud',1,2)")
        destination = self.path.with_name('local.sqlite3')
        self.assertEqual(prepare_working_copy(self.path, destination), [run])
        with closing(sqlite3.connect(self.path)) as raw, closing(sqlite3.connect(destination)) as local:
            self.assertEqual(raw.execute('SELECT * FROM observations').fetchall(), local.execute('SELECT * FROM observations').fetchall())
            self.assertEqual(raw.execute('SELECT state FROM runs').fetchone()[0], 'running')
            self.assertEqual(local.execute('SELECT state FROM runs').fetchone()[0], 'cancelled')
            self.assertEqual(local.execute('SELECT count(*) FROM worker_lease').fetchone()[0], 0)
            self.assertEqual(local.execute('SELECT enabled FROM external_sources').fetchone()[0], 0)
            self.assertEqual(local.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(FileExistsError):
            prepare_working_copy(self.path, destination)

    def test_local_services_start_worker_and_never_open_postgres(self):
        from price_monitor.startup import open_local_services
        with patch.dict(os.environ, {'PRICE_MONITOR_DB': str(self.path)}), \
             patch('price_monitor.cloud.EmbeddedWorker') as worker, \
             patch('price_monitor.enrichment.EnrichmentWorker'), \
             patch('price_monitor.startup.CatalogMaintenance'), \
             patch('price_monitor.startup.atexit.register'), \
             patch('price_monitor.startup.open_cloud_services', side_effect=AssertionError('Cloud access')):
            store, controller = open_local_services()
        self.assertIsNone(store.pg)
        self.assertEqual(store.path, self.path)
        self.assertIs(controller, worker.return_value)
        worker.assert_called_once_with(store, startup_delay=0)

    def test_sensoren_browser_is_used_only_for_local_real_transport(self):
        from price_monitor.sensoren_browser import SensorenBrowserClient
        from price_monitor.transport import SourceClient
        store = Store(self.path)
        worker = Worker(store)
        local = worker.source_client('sensoren')
        self.assertIsInstance(local, SensorenBrowserClient)
        local.close()
        cloud = Worker(Mock(pg=Mock())).source_client('sensoren')
        self.assertIs(type(cloud), SourceClient)
        cloud.close()
        injected = Mock()
        Worker(store, client_factory=injected).source_client('sensoren')
        injected.assert_called_once_with('sensoren')

    def test_local_collector_records_and_finishes_a_job(self):
        store = Store(self.path)
        run = store.enqueue([Rule('sensoren', 'ifm', 'SI5000', 'https://sensoren.ru/product/datchik_ifm_si5000/')])
        client = Mock()
        client.fetch.return_value = ('https://sensoren.ru/product/datchik_ifm_si5000/', 200, 'fixture')
        observation = Observation('priced', client.fetch.return_value[0], price='12.50', currency='RUB')
        adapter = Mock()
        adapter.parse.return_value = observation
        with patch.dict('price_monitor.worker.ADAPTERS', {'sensoren': adapter}):
            Worker(store, client_factory=Mock(return_value=client)).run(once=True)
        self.assertEqual(store.runs()[0]['state'], 'completed')
        self.assertEqual(store.results(run_id=run)[0]['price'], '12.50')
        self.assertEqual(Store(self.path).results(run_id=run)[0]['price'], '12.50')

    def test_local_entry_pages_ignore_cloud_secrets(self):
        from streamlit.testing.v1 import AppTest
        import streamlit as st
        with patch.dict(os.environ, {'PRICE_MONITOR_DB': str(self.path)}), \
             patch('price_monitor.cloud.EmbeddedWorker'), \
             patch('price_monitor.enrichment.EnrichmentWorker'), \
             patch('price_monitor.startup.CatalogMaintenance'), \
             patch('price_monitor.startup.atexit.register'), \
             patch('price_monitor.startup.open_cloud_services', side_effect=AssertionError('Cloud access')):
            st.cache_resource.clear()
            app = AppTest.from_file(str(ROOT / 'local_app.py'))
            app.secrets['database'] = {'host': 'unavailable.invalid'}
            app.query_params['workspace'] = 'prices'
            for section in ('home', 'collect', 'products', 'compare', 'runs', 'sources'):
                app.query_params['price_section'] = section
                app.run(timeout=30)
                self.assertFalse(app.exception, (section, list(app.exception)))
                self.assertFalse(app.error, (section, list(app.error)))
            st.cache_resource.clear()


if __name__ == '__main__':
    unittest.main()
