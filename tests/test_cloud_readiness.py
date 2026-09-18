import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from cloud.readiness import (
    check_all, check_drive, check_login_config, check_neon, load_secrets, quota_result,
)


class CloudReadinessTests(unittest.TestCase):
    def test_empty_settings_do_not_attempt_connections(self):
        with patch("requests.Session") as session:
            self.assertEqual([x.status for x in check_all({})], ["pending"] * 3)
            session.assert_not_called()

    def test_neon_rejects_host_override_and_non_tls_before_connecting(self):
        connect = MagicMock()
        urls = [
            "postgresql://user:secret@localhost/db",
            "postgresql://user:secret@host.neon.tech/db?sslmode=disable",
            "postgresql://user:secret@host.neon.tech/db?host=localhost",
            "postgresql://user:secret@host.neon.tech/db?sslmode=require&sslmode=disable",
            "postgresql://user:secret@host.neon.tech:bad/db",
        ]
        for url in urls:
            with self.subTest(url=url):
                result = check_neon({"cloud": {"database_url": url}}, connect=connect)
                self.assertEqual(result.status, "error")
                self.assertNotIn("secret", json.dumps(result.to_dict()))
        connect.assert_not_called()

    def test_neon_is_read_only_and_decodes_password(self):
        connect = MagicMock()
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = (1,)
        config = {"cloud": {"database_url": "postgresql://user:p%40ss@ep-sample.neon.tech/neondb?sslmode=require&channel_binding=require"}}
        self.assertEqual(check_neon(config, connect=connect).status, "ok")
        params = connect.call_args.kwargs
        self.assertEqual(params["password"], "p@ss")
        self.assertIn("default_transaction_read_only=on", params["options"])
        self.assertEqual(params["sslmode"], "require")
        connection.execute.assert_called_once_with("SELECT 1")
        connect.reset_mock()
        self.assertEqual(check_neon(config, offline=True, connect=connect).status, "unchecked")
        connect.assert_not_called()

    def test_driver_error_does_not_echo_connection_secrets(self):
        connect = MagicMock(side_effect=RuntimeError("password=DO_NOT_PRINT"))
        result = check_neon({"cloud": {"database_url": "postgresql://user:DO_NOT_PRINT@ep-sample.neon.tech/db"}}, connect=connect)
        self.assertEqual(result.status, "error")
        self.assertNotIn("DO_NOT_PRINT", json.dumps(result.to_dict()))

    def test_drive_quota_handles_overflow_missing_and_shared_storage(self):
        result = quota_result({"storageQuota": {"limit": "15000000000", "usage": "2000000000", "usageInDrive": "1"}})
        self.assertEqual(result.details["free_bytes"], 13000000000)
        self.assertEqual(result.status, "ok")
        self.assertEqual(quota_result({"storageQuota": {"limit": "1", "usage": "2"}}).details["free_bytes"], 0)
        self.assertEqual(quota_result({"storageQuota": {"usage": "0"}}).status, "warning")
        self.assertEqual(quota_result({}).status, "error")

    def test_drive_reads_only_quota_and_keeps_tokens_out_of_results(self):
        config = {"drive": {"client_id": "client", "client_secret": "SECRET", "refresh_token": "REFRESH"}}
        session = MagicMock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"access_token": "ACCESS"}
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"storageQuota": {"limit": "15000000000", "usage": "0"}}
        result = check_drive(config, session=session)
        self.assertEqual(result.status, "ok")
        self.assertEqual(session.get.call_args.args, ("https://www.googleapis.com/drive/v3/about",))
        self.assertEqual(session.get.call_args.kwargs["params"], {"fields": "storageQuota"})
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        for token in ("ACCESS", "SECRET", "REFRESH"):
            self.assertNotIn(token, json.dumps(result.to_dict()))
        session.get.reset_mock()
        session.post.return_value.status_code = 302
        self.assertEqual(check_drive(config, session=session).status, "error")
        session.get.assert_not_called()

    def test_toml_syntax_failure_never_echoes_secret(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / "bad.toml"
            path.write_text('[cloud]\ndatabase_url = DO_NOT_PRINT', encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                load_secrets(path)
            self.assertNotIn("DO_NOT_PRINT", str(raised.exception))

    def test_login_shape_is_never_reported_as_verified_login(self):
        auth = {
            "redirect_uri": "https://sample.streamlit.app/oauth2callback",
            "cookie_secret": "x" * 32, "client_id": "id", "client_secret": "secret",
            "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
        }
        self.assertEqual(check_login_config({"auth": auth}).status, "unchecked")
        auth["redirect_uri"] = "https://sample.streamlit.app.evil.test/oauth2callback"
        self.assertEqual(check_login_config({"auth": auth}).status, "error")


if __name__ == "__main__":
    unittest.main()
