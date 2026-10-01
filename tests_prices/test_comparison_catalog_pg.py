import os
import unittest

import test_sensoren_payload_pg as fixtures
import test_comparison_catalog as common
from price_monitor.catalog_storage import CatalogRepository
from price_monitor.own_prices import OwnPrices
from test_group_comparison import sensor, workbook
from price_monitor.matching import Matcher
from price_monitor.own_prices import read_prices


@unittest.skipUnless(os.getenv('SKB_TEST_POSTGRES_URL'),'PostgreSQL integration settings absent')
class ComparisonCatalogPostgresTests(common.ComparisonCatalogTests):
    sql=classmethod(fixtures.SensorenPersistenceTests.sql.__func__)
    setUpClass=classmethod(fixtures.SensorenPersistenceTests.setUpClass.__func__)
    cleanup_schema=classmethod(fixtures.SensorenPersistenceTests.cleanup_schema.__func__)

    def setUp(self):
        self.sql('TRUNCATE rules,runs,product_documents,own_price_imports CASCADE')
        self.repo=CatalogRepository(settings={'fixture':True})
        self.repo.batch=lambda statements,params=None:self.sql(';'.join(statements) if isinstance(statements,list) else statements,params)
        self.seed()

    def test_price_import_is_atomic_readable_and_idempotent(self):
        metadata,rows=read_prices(workbook([['Test','00123',100.05]]),'test.xlsx','2026-10-01',Matcher([sensor()]))
        prices=OwnPrices(self.repo)
        self.assertTrue(prices.import_rows(metadata,rows))
        self.assertFalse(prices.import_rows(metadata,rows))
        actual=prices.current('2026-10-01')
        self.assertEqual(len(actual),1)
        self.assertEqual((actual[0]['article'],actual[0]['net_price'],actual[0]['gross_price']),('00123','100.05','122.06'))


if __name__=='__main__':unittest.main()
