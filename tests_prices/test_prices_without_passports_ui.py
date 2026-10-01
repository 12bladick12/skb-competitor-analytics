import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]


class PricesWithoutPassportsUiTests(unittest.TestCase):
    def test_offline_sensoren_does_not_show_old_phase_as_current_work(self):
        from price_monitor.storage import Store
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as folder:
            path=Path(folder)/'offline.db'
            store=Store(path)
            run=store.enqueue_catalog({'sensoren':['ifm']})
            with store.connect() as conn:
                conn.execute("UPDATE runs SET state='running' WHERE id=?",(run,))
                conn.execute("UPDATE catalog_sources SET state='running' WHERE run_id=?",(run,))
                conn.execute("INSERT INTO external_sources(source,enabled,owner,heartbeat,protocol) VALUES('sensoren',1,'old-agent',?,2)",(time.time()-3600,))
                conn.execute("INSERT INTO worker_lease(id,owner,heartbeat) VALUES(1,'router-test',?)",(time.time(),))
                conn.execute("INSERT INTO collector_health(source,owner,run_id,phase,url,phase_started,activity_at,completed_at,recoveries,detail) VALUES('sensoren','old-agent',?,'download','',?,?,?,0,'')",(run,*([time.time()-3600]*3)))
            with patch.dict(os.environ,{'PRICE_MONITOR_DB':str(path)}):
                # Use a unique services cache key for this isolated database.
                app=AppTest.from_string(f"import streamlit as st\nst.cache_resource.clear()\nimport runpy\nrunpy.run_path({str(ROOT/'price_monitor'/'ui.py')!r},init_globals={{'CLOUD_MODE':False}})")
                app.run(timeout=30)
                self.assertEqual(len(app.exception),0)
                warnings=[item.value for item in app.warning]
                self.assertTrue(any('ждёт подключения' in text for text in warnings))
                self.assertFalse(any('не завершён' in text for text in warnings))
                self.assertTrue(any('Облачный сборщик подключён' in item.value for item in app.caption))
                self.assertEqual(app.dataframe[0].value.iloc[0]['Текущий этап'],'Ожидание внешнего сборщика')

    def test_old_passport_route_and_price_pages_remain_usable(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as folder:
            with patch.dict(os.environ,{'PRICE_MONITOR_DB':str(Path(folder)/'ui.db')}):
                app=AppTest.from_string(f"import streamlit as st\nst.cache_resource.clear()\nimport runpy\nrunpy.run_path({str(ROOT/'price_monitor'/'ui.py')!r},init_globals={{'CLOUD_MODE':False}})")
                app.query_params['price_section']='passports'
                app.run(timeout=30)
                self.assertEqual(len(app.exception),0)
                self.assertEqual(app.sidebar.radio(key='price_navigation').options,['Сбор цен','База товаров','Сравнение цен','Запуски'])
                self.assertEqual(app.sidebar.radio(key='price_navigation').value,'collect')
                for section in ('products','sources'):
                    app.query_params['price_section']=section;app.run(timeout=30)
                    self.assertEqual(len(app.exception),0)
                    self.assertFalse(any('паспорт' in item.label.lower() for item in app.button))
