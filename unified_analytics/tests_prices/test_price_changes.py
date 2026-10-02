import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from price_monitor.library import Library
from price_monitor.price_changes import calculate_price_changes, change_table, history_overview, load_price_changes
from price_monitor.storage import Store


TERMS = {"basis": "gross", "rate": 20, "unit": "piece"}


def quote(price, checked_at, **extra):
    return {"id": 1, "rule_id": 1, "source": "beskonta", "manufacturer": "BESKONTA", "article": "001-A",
            "product_url": "https://beskonta.ru/product/001-a/", "url": "https://beskonta.ru/product/001-a/",
            "price": price, "checked_at": checked_at, "status": "priced", "currency": "RUB",
            "_specifications": {"price_terms": TERMS}, **extra}


class PriceChangesTests(unittest.TestCase):
    def test_previous_quote_before_period_and_exclusive_end(self):
        rows = [quote("100", "2026-09-30T10:00:00Z"), quote("120", "2026-10-01", id=2),
                quote("90", "2026-10-02T23:59:59+00:00", id=3), quote("70", "2026-10-03", id=4)]
        report = calculate_price_changes(reversed(rows), "2026-10-01", "2026-10-03")
        self.assertEqual(len(report.events), 2)
        lower, higher = report.events
        self.assertEqual((lower["direction"], lower["change"], lower["change_percent"]), ("decrease", 30, 25))
        self.assertEqual((higher["previous_price"], higher["price"], higher["change_percent"]), (100, 120, 20))
        self.assertTrue(higher["previous_checked_at"].startswith("2026-09-30"))

    def test_missing_prices_failures_duplicates_and_invalid_numbers_are_not_changes(self):
        rows = [quote("100", "2026-09-01")]
        for index, (price, status) in enumerate([(None, "network_error"), ("1", "parse_error"),
                                               ("0", "priced"), ("NaN", "priced"), ("-12", "priced"),
                                               ("Infinity", "priced"), ("100.00", "priced")], start=2):
            rows.append(quote(price, f"2026-09-{index:02d}", id=index, status=status))
        rows.append(quote("110", "2026-10-01", id=20))
        report = calculate_price_changes(rows)
        self.assertEqual(len(report.events), 1)
        self.assertEqual(report.events[0]["previous_price"], 100)
        self.assertEqual(report.events[0]["change"], 10)

    def test_changed_or_unknown_conditions_are_not_compared(self):
        original = quote("100", "2026-09-01")
        for extra in [{"currency": "USD"}, {"currency": None},
                      {"_specifications": {"price_terms": {**TERMS, "basis": "net"}}},
                      {"_specifications": {"price_terms": {**TERMS, "basis": "unknown"}}},
                      {"_specifications": {"price_terms": {**TERMS, "rate": 22}}},
                      {"_specifications": {"price_terms": {**TERMS, "unit": "pack"}}},
                      {"_specifications": {"price_terms": {**TERMS, "unit": ""}}}]:
            with self.subTest(extra=extra):
                result = calculate_price_changes([original, quote("120", "2026-10-01", id=2, **extra)])
                self.assertEqual(result.events, [])
                self.assertEqual(result.incompatible_pairs, 1)

    def test_returning_currency_does_not_bridge_different_intermediate_quote(self):
        rows = [quote("100", "2026-09-01"), quote("20", "2026-09-02", id=2, currency="USD"),
                quote("120", "2026-09-03", id=3), quote("130", "2026-09-04", id=4)]
        result = calculate_price_changes(rows)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0]["previous_price"], 120)
        self.assertEqual(result.incompatible_pairs, 2)

    def test_source_model_and_product_card_do_not_cross(self):
        original = quote("100", "2026-09-01")
        for extra in [{"source": "sensor"}, {"rule_id": 2}, {"article": "002-A"},
                      {"manufacturer": "OTHER"}, {"product_url": "https://beskonta.ru/product/other/"}]:
            self.assertEqual(calculate_price_changes([original, quote("120", "2026-10-01", **extra)]).events, [])

    def test_first_observation_is_not_a_change_and_decimal_subtraction_is_exact(self):
        self.assertEqual(calculate_price_changes([quote("100", "2026-09-01")]).events, [])
        result = calculate_price_changes([quote("100.10", "2026-09-01"), quote("100.20", "2026-09-02", id=2)])
        self.assertEqual(result.events[0]["change"], .1)

    def test_sensoren_historical_rows_are_gross_without_inventing_a_rate(self):
        first = quote("100", "2026-09-01", source="sensoren", _specifications={}, price_text="100 руб. / шт")
        last = {**first, "id": 2, "price": "120", "checked_at": "2026-10-01"}
        event = calculate_price_changes([first, last]).events[0]
        self.assertEqual(event["tax_basis"], "gross")
        self.assertIsNone(event["vat_rate"])

    def test_display_and_export_preserve_designation_and_positive_magnitudes(self):
        events = calculate_price_changes([quote("100", "2026-09-01"), quote("75", "2026-10-01", id=2)]).events
        row = change_table(events)[0]
        self.assertEqual(row["Маркировка"], "001-A")
        self.assertEqual(row["Направление"], "Снижение")
        self.assertEqual((row["Величина изменения"], row["Изменение, %"]), (25, 25))
        self.assertEqual(row["ID предыдущего наблюдения"], 1)


class PriceChangesRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Store(Path(self.tmp.name) / "prices.sqlite3").catalog
        self.repo.batch([
            "INSERT INTO rules(id,rule_key,source,manufacturer,article,product_url,url_template,created_at) VALUES(1,'one','beskonta','BESKONTA','001-A','https://beskonta.ru/product/001-a/','','2026-09-01'),(2,'two','beskonta','BESKONTA','002-A','https://beskonta.ru/product/002-a/','','2026-09-01')",
            "INSERT INTO runs(id,state,created_at) VALUES(1,'completed','2026-09-01'),(2,'completed','2026-10-01'),(3,'completed','2026-10-02')",
            "INSERT INTO jobs(id,run_id,rule_id,state) VALUES(1,1,1,'done'),(2,2,1,'done'),(3,3,1,'done'),(4,2,2,'done')",
            "INSERT INTO product_documents(fingerprint,details_json) VALUES('old',%(old)s),('new',%(new)s)",
            "INSERT INTO product_index(rule_id,title,category,search_text,details_hash,attributes_count,updated_at) VALUES(1,'001-A','','001-A','new',0,'2026-10-02')",
        ], {"old": json.dumps({"price_terms": TERMS}), "new": json.dumps({"price_terms": {**TERMS, "basis": "net"}})})
        for oid, job, price, checked_at, status in [(1, 1, "100", "2026-09-30", "priced"),
                                                   (2, 2, "120", "2026-10-01", "priced"),
                                                   (3, 3, None, "2026-10-02", "network_error"),
                                                   (4, 4, "99", "2026-10-01", "priced")]:
            self.repo.batch("""INSERT INTO observations(id,job_id,status,url,title,price,currency,availability,price_text,
                availability_text,detail,checked_at,response_hash,details_json)
                VALUES(%(id)s,%(job)s,%(status)s,'https://beskonta.ru/product/001-a/','',%(price)s,'RUB','','','','',%(checked)s,'','{"ref":"old"}')""",
                            {"id": oid, "job": job, "price": price, "checked": checked_at, "status": status})
        self.library = Library(self.repo)

    def test_load_is_read_only_uses_previous_snapshot_and_avoids_single_point_cards(self):
        original = self.repo.batch
        calls = []
        def read(sql, params=None):
            self.assertIsInstance(sql, str)
            self.assertTrue(sql.lstrip().startswith("SELECT "))
            calls.append((sql, params))
            return original(sql, params)
        with patch.object(self.repo, "batch", side_effect=read):
            overview = history_overview(self.library)
            report = load_price_changes(self.library, "2026-10-01", "2026-10-03")
        self.assertEqual(overview[0]["manufacturer"], "BESKONTA")
        self.assertEqual((report.observations_in_period, report.models_in_period, report.models_with_history), (2, 2, 1))
        self.assertEqual(len(report.events), 1)
        self.assertEqual(report.events[0]["tax_basis"], "gross")
        history_call = next((sql, params) for sql, params in calls if "SELECT j.rule_id,j.run_id" in sql)
        self.assertEqual(history_call[1]["id0"], 1)
        self.assertNotIn("id1", history_call[1])
        self.assertNotIn("start", history_call[1])

    def test_filters_and_literal_search(self):
        self.assertEqual(len(load_price_changes(self.library, "2026-10-01", "2026-10-02", query="001-A").events), 1)
        for kwargs in [{"manufacturer": "Other"}, {"query": "002-A"}, {"query": "%"}, {"query": "' OR 1=1"}]:
            self.assertEqual(load_price_changes(self.library, "2026-10-01", "2026-10-02", **kwargs).events, [])

    def test_bad_period_is_rejected_before_query(self):
        with self.assertRaises(ValueError):
            load_price_changes(self.library, "2026-10-03", "2026-10-01")

    def test_ui_renders_events_filters_exports_and_read_error(self):
        from streamlit.testing.v1 import AppTest
        script = '''
from price_monitor.catalog_storage import CatalogRepository
from price_monitor.library import Library
from price_monitor.price_changes_ui import render_price_changes
render_price_changes(Library(CatalogRepository(path=DATABASE_PATH)))
'''.replace("DATABASE_PATH", repr(str(self.repo.path)))
        app = AppTest.from_string(script).run()
        app.date_input(key="price_changes_period").set_value((__import__("datetime").date(2026, 10, 1),
                                                               __import__("datetime").date(2026, 10, 2))).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(app.dataframe[0].value.iloc[0]["Величина изменения"], 20)
        self.assertEqual(len(app.get("download_button")), 2)
        app.selectbox(key="price_changes_direction").set_value("decrease").run()
        self.assertEqual(len(app.dataframe), 0)
        self.assertIn("направления", app.info[0].value)
        with patch("price_monitor.price_changes_ui.history_overview", side_effect=RuntimeError("read failed")):
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 1)
        self.assertIn("Не удалось прочитать", app.error[0].value)


if __name__ == "__main__":
    unittest.main()
