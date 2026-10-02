"""Real PostgreSQL transactions in CI's isolated, disposable database only."""
import os
import unittest
from urllib.parse import urlsplit

from cloud.drive_store import StorageError
from cloud.library import Repository


@unittest.skipUnless(os.environ.get('SKB_TEST_POSTGRES_URL'), 'Isolated PostgreSQL service required')
class PostgresImportTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        url=os.environ['SKB_TEST_POSTGRES_URL']
        parsed=urlsplit(url)
        if parsed.hostname not in ('localhost','127.0.0.1') or parsed.path!='/skb_test':
            raise RuntimeError('Only the disposable localhost skb_test database is allowed')
        self.repository=Repository({'cloud':{'database_url':'postgresql://test:TEST@ep-test.neon.tech/db'}},
            connect=lambda **kw: psycopg.connect(url,autocommit=True))
        self.repository.initialize()
        with self.repository.transaction(write=True) as conn:
            conn.execute('TRUNCATE skb_analytics.state,skb_analytics.assets,skb_analytics.records,skb_analytics.imports CASCADE')

    def catalog(self):
        return {'manifest':{'source_created_at':'2026-09-18'},'assets':{},'records':[
            {'kind':'event','key':'1','payload':{'title':'Ручной текст','version':'unchanged'}}]}

    def test_failed_import_is_invisible_and_does_not_leave_partial_rows(self):
        broken=self.catalog()
        broken['records'].append({'kind':'event','key':None,'payload':{}})
        with self.assertRaises(StorageError):
            self.repository.activate(broken,{})
        self.assertIsNone(self.repository.load())
        with self.repository.transaction() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM skb_analytics.imports').fetchone()[0],0)

    def test_import_is_idempotent_and_another_import_cannot_overwrite_data(self):
        catalog=self.catalog()
        catalog['records'][0]['payload']['untrusted_text']="Quotes ' ; $migration$ $$ and Unicode: Ёж"
        ident=self.repository.activate(catalog,{})
        self.assertEqual(self.repository.activate(catalog,{}),ident)
        self.assertEqual(self.repository.load()['event']['1'],catalog['records'][0]['payload'])
        catalog['records'][0]['payload']['title']='Overwrite attempt'
        with self.assertRaises(ValueError):
            self.repository.activate(catalog,{})
        self.assertEqual(self.repository.load()['event']['1']['title'],'Ручной текст')
