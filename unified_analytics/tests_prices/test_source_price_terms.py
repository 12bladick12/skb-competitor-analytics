"""Source corrections apply on read without changing identity or raw evidence."""
import copy
import json
import unittest

from price_monitor.adapters import ADAPTERS
from price_monitor.automatic_price_terms import current_terms
from price_monitor.models import Rule
from price_monitor.price_terms import price_views, terms_for
from price_monitor.product_labels import display_article, normalize_article


class SensorenPriceTermsTests(unittest.TestCase):
    def test_new_observation_has_gross_price_without_assumed_rate_or_unit(self):
        model = 'LR18XBF08DPOY-E2'
        url = 'https://sensoren.ru/product/test/'
        html = f'''<h1>Индуктивный датчик LANBAO {model}</h1>
            <div class="product-info"><div class="product-info__all-order">
            <div class="product-info__all-order-price"><b class="price">122 руб.</b></div>
            </div></div>'''
        result = ADAPTERS['sensoren'].parse(Rule('sensoren', 'LANBAO', model, url), html)
        self.assertEqual(result.status, 'priced')
        terms = json.loads(result.details_json)['price_terms']
        self.assertEqual(terms['basis'], 'gross')
        self.assertIsNone(terms['rate'])
        self.assertEqual(terms['unit'], '')
        self.assertEqual(terms['source_url'], url)
        self.assertIn('122 руб.', terms['evidence'])
        self.assertEqual(terms['basis_confirmation_source'], 'user')
        self.assertEqual(price_views(result.price, terms), {'internet': 122., 'gross': 122., 'net': None})

    def test_saved_json_and_dictionary_forms_retain_original_evidence(self):
        raw_terms = {'basis': 'unknown', 'rate': 22, 'unit': 'piece',
                     'evidence': '122 руб. НДС 22% / шт.', 'checked_at': '2026-09-30'}
        details = {'price_terms': raw_terms}
        for field in ('_specifications', 'details_json'):
            for payload in (details, json.dumps(details)):
                with self.subTest(field=field, type=type(payload).__name__):
                    row = {'source': 'sensoren', field: payload}
                    before = copy.deepcopy(row)
                    terms = terms_for(row)
                    self.assertEqual(terms['basis'], 'gross')
                    self.assertEqual(terms['rate'], 22)
                    self.assertEqual(terms['checked_at'], '2026-09-30')
                    self.assertEqual(terms['evidence'], raw_terms['evidence'])
                    self.assertEqual(price_views(122, terms)['net'], 100)
                    self.assertEqual(row, before)

    def test_old_net_label_is_corrected_without_rewriting_source(self):
        row = {'source': 'sensoren', '_specifications': {'price_terms':
               {'basis': 'net', 'rate': 20, 'evidence': '120 руб. без НДС 20%'}}}
        before = copy.deepcopy(row)
        terms = terms_for(row)
        self.assertEqual((terms['basis'], terms['rate']), ('gross', 20))
        self.assertEqual(price_views(120, terms)['gross'], 120)
        self.assertEqual(row, before)

    def test_history_can_identify_sensoren_by_exact_source_url(self):
        for key in ('url', 'product_url'):
            terms = terms_for({key: 'https://www.sensoren.ru/product/test/', 'price_text': '122 руб.'})
            self.assertEqual(terms['basis'], 'gross')
        terms = terms_for({'_specifications': {'price_terms': {'basis': 'unknown',
                          'source_url': 'https://sensoren.ru/product/test/'}}})
        self.assertEqual(terms['basis'], 'gross')
        for url in ('https://sensoren.ru.example.org/', 'https://example.org/sensoren.ru/'):
            self.assertEqual(terms_for({'url': url})['basis'], 'unknown')

    def test_last_quote_and_automatic_terms_keep_source_and_own_rate(self):
        for key in ('_price_snapshot_terms', '_automatic_price_terms'):
            for payload in ({'basis': 'unknown', 'rate': 20}, json.dumps({'basis': 'unknown', 'rate': 20})):
                with self.subTest(key=key, type=type(payload).__name__):
                    row = {'source': 'sensoren', key: payload,
                           '_specifications': {'price_terms': {'basis': 'gross', 'rate': 22}}}
                    terms = current_terms(row)
                    self.assertEqual((terms['basis'], terms['rate']), ('gross', 20))
                    self.assertEqual(price_views(120, terms)['net'], 100)

    def test_unknown_rate_stays_unknown_in_current_quote(self):
        row = {'source': 'sensoren', '_price_snapshot_terms': {'basis': 'unknown'},
               '_specifications': {'price_terms': {'basis': 'gross', 'rate': 22}}}
        self.assertIsNone(price_views(120, current_terms(row))['net'])

    def test_invalid_json_is_safe_and_other_sources_are_unchanged(self):
        for payload in ('broken', 'null', '[]', '42', {'price_terms': '[]'}):
            self.assertEqual(terms_for({'source': 'sensoren', 'details_json': payload})['basis'], 'gross')
        self.assertEqual(terms_for({'source': 'teko', 'price_text': '120 руб.'})['basis'], 'unknown')
        self.assertEqual(terms_for({'source': 'beskonta', 'price_text': '100 руб. без НДС 22%'})['basis'], 'net')


class TekoLabelTests(unittest.TestCase):
    def test_marking_is_shown_without_type_while_row_identity_is_untouched(self):
        raw = 'Выключатель индуктивный ISB A0B-31N-0,8'
        row = {'source': 'teko', 'article': raw, 'title': raw, 'rule_id': 47}
        before = copy.deepcopy(row)
        rule = Rule('teko', 'ТЕКО', raw, 'https://teko-com.ru/catalog/product/test/')
        key = rule.key
        self.assertEqual(display_article(row), 'ISB A0B-31N-0,8')
        self.assertEqual(row, before)
        self.assertEqual(rule.key, key)

    def test_supported_prefixes_keep_suffixes_and_special_execution_codes(self):
        cases = {
            'Выключатель индуктивный взрывозащищенный ISN E2A-31P-4-L': 'ISN E2A-31P-4-L',
            'Выключатель индуктивный морского исполнения ISB A2A-31P-4-L-C': 'ISB A2A-31P-4-L-C',
            'Выключатель индуктивный для автомобильного транспорта ISB A2A-31P-4-L': 'ISB A2A-31P-4-L',
            'ТЕКО Выключатель индуктивный ISB A2A-31P-4-L': 'ISB A2A-31P-4-L',
            'Выключатель емкостный CSB A41A5-01G-6-L': 'CSB A41A5-01G-6-L',
            'Выключатель индуктивный\u00a0ISB A0B-31N-0,8': 'ISB A0B-31N-0,8',
            'CC S19-3/S4-0,5': 'CC S19-3/S4-0,5',
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(normalize_article('teko', title), expected)

    def test_other_sources_generic_titles_and_unknown_labels_are_preserved(self):
        raw = 'Выключатель индуктивный ISB A0B-31N-0,8'
        self.assertEqual(normalize_article('sensor', raw), raw)
        for title in ('Выключатель индуктивный', 'Неизвестный тип ISB A0B-31N-0,8'):
            self.assertEqual(normalize_article('teko', title), title)
        self.assertEqual(display_article({'manufacturer': 'ТЕКО', 'article': raw}), 'ISB A0B-31N-0,8')


if __name__ == '__main__':
    unittest.main()
