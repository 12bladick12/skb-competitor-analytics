import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(HAS_STREAMLIT, "Install requirements-cloud.txt to run UI tests")
class CloudScreenTests(unittest.TestCase):
    def application(self, access=None):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=15)
        app.secrets.clear()
        app.secrets.update({
            "auth": {
                "redirect_uri": "https://example.streamlit.app/oauth2callback",
                "cookie_secret": "TEST-ONLY-" * 8,
                "client_id": "TEST-ID", "client_secret": "TEST-SECRET",
                "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
            },
            "access": access or {"admin_emails": ["admin@example.com"], "viewer_emails": ["reader@example.com"]},
        })
        return app

    def user(self, email, logged_in=True, verified=True):
        user = MagicMock()
        user.is_logged_in = logged_in
        user.to_dict.return_value = dict(email=email, email_verified=verified,
                                         iss="https://accounts.google.com", sub="test-subject")
        return user

    def test_unconfigured_app_starts_without_network_or_sensitive_details(self):
        app = self.application()
        app.secrets.clear()
        # Keep an explicit empty section: a fully empty AppTest Secrets object
        # otherwise reloads the developer's real local TOML on first access.
        app.secrets["auth"] = {}
        with patch("streamlit.user", self.user("", False)), patch("requests.Session") as network:
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("пока не настроен", app.info[0].value)
        self.assertEqual(len(app.button), 0)
        network.assert_not_called()

    def test_anonymous_visitor_sees_only_login(self):
        app = self.application()
        with patch("streamlit.user", self.user("", False)):
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual([b.label for b in app.button], ["Войти через Google"])

    def test_unknown_email_and_unverified_email_cannot_see_diagnostics(self):
        for email, verified in [("outsider@example.com", True), ("admin@example.com", False)]:
            with self.subTest(email=email, verified=verified):
                app = self.application()
                with patch("streamlit.user", self.user(email, verified=verified)):
                    app.run()
                self.assertEqual(len(app.exception), 0)
                self.assertIn("Доступ не предоставлен", app.warning[0].value)
                self.assertNotIn("Проверить Neon и Google Drive", [b.label for b in app.button])

    def test_viewer_cannot_see_or_trigger_admin_checks(self):
        app = self.application()
        with patch("streamlit.user", self.user("reader@example.com")), patch("requests.Session") as network:
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("Проверить Neon и Google Drive", [b.label for b in app.button])
        network.assert_not_called()

    def test_admin_gets_missing_config_messages_without_network(self):
        app = self.application()
        with patch("streamlit.user", self.user("admin@example.com")), patch("requests.Session") as network:
            app.run()
            next(b for b in app.button if b.label == "Проверить Neon и Google Drive").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("cloud.database_url" in item.value for item in app.info))
        network.assert_not_called()

    def test_revoked_admin_cannot_use_previously_rendered_button(self):
        app = self.application()
        with patch("streamlit.user", self.user("admin@example.com")), patch("requests.Session") as network:
            app.run()
            button = next(b for b in app.button if b.label == "Проверить Neon и Google Drive")
            app.secrets["access"]["admin_emails"] = []
            button.click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("Доступ не предоставлен", app.warning[0].value)
        network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
