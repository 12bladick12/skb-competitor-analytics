from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from urllib.parse import urlsplit

from cloud.access import current_access
from cloud.library import Repository
from cloud.members import MemberStore, MemberService


@unittest.skipUnless(os.environ.get('SKB_TEST_POSTGRES_URL'),'Isolated PostgreSQL required')
class MemberPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        uri=os.environ['SKB_TEST_POSTGRES_URL']
        parsed=urlsplit(uri)
        if parsed.hostname not in ('localhost','127.0.0.1') or parsed.path!='/skb_test':
            raise RuntimeError('Only disposable localhost skb_test is allowed')
        self.config={'cloud':{'database_url':'postgresql://test:TEST@ep-test.neon.tech/db'},
                     'access':{'admin_emails':['owner@example.com'],'editor_emails':['editor@example.com']}}
        self.repo=Repository(self.config,connect=lambda **_:psycopg.connect(uri,autocommit=True))
        self.repo.initialize()
        self.store=MemberStore({},repository=self.repo)
        self.store.initialize(self.config['access'])
        with self.repo.transaction(write=True) as conn:
            conn.execute('TRUNCATE skb_analytics.member_audit,skb_analytics.members')
        self.store.initialize(self.config['access'])
        self.owner=self.store.register('owner@example.com','owner-sub')
        self.factory=lambda _:self.store

    def claims(self,email,subject):
        return dict(email=email,sub=subject,is_logged_in=True,email_verified=True,iss='https://accounts.google.com')

    def test_registration_race_creates_one_viewer_and_seed_roles_survive(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.store.register('new@example.com','new-sub'),range(2)))
        self.assertEqual([r['role'] for r in results],['viewer','viewer'])
        self.assertEqual(sum(r['email']=='new@example.com' for r in self.store.list()),1)
        self.assertEqual(self.store.register('editor@example.com','editor-sub')['role'],'editor')
        self.assertEqual(self.owner['role'],'admin')

    def test_block_persists_on_relogin_changed_email_and_repeated_migration(self):
        member=self.store.register('new@example.com','new-sub')
        self.store.change('owner@example.com',member['email'],1,'viewer','blocked')
        self.store.initialize(self.config['access'])
        for email in ('new@example.com','renamed@example.com'):
            access=current_access(self.claims(email,'new-sub'),self.config,register=True,store_factory=self.factory)
            self.assertFalse(access.allowed)
            self.assertEqual(access.status,'blocked')
        self.assertEqual(len(self.store.query('SELECT * FROM skb_analytics.member_audit')),1)

    def test_viewer_cannot_administer_owner_cannot_be_removed_and_stale_changes_fail(self):
        member=self.store.register('new@example.com','new-sub')
        service=MemberService(lambda:self.config,lambda:self.claims('new@example.com','new-sub'),self.factory)
        with self.assertRaises(PermissionError):service.list()
        with self.assertRaises(ValueError):self.store.change('new@example.com','editor@example.com',1,'admin','active')
        with self.assertRaises(ValueError):self.store.change('owner@example.com','owner@example.com',1,'viewer','blocked')
        self.store.change('owner@example.com','new@example.com',1,'editor','active')
        with self.assertRaises(ValueError):self.store.change('owner@example.com','new@example.com',1,'admin','active')
        self.assertEqual(self.store.find('new@example.com','new-sub')['role'],'editor')

    def test_revoked_admin_cannot_apply_previously_rendered_action(self):
        other=self.store.register('other@example.com','other-sub')
        self.store.change('owner@example.com',other['email'],1,'admin','active')
        service=MemberService(lambda:self.config,lambda:self.claims('other@example.com','other-sub'),self.factory)
        self.assertTrue(service.list())
        self.store.change('owner@example.com',other['email'],2,'admin','blocked')
        with self.assertRaises(PermissionError):service.change('editor@example.com',1,'viewer','active')
        with self.assertRaises(ValueError):self.store.change('other@example.com','editor@example.com',1,'viewer','active')
