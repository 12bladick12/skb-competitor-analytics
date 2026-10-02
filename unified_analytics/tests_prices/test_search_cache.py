from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from price_monitor.search_cache import SearchCatalogCache,IdentityIndex


def product():
    return {'rule_id':1,'manufacturer':'LANBAO','source':'sensoren','article':'LR18XBF08DPOY-E2',
            'product_url':'https://example.invalid/model','title':'Sensor','category':'Inductive sensor',
            '_catalog_details_hash':'immutable','_specifications':{'attributes':[{'name':'Output type','value':'PNP'}]}}


class SearchCacheTests(unittest.TestCase):
    def setUp(self):
        self.library=Mock()
        self.library.iter_search_identities.side_effect=lambda:iter([product()])
        self.library.iter_search_products.side_effect=lambda:iter([product()])
        self.library.comparison_reference.side_effect=lambda row:row
        self.cache=SearchCatalogCache(self.library,catalog_loader=lambda:(None,SimpleNamespace(products=[])),clock=lambda:100)
        prices=patch('price_monitor.own_prices.OwnPrices.current',return_value=[])
        prices.start();self.addCleanup(prices.stop)

    def test_names_are_published_before_characteristics_without_inventing_values(self):
        def cards():
            state=self.cache.state()
            self.assertIsInstance(state.index,IdentityIndex)
            self.assertFalse(state.complete)
            row=state.index.records['competitor:1']
            self.assertEqual(row['sensor'].values,{})
            self.assertEqual(row['model'],'LR18XBF08DPOY-E2')
            return iter([product()])
        self.library.iter_search_products.side_effect=cards
        self.cache._build()
        state=self.cache.state()
        self.assertTrue(state.complete)
        self.assertEqual(state.generation,2)
        self.assertGreater(state.updated_at,0)
        self.assertFalse(state.loading)
        self.library.comparison_prices.assert_not_called()

    def test_failed_refresh_keeps_complete_catalog_and_delays_retry(self):
        self.cache._build()
        good=self.cache.state()
        self.library.iter_search_products.side_effect=RuntimeError('private connection details')
        with self.assertLogs('price_monitor.search_cache',level='WARNING') as logs:self.cache._build()
        failed=self.cache.state()
        self.assertIs(failed.index,good.index)
        self.assertTrue(failed.complete)
        self.assertEqual(failed.generation,good.generation)
        self.assertEqual(failed.error,'RuntimeError')
        self.assertNotIn('private connection details',''.join(logs.output))
        with patch('price_monitor.search_cache.threading.Thread') as thread:self.cache.request_refresh()
        thread.assert_not_called()
        self.assertEqual(self.cache.next_refresh,130)

    def test_one_refresh_at_a_time_and_manual_retry_is_available(self):
        self.cache.next_refresh=1000
        with patch('price_monitor.search_cache.threading.Thread') as thread:
            self.cache.request_refresh()
            thread.assert_not_called()
            self.cache.request_refresh(force=True)
            self.cache.request_refresh(force=True)
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
        self.assertTrue(self.cache.state().loading)

    def test_failed_initial_characteristics_preserve_name_search(self):
        self.library.iter_search_products.side_effect=TimeoutError('private')
        with self.assertLogs('price_monitor.search_cache',level='WARNING'):self.cache._build()
        state=self.cache.state()
        self.assertIsInstance(state.index,IdentityIndex)
        self.assertFalse(state.complete)
        self.assertEqual(len(state.index.records),1)


if __name__=='__main__':unittest.main()
