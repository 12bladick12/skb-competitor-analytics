"""Atomic draft persistence against CI's disposable PostgreSQL service."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import unittest
from urllib.parse import urlsplit

from cloud.drafts import DraftStore
from cloud.draft_rules import DraftConflict, create_payload
from cloud.library import Repository
from test_cloud_drafts import fixture, draft_fixture


@unittest.skipUnless(os.environ.get('SKB_TEST_POSTGRES_URL'), 'Isolated PostgreSQL service required')
class DraftPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        uri=os.environ['SKB_TEST_POSTGRES_URL']
        parsed=urlsplit(uri)
        if parsed.hostname not in ('localhost','127.0.0.1') or parsed.path!='/skb_test':
            raise RuntimeError('Only disposable localhost skb_test is allowed')
        self.repo=Repository({'cloud':{'database_url':'postgresql://test:TEST@ep-test.neon.tech/db'}},connect=lambda **_:psycopg.connect(uri,autocommit=True))
        self.repo.initialize()
        self.store=DraftStore({},repository=self.repo)
        self.store.initialize()
        with self.repo.transaction(write=True) as conn:
            conn.execute('TRUNCATE skb_analytics.draft_revisions,skb_analytics.draft_heads,skb_analytics.state,skb_analytics.assets,skb_analytics.records,skb_analytics.imports')
        data=fixture()
        imported=draft_fixture(data)
        imported['items'][0]['title']='Сохранённая ручная правка'
        records=[{'kind':kind,'key':str(key),'payload':value} for kind in ('event','period','competitor') for key,value in data[kind].items()]
        records.append({'kind':'draft','key':imported['id'],'payload':imported})
        catalog={'manifest':data['manifest'],'assets':{'a'*64:{'pack':'b'*64,'bytes':7}},'records':records}
        self.import_id=self.repo.activate(catalog,{'b'*64:'test_private_drive_id'})
        self.store.initialize()

    def test_migration_preserves_manual_text_and_repeat_does_not_reset_saved_work(self):
        draft=self.store.read(self.import_id,'2026-09')
        self.assertEqual(draft['items'][0]['title'],'Сохранённая ручная правка')
        saved=self.store.commit(self.import_id,draft,1,{'conclusions':"Quote '; $$ $draft_schema$ Ё",'items':draft['items']},'editor@example.com')
        self.store.initialize()
        self.assertEqual(self.store.read(self.import_id,'2026-09')['revision'],2)
        self.assertEqual(self.store.version(self.import_id,draft['id'],1)['conclusions'],'')
        self.assertEqual(self.store.version(self.import_id,draft['id'],2)['conclusions'],saved['conclusions'])
        self.assertEqual(self.repo.load()['draft'][draft['id']]['revision'],1)

    def test_concurrent_saves_allow_one_winner_and_exactly_one_history_entry(self):
        draft=self.store.read(self.import_id,'2026-09')
        def save(text):
            try:return self.store.commit(self.import_id,draft,1,{'conclusions':text,'items':draft['items']},'editor@example.com')
            except DraftConflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(save,['First','Second']))
        self.assertEqual(sum(v is not None for v in outcomes),1)
        self.assertEqual(len(self.store.history(self.import_id,draft['id'])),2)
        self.assertEqual(self.store.read(self.import_id,'2026-09')['revision'],2)

    def test_create_is_idempotent_and_missing_period_creates_nothing(self):
        draft=self.store.read(self.import_id,'2026-09')
        payload=create_payload(self.repo.load(),'2026-09')
        existing=self.store.create(self.import_id,'2026-09',payload,'editor@example.com')
        self.assertEqual(existing['id'],draft['id'])
        self.assertEqual(existing['items'][0]['title'],'Сохранённая ручная правка')
        with self.assertRaises(DraftConflict):self.store.create(self.import_id,'2030-01',payload,'editor@example.com')

    def test_history_failure_rolls_back_head_update(self):
        draft=self.store.read(self.import_id,'2026-09')
        with self.repo.transaction(write=True) as conn:
            conn.execute('INSERT INTO skb_analytics.draft_revisions SELECT import_id,id,2,payload,updated_at,updated_by FROM skb_analytics.draft_heads')
        from cloud.drive_store import StorageError
        with self.assertRaises(StorageError):
            self.store.commit(self.import_id,draft,1,{'conclusions':'must rollback','items':[]},'editor@example.com')
        self.assertEqual(self.store.read(self.import_id,'2026-09')['revision'],1)

