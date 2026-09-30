import os
import unittest
import test_sensoren_payload_pg as fixtures
from test_retired_documents import check_retirement
from price_monitor.catalog_storage import CatalogRepository


@unittest.skipUnless(os.getenv('SKB_TEST_POSTGRES_URL') or os.getenv('PRICE_TEST_SECRETS'),'PostgreSQL integration settings absent')
class RetiredDocumentsPostgresTests(unittest.TestCase):
    sql=classmethod(fixtures.SensorenPersistenceTests.sql.__func__)
    setUpClass=classmethod(fixtures.SensorenPersistenceTests.setUpClass.__func__)
    cleanup_schema=classmethod(fixtures.SensorenPersistenceTests.cleanup_schema.__func__)

    def test_old_queue_fenced_in_postgres(self):
        repo=CatalogRepository(settings={'fixture':True})
        repo.batch=lambda statements,params=None:self.sql(';'.join(statements) if isinstance(statements,list) else statements,params)
        check_retirement(self,repo)
