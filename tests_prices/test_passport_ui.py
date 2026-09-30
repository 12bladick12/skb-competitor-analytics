"""UI smoke test: state and review screens remain usable without cloud storage."""
import tempfile
from pathlib import Path
import unittest
from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]


class PassportUiTests(unittest.TestCase):
    def test_pending_document_screen_and_manual_retry(self):
        (ROOT/'data').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as tmp:
            db=str(Path(tmp)/'ui.sqlite3')
            source=f'''
import streamlit as st
from price_monitor.storage import Store
from price_monitor.passport_ui import render_page
import price_monitor.passport_ui as ui
ui.settings=lambda:{{}}
store=Store({db!r})
store.catalog.batch("INSERT INTO rules VALUES(1,'key','megak','МЕГА-К','TEST','https://mega-k.com/products/test','','2026-09-30') ON CONFLICT(id) DO NOTHING")
store.catalog.batch("INSERT INTO passport_products(rule_id) VALUES(1) ON CONFLICT(rule_id) DO NOTHING")
render_page(store.catalog)
'''
            app=AppTest.from_string(source).run(timeout=20)
            self.assertEqual(len(app.exception),0)
            self.assertTrue(any('хранилище' in item.value for item in app.info))
            app.button(key='passport_refresh_1').click().run(timeout=20)
            self.assertEqual(len(app.exception),0)
            self.assertTrue(any('очередь' in item.value for item in app.success))
