import unittest
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from price_monitor.catalog_search import CatalogSearch, merge_selection
from price_monitor.matching_normalize import Sensor


def records():
    rows = [
        ('a', 'СКБ Индукция', 'И27 Д5', '00123', {'body_type': 'threaded', 'diameter': 18.0, 'output': 'PNP', 'function': 'NO'}),
        ('b', 'ТЕКО', 'ISB M18A-31P-5-L', '00234', {'body_type': 'threaded', 'diameter': 18.0, 'output': 'PNP', 'function': 'NC'}),
        ('c', 'Другой', 'И27 Д5', '01234', {'diameter': 18.0}),
        ('d', 'СКБ Индукция', 'И18-1,5', '00987', {'diameter': 12.0, 'output': 'NPN'}),
        ('e', 'BESKONTA', 'CAP-20', '00001', {'body_type': 'rectangular', 'output': 'PNP'}),
    ]
    return {entry: {'entry_id': entry, 'brand': brand, 'model': model, 'article': article,
                    'sensor': Sensor(entry, model, family='capacitive' if entry == 'e' else 'inductive', values=values)}
            for entry, brand, model, article, values in rows}


class CatalogSearchTests(unittest.TestCase):
    def setUp(self):
        self.records = records()
        self.search = CatalogSearch(self.records)

    def test_exact_article_preserves_leading_zeroes_and_brand_disambiguates(self):
        self.assertEqual(self.search.search('00123'), ['a'])
        self.assertEqual(self.search.resolve_paste('123')[0].status, 'partial')
        self.assertEqual(set(self.search.search('И27 Д5')), {'a', 'c'})
        self.assertEqual(self.search.search('скб индукция И27 Д5'), ['a'])

    def test_paste_separates_known_models_without_destroying_spaces_or_comma(self):
        parsed = self.search.resolve_paste('00123 ISB M18A-31P-5-L И18-1,5\nCAP-20\t00123;NO-SUCH-1, NO-SUCH-2')
        self.assertEqual([r.query for r in parsed], ['00123', 'ISB M18A-31P-5-L', 'И18-1,5', 'CAP-20', 'NO-SUCH-1', 'NO-SUCH-2'])
        self.assertEqual([r.status for r in parsed], ['exact'] * 4 + ['missing'] * 2)

    def test_ambiguity_and_partial_queries_are_never_silently_selected(self):
        rows = self.search.resolve_paste('И27 Д5\nISB\nunknown phrase')
        self.assertEqual([r.status for r in rows], ['ambiguous', 'partial', 'missing'])
        self.assertEqual(rows[-1].query, 'unknown phrase')

    def test_new_rows_enter_new_index_and_selection_has_no_duplicates(self):
        added = dict(self.records['b'], entry_id='f', model='NEW-01', article='00555')
        self.records['f'] = added
        refreshed = CatalogSearch(self.records)
        self.assertEqual(refreshed.search('00555'), ['f'])
        self.assertEqual(merge_selection(['a', 'deleted'], ['a', 'b', 'b'], self.records), ['a', 'b'])

    def test_all_active_characteristics_must_be_present_and_equal(self):
        self.assertEqual(self.search.filter({'diameter': 18.0, 'output': 'PNP', 'function': 'NO'}), ['a'])
        self.assertEqual(self.search.filter({'family': 'capacitive', 'output': 'PNP'}), ['e'])
        self.assertEqual(self.search.options('function', {'diameter': 18.0, 'output': 'PNP'}), ['NC', 'NO'])
        self.records['a']['sensor'].conflicts.add('output')
        self.assertEqual(CatalogSearch(self.records).filter({'output': 'PNP', 'function': 'NO'}), [])

    def test_source_dimensions_preserve_orientation_and_conflicts_are_excluded(self):
        row = self.records['e']
        row['props'] = {'Габаритные размеры, мм': '20 x 30 x 40'}
        search = CatalogSearch(self.records)
        self.assertEqual(search.filter({'dimensions': '20×30×40'}), ['e'])
        row['props']['Dimensions'] = '40 x 30 x 20'
        self.assertEqual(CatalogSearch(self.records).filter({'dimensions': '20×30×40'}), [])

    def test_large_paste_is_not_truncated(self):
        many = {str(i): dict(self.records['a'], entry_id=str(i), model=f'MODEL-{i}', article=f'{i:06}') for i in range(260)}
        results = CatalogSearch(many).resolve_paste('\n'.join(f'{i:06}' for i in range(260)))
        self.assertEqual(len(results), 260)
        self.assertTrue(all(row.status == 'exact' for row in results))


UI_SCRIPT = '''
import streamlit as st
from types import SimpleNamespace
from tests_prices.test_catalog_search import records
from price_monitor.catalog_search_ui import render_search
chosen = render_search(SimpleNamespace(records=records()))
st.write('Selected IDs: ' + ','.join(row['entry_id'] for row in chosen))
'''


class CatalogSearchUiTests(unittest.TestCase):
    def app(self):
        app = AppTest.from_string(UI_SCRIPT).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def test_autocomplete_multiple_models_and_paste_share_selection(self):
        app = self.app()
        app.multiselect(key='catalog_search_selection').set_value(['a', 'b']).run()
        self.assertFalse(app.exception)
        app.text_area(key='catalog_search_paste').set_value('00123\nCAP-20\nUNKNOWN').run()
        app.button(key='catalog_search_parse').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.multiselect(key='catalog_search_selection').value, ['a', 'b', 'e'])
        self.assertTrue(any('UNKNOWN' in item.value for item in app.warning))
        app.multiselect(key='catalog_search_selection').set_value([]).run()
        self.assertEqual(app.multiselect(key='catalog_search_selection').value, [])

    def test_characteristic_selection_and_reset_of_incompatible_filter(self):
        app = self.app()
        app.selectbox(key='catalog_filter_diameter').set_value(18.0).run()
        app.selectbox(key='catalog_filter_function').set_value('NO').run()
        app.button(key='catalog_add_all_characteristics').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.multiselect(key='catalog_search_selection').value, ['a'])
        app.selectbox(key='catalog_filter_diameter').set_value(12.0).run()
        self.assertIsNone(app.selectbox(key='catalog_filter_function').value)
        self.assertFalse(app.exception)

    def test_ambiguous_name_requires_an_explicit_choice(self):
        app = self.app()
        app.text_area(key='catalog_search_paste').set_value('И27 Д5').run()
        app.button(key='catalog_search_parse').click().run()
        self.assertEqual(app.multiselect(key='catalog_search_selection').value, [])
        choice = next(item for item in app.selectbox if str(item.key).startswith('catalog_resolve_'))
        choice.set_value('c').run()
        app.button(key=choice.key.replace('catalog_resolve_', 'catalog_add_')).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.multiselect(key='catalog_search_selection').value, ['c'])

    def test_result_pages_keep_all_models_and_reset_after_new_selection(self):
        script = '''
import streamlit as st
from types import SimpleNamespace
from unittest.mock import patch
from tests_prices.test_catalog_search import records
from price_monitor.group_comparison_ui import render_comparison
template = records()['a']
rows = {str(i): dict(template, entry_id=str(i), model='MODEL-'+str(i), article='ART-'+str(i)) for i in range(12)}
index = SimpleNamespace(records=rows)
library = SimpleNamespace(repo=SimpleNamespace(path='test-search'))
def group(anchor, *args):
    st.write('Rendered ID: '+anchor['entry_id'])
with patch('price_monitor.group_comparison_ui.comparison_index', return_value=index), patch('price_monitor.group_comparison_ui.OwnPrices') as prices, patch('price_monitor.group_comparison_ui.render_group', side_effect=group):
    prices.return_value.imports.return_value = []
    render_comparison(library)
'''
        app = AppTest.from_string(script).run(timeout=30)
        app.multiselect(key='catalog_search_selection').set_value([str(i) for i in range(12)]).run()
        self.assertFalse(app.exception)
        self.assertEqual(sum(item.value.startswith('Rendered ID:') for item in app.markdown), 5)
        app.number_input(key='catalog_search_result_page').set_value(3).run()
        self.assertEqual([item.value for item in app.markdown if item.value.startswith('Rendered ID:')], ['Rendered ID: 10', 'Rendered ID: 11'])
        self.assertEqual(len(app.multiselect(key='catalog_search_selection').value), 12)
        app.multiselect(key='catalog_search_selection').set_value(['0']).run()
        self.assertFalse(app.exception)
        self.assertEqual([item.value for item in app.markdown if item.value.startswith('Rendered ID:')], ['Rendered ID: 0'])


if __name__ == '__main__':
    unittest.main()
