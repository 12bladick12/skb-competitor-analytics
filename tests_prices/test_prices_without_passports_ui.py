import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]


class PricesWithoutPassportsUiTests(unittest.TestCase):
    def test_old_passport_route_and_price_pages_remain_usable(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as folder:
            with patch.dict(os.environ,{'PRICE_MONITOR_DB':str(Path(folder)/'ui.db')}):
                app=AppTest.from_string(f"import runpy\nrunpy.run_path({str(ROOT/'price_monitor'/'ui.py')!r},init_globals={{'CLOUD_MODE':False}})")
                app.query_params['price_section']='passports'
                app.run(timeout=30)
                self.assertEqual(len(app.exception),0)
                self.assertEqual(app.sidebar.radio(key='price_navigation').options,['Сбор цен','База товаров','Сравнение цен','Запуски'])
                self.assertEqual(app.sidebar.radio(key='price_navigation').value,'collect')
                for section in ('products','sources'):
                    app.query_params['price_section']=section;app.run(timeout=30)
                    self.assertEqual(len(app.exception),0)
                    self.assertFalse(any('паспорт' in item.label.lower() for item in app.button))
