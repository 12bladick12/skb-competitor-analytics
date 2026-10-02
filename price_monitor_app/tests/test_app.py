import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest
import streamlit as st

from price_monitor.storage import Store
from price_monitor.models import Rule,Observation
from support import TestDirectory


class AppTests(unittest.TestCase):
    def test_upload_start_and_cancel_from_ui(self):
        root=Path(__file__).parents[1]
        upload=SimpleNamespace(name='tasks.csv',getvalue=lambda:(root/'examples/tasks.csv').read_bytes())
        with TestDirectory() as tmp,patch.dict(os.environ,{'PRICE_MONITOR_DB':str(Path(tmp)/'ui.db')}),patch('streamlit.file_uploader',return_value=upload):
            st.cache_resource.clear()
            app=AppTest.from_file(str(root/'app.py'),default_timeout=30).run()
            self.assertFalse(app.exception)
            next(b for b in app.button if b.label=='Запустить сбор').click().run()
            self.assertFalse(app.exception)
            db=Store()
            self.assertEqual(db.runs()[0]['total'],11)
            next(b for b in app.button if b.label=='Остановить запуск').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(db.runs()[0]['state'],'cancelled')
            self.assertTrue(all(r['status']=='cancelled' for r in db.results()))
            st.cache_resource.clear()

    def test_empty_app_and_all_sections_with_saved_results(self):
        with TestDirectory() as tmp,patch.dict(os.environ,{'PRICE_MONITOR_DB':str(Path(tmp)/'ui.db')}):
            st.cache_resource.clear()
            app=AppTest.from_file(str(Path(__file__).parents[1]/'app.py'),default_timeout=30).run()
            self.assertFalse(app.exception)
            db=Store()
            rule=Rule('sensoren','ifm','SI5000','https://sensoren.ru/product/datchik_potoka_ifm_electronic_si5000/')
            run=db.enqueue([rule]);db.acquire('test');db.claim_run('test')
            job=db.pending(run)[0][0]
            db.record(job,Observation('priced',rule.url,price='29530.00',currency='RUB'),'test')
            db.finish(run);db.release('test')
            for page in ['Результаты','История модели','Источники','Сбор цен']:
                app.sidebar.radio[0].set_value(page).run()
                self.assertFalse(app.exception,page)
            st.cache_resource.clear()


if __name__=='__main__':unittest.main()
