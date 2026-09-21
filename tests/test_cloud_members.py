import unittest
from unittest.mock import MagicMock, patch

from cloud.access import current_access


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.config={'cloud':{'database_url':'postgresql://test:TEST@ep-test.neon.tech/db'}}
        self.claims=dict(is_logged_in=True,email_verified=True,iss='https://accounts.google.com',sub='subject',email='new@example.com')
        self.store=MagicMock()
        self.factory=MagicMock(return_value=self.store)

    def access(self, **extra):
        return current_access(self.claims,self.config,store_factory=self.factory,**extra)

    def test_verified_registration_is_viewer_and_untrusted_claims_never_touch_database(self):
        self.store.find.return_value=None
        self.store.register.return_value={'email':'new@example.com','subject':'subject','role':'viewer','status':'active'}
        self.assertEqual(self.access(register=True).role,'viewer')
        self.store.register.assert_called_once_with('new@example.com','subject')
        for change in ({'email_verified':False},{'iss':'https://evil.test'},{'sub':''},{'is_logged_in':False}):
            self.factory.reset_mock()
            self.assertFalse(current_access(self.claims|change,self.config,store_factory=self.factory,register=True).allowed)
            self.factory.assert_not_called()

    def test_blocked_and_bound_to_another_subject_cannot_reregister(self):
        self.store.find.return_value={'email':'new@example.com','subject':'subject','role':'viewer','status':'blocked'}
        self.assertFalse(self.access(register=True).allowed)
        self.store.register.assert_not_called()
        self.store.find.return_value.update(status='active',subject='someone-else')
        self.assertFalse(self.access(register=True).allowed)

    def test_membership_outage_fails_closed_and_actions_recheck_access(self):
        from cloud.drive_store import StorageError
        self.store.find.side_effect=StorageError('unavailable')
        with self.assertRaises(StorageError):self.access()
        from cloud.access import Access
        from cloud.drafts import DraftService
        from cloud.jobs import JobService
        from cloud.screens import file_bytes
        with patch('cloud.drafts.current_access',return_value=Access()), \
             patch('cloud.jobs.current_access',return_value=Access()), \
             patch('cloud.screens.current_access',return_value=Access()),patch('cloud.screens.DriveStore') as drive:
            factory=MagicMock()
            for action in (
                lambda:DraftService(lambda:self.config,lambda:self.claims,factory).save('c'*64,'2026-09',1,'',[]),
                lambda:JobService(lambda:self.config,lambda:self.claims,factory).collect('c'*64,'2026-09'),
                lambda:file_bytes(lambda:self.config,lambda:self.claims,'c'*64,'a'*64)):
                with self.assertRaises(PermissionError):action()
            factory.assert_not_called();drive.assert_not_called()
