import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch, call

from price_monitor.scope import refresh_scope
from price_monitor.startup import CatalogMaintenance, DatabaseSchemaError, open_cloud_services, verify_schema


class CloudStartupTests(unittest.TestCase):
    def store(self,ready=True):
        repository=Mock()
        repository.batch.return_value=[{'version':1,'ready':ready,'fences':2}]
        return SimpleNamespace(catalog=repository,pg=Mock())

    def test_open_does_not_run_migrations_or_synchronous_backfill(self):
        store=self.store()
        with patch('price_monitor.startup.Store',return_value=store) as factory, \
             patch('price_monitor.cloud.retire_legacy_workers'), \
             patch('price_monitor.cloud.EmbeddedWorker') as worker, \
             patch('price_monitor.enrichment.EnrichmentWorker'), \
             patch('price_monitor.startup.CatalogMaintenance') as maintenance, \
             patch('price_monitor.startup.atexit.register'):
            actual,controller=open_cloud_services({'host':'test-only'})
        factory.assert_called_once_with(postgres={'host':'test-only'},initialize=False)
        self.assertIs(actual,store)
        self.assertIs(controller,worker.return_value)
        self.assertEqual(store.catalog.batch.call_count,1)
        sql=store.catalog.batch.call_args.args[0]
        self.assertTrue(sql.startswith('SELECT '))
        self.assertNotIn('CREATE ',sql)
        maintenance.assert_called_once_with(store.catalog)

    def test_unready_schema_closes_pool_without_starting_workers(self):
        store=self.store(ready=False)
        with patch('price_monitor.startup.Store',return_value=store), \
             patch('price_monitor.cloud.retire_legacy_workers'), \
             patch('price_monitor.cloud.EmbeddedWorker') as worker:
            with self.assertRaises(DatabaseSchemaError):open_cloud_services({})
        store.pg.close.assert_called_once()
        worker.assert_not_called()

    def test_partially_started_workers_are_closed_on_error(self):
        store=self.store()
        with patch('price_monitor.startup.Store',return_value=store), \
             patch('price_monitor.cloud.retire_legacy_workers'), \
             patch('price_monitor.cloud.EmbeddedWorker') as worker, \
             patch('price_monitor.enrichment.EnrichmentWorker',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):open_cloud_services({})
        worker.return_value.close.assert_called_once()
        store.pg.close.assert_called_once()

    def test_retirement_fences_and_schema_version_are_required(self):
        for result in ([],[{'version':2,'ready':True,'fences':2}],[{'version':1,'ready':True,'fences':1}]):
            repository=Mock();repository.batch.return_value=result
            with self.assertRaises(DatabaseSchemaError):verify_schema(repository)

    def test_scope_backfill_stops_after_one_batch(self):
        repository=Mock(settings={})
        repository.batch.side_effect=[[
            {'id':1,'details_hash':'source','updated_at':'2026-10-02',
             'details_json':json.dumps({'manufacturer':'ТЕКО','attributes':[]})}
        ],[]]
        self.assertEqual(refresh_scope(repository,max_batches=1),1)
        self.assertEqual(repository.batch.call_count,2)
        self.assertIn('NOT EXISTS',repository.batch.call_args_list[0].args[0])
        self.assertIn('INSERT INTO product_scope',repository.batch.call_args_list[1].args[0])

    def test_background_failure_retries_without_disclosing_error_details(self):
        with patch('price_monitor.startup.threading.Thread'):
            maintenance=CatalogMaintenance(Mock())
        maintenance.stopping=Mock()
        maintenance.stopping.is_set.side_effect=[False,False,True]
        with patch('price_monitor.scope.refresh_scope',side_effect=[RuntimeError('private details'),0]) as refresh, \
             self.assertLogs('price_monitor.startup',level='WARNING') as logs:
            maintenance._loop()
        self.assertEqual(refresh.call_count,2)
        self.assertEqual(maintenance.stopping.wait.call_args_list,[call(60),call(300)])
        self.assertIn('RuntimeError',logs.output[0])
        self.assertNotIn('private details',logs.output[0])

    def test_connection_error_offers_a_working_retry_without_blame_or_private_details(self):
        from streamlit.testing.v1 import AppTest
        path=Path(__file__).resolve().parents[1]/'price_monitor/ui.py'
        script=f'''
import streamlit as st
import runpy
from unittest.mock import patch
def unavailable(*args):
    st.session_state['connection_attempts']=st.session_state.get('connection_attempts',0)+1
    raise TimeoutError('private details')
with patch.object(st,'secrets',{{'database':{{'host':'test-only'}}}}), patch('price_monitor.startup.open_cloud_services',side_effect=unavailable):
    runpy.run_path({str(path)!r},init_globals={{'CLOUD_MODE':True}})
'''
        app=AppTest.from_string(script).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['connection_attempts'],1)
        self.assertNotIn('private details',app.error[0].value)
        self.assertNotIn('проверить настройки',app.error[0].value)
        app.button(key='retry_price_connection').click().run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['connection_attempts'],2)


if __name__=='__main__':unittest.main()
