import unittest

from cloud.access import authorize, require_admin


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.claims = dict(is_logged_in=True, email_verified=True, iss="https://accounts.google.com", sub="123", email="Owner@example.com")
        self.invites = dict(admin_emails=["owner@example.com"], editor_emails=["editor@example.com"], viewer_emails=["reader@example.com"])

    def test_invited_verified_account_gets_exact_role(self):
        for email, role in [(" OWNER@example.com ", "admin"), ("editor@example.com", "editor"), ("reader@example.com", "viewer")]:
            self.assertEqual(authorize({**self.claims, "email": email}, self.invites).role, role)

    def test_default_denial_for_untrusted_or_missing_claims(self):
        for extra in [{"is_logged_in": False}, {"email_verified": False}, {"email_verified": "true"}, {"sub": ""},
                      {"iss": "https://example.com"}, {"email": "outsider@example.com"}, {"email": "owner+other@example.com"}]:
            with self.subTest(extra=extra):
                self.assertFalse(authorize(self.claims | extra, self.invites).allowed)
        self.assertFalse(authorize({}, self.invites).allowed)

    def test_malformed_allowlist_does_not_grant_substring_access(self):
        self.assertFalse(authorize(self.claims, {"admin_emails": "owner@example.com"}).allowed)
        self.assertFalse(authorize(self.claims, {}).allowed)

    def test_revocation_is_checked_again_without_cached_role(self):
        self.assertEqual(require_admin(self.claims, self.invites).role, "admin")
        self.invites["admin_emails"] = []
        with self.assertRaises(PermissionError):
            require_admin(self.claims, self.invites)

    def test_editor_and_viewer_cannot_run_admin_actions(self):
        for email in ("editor@example.com", "reader@example.com"):
            with self.assertRaises(PermissionError):
                require_admin(self.claims | {"email": email}, self.invites)


if __name__ == "__main__":
    unittest.main()
