from datetime import date
import unittest
from unittest.mock import patch

from price_monitor.comparison_groups import history_points
from price_monitor.price_charts import build_price_chart, chart_points, comparison_panels


def observation(stamp, price=122, **kwargs):
    return {'checked_at': stamp, 'price': price, 'currency': 'RUB', 'status': 'priced',
            '_specifications': {'price_terms': {'basis': 'gross', 'rate': 22, 'unit': 'piece'}}, **kwargs}


def competitor():
    return {'entry_id': 'competitor:1', 'rule_id': 1, 'model': 'A', 'brand': 'ТЕКО',
            'source': 'teko', 'is_ours': False, 'last_currency': 'RUB', 'last_price': 122}


class PriceChartTests(unittest.TestCase):
    def test_empty_and_invalid_prices_do_not_render_as_zero(self):
        points = [dict(date='2026-10-02', price=p) for p in (None, 0, -1, float('inf'), float('nan'), 'нет')]
        points += [dict(date='bad-date', price=100), dict(price=100), dict(date=42, price=100)]
        self.assertEqual(chart_points(points), [])
        self.assertIsNone(build_price_chart(points))

    def test_iso_strings_and_datetime_dates_are_sorted_by_utc(self):
        points = chart_points([dict(date='2026-10-02T10:00:00+00:00', price=200),
                               dict(date='2026-10-02T12:00:00+05:00', price=100),
                               dict(date=date(2026, 10, 1), price=50)])
        self.assertEqual([p['price'] for p in points], [50, 100, 200])
        self.assertEqual(points[1]['observed_at'], '02.10.2026 07:00 UTC')

    def test_single_point_has_visible_marker_valid_domain_and_nonnegative_axis(self):
        chart = build_price_chart([dict(date='2026-10-02', price=122, currency='RUB', unit='piece', price_basis='gross')])
        spec = chart.to_dict(validate=True)
        self.assertEqual(spec['layer'][0]['mark']['interpolate'], 'step-after')
        self.assertEqual(spec['layer'][1]['mark']['type'], 'point')
        scale = spec['layer'][0]['encoding']['y']['scale']
        self.assertTrue(scale['zero'])
        self.assertEqual(scale['domainMin'], 0)
        self.assertEqual(spec['layer'][0]['encoding']['y']['title'], 'Цена, ₽ / шт.')
        self.assertNotEqual(*spec['layer'][0]['encoding']['x']['scale']['domain'])
        rows = next(iter(spec['datasets'].values()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['price_label'], '122,00 ₽ / шт.')

    def test_currency_units_and_vat_are_not_overlaid(self):
        base = dict(date='2026-10-02', price=100, currency='RUB', unit='piece', price_basis='gross')
        values = [{**base, 'entry_id': 'A'}, {**base, 'entry_id': 'B'},
                  {**base, 'entry_id': 'C', 'currency': 'EUR'},
                  {**base, 'entry_id': 'D', 'unit': 'pack'},
                  {**base, 'entry_id': 'E', 'price_basis': 'net'},
                  {**base, 'entry_id': 'F', 'unit': ''}, {**base, 'entry_id': 'G', 'unit': ''}]
        panels = comparison_panels(values)
        self.assertEqual(sorted(map(len, panels)), [1, 1, 1, 1, 1, 2])

    def test_history_sorts_before_preserving_failed_collection_gaps(self):
        history = [observation('2026-10-02T10:00:00+00:00', 130),
                   observation('2026-10-02T08:00:00+00:00', status='network_error'),
                   observation('2026-10-02T12:00:00+05:00', 122)]
        points = history_points(competitor(), history, 'gross')
        self.assertEqual([p['price'] for p in points], [122, 130])
        self.assertEqual([p['segment'] for p in points], ['0', '1'])
        self.assertEqual(points[0]['currency'], 'RUB')
        self.assertEqual(points[0]['unit'], 'piece')

    def test_change_of_conditions_breaks_the_line(self):
        history = [observation('2026-10-01', 122), observation('2026-10-02', 100,
                   _specifications={'price_terms': {'basis': 'net', 'unit': 'piece'}})]
        points = history_points(competitor(), history, 'internet')
        self.assertEqual([p['segment'] for p in points], ['0', '1'])
        self.assertEqual(len(comparison_panels(points)), 2)

    def test_source_identity_reaches_historical_vat_resolution(self):
        record = {**competitor(), 'source': 'sensoren'}
        history = [observation('2026-10-01', 122, _specifications={})]
        points = history_points(record, history, 'gross')
        self.assertEqual(points[0]['price_basis'], 'gross')
        self.assertEqual(points[0]['price'], 122)

    def test_invalid_historical_date_is_skipped(self):
        points = history_points(competitor(), [observation('bad-date'), observation('2026-10-02')], 'gross')
        self.assertEqual(len(points), 1)

    def test_own_price_is_one_point_on_price_list_date(self):
        record = {'is_ours': True, 'entry_id': 'ours:A', 'model': 'A', 'brand': 'СКБ Индукция',
                  'own_price': {'net_price': '100', 'gross_price': '122', 'currency': 'RUB',
                                'vat_rate': '22', 'unit': 'piece', 'effective_date': '2026-10-01', 'source_name': 'price.xlsx'}}
        points = history_points(record, [], 'gross')
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['date'], '2026-10-01T00:00:00+00:00')
        self.assertEqual(points[0]['source'], 'price.xlsx')

    def test_reference_remains_explicitly_current_and_invalid_reference_is_ignored(self):
        points = [dict(date='2026-10-02', price=122)]
        spec = build_price_chart(points, reference=100).to_dict(validate=True)
        self.assertEqual(spec['layer'][-1]['mark']['type'], 'rule')
        self.assertIn('Текущая цена СКБ (ориентир)', str(spec['datasets']))
        self.assertEqual(len(build_price_chart(points, reference=-100).to_dict()['layer']), 2)

    def test_streamlit_renderer_accepts_one_point_and_empty_history(self):
        from price_monitor.group_comparison_ui import render_chart
        record = competitor()
        with patch('price_monitor.group_comparison_ui.st') as streamlit:
            render_chart([record], {record['entry_id']}, {1: [observation('2026-10-02')]},
                         'gross', (date(2026, 10, 1), date(2026, 10, 2)), record)
            streamlit.altair_chart.assert_called_once()
            streamlit.altair_chart.call_args.args[0].to_dict(validate=True)
        with patch('price_monitor.group_comparison_ui.st') as streamlit:
            render_chart([record], {record['entry_id']}, {}, 'gross',
                         (date(2026, 10, 1), date(2026, 10, 2)), record)
            streamlit.altair_chart.assert_not_called()
            streamlit.info.assert_called_once()


if __name__ == '__main__':
    unittest.main()
