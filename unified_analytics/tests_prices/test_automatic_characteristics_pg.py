import os
import unittest
import test_sensoren_payload_pg as fixtures
import test_automatic_characteristics as common
from price_monitor.catalog_storage import CatalogRepository


@unittest.skipUnless(os.getenv('SKB_TEST_POSTGRES_URL') or os.getenv('PRICE_TEST_SECRETS'),'PostgreSQL integration settings absent')
class AutomaticCharacteristicsPostgresTests(common.AutomaticCharacteristicsTests):
    sql=classmethod(fixtures.SensorenPersistenceTests.sql.__func__)
    setUpClass=classmethod(fixtures.SensorenPersistenceTests.setUpClass.__func__)
    cleanup_schema=classmethod(fixtures.SensorenPersistenceTests.cleanup_schema.__func__)

    def setUp(self):
        self.sql('TRUNCATE rules,product_documents CASCADE')
        self.repo=CatalogRepository(settings={'fixture':True})
        self.repo.batch=lambda statements,params=None:self.sql(';'.join(statements) if isinstance(statements,list) else statements,params)
        self.seed()

    def tearDown(self):pass
