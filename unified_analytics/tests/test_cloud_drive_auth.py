import base64
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from cloud.drive_auth import (
    AUTH_URL, DRIVE_SCOPE, TOKEN_URL, DriveAuthError, authorization_url, authorize_owner,
    exchange_code, read_desktop_client, read_settings_snapshot, save_drive_settings,
)


CLIENT = {"client_id": "123-test.apps.googleusercontent.com", "client_secret": "TEST-CLIENT-SECRET"}
CREDENTIALS = {**CLIENT, "refresh_token": "TEST-REFRESH-TOKEN"}
SETTINGS = '''# Keep owner settings and comments.
[auth]
client_secret = "KEEP-LOGIN-SECRET"
cookie_secret = "KEEP-COOKIE"
[cloud]
database_url = "KEEP-DATABASE"
[drive]
client_id = ""
client_secret = ""
refresh_token = ""
folder_id = "KEEP-FOLDER"

[access]
admin_emails = ["owner@example.com"]
'''


class DriveOwnerAuthorizationTests(unittest.TestCase):
    def test_client_type_and_provider_are_checked_before_authorization(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "credentials.json"
            installed = {**CLIENT, "auth_uri": AUTH_URL, "token_uri": TOKEN_URL}
            path.write_text(json.dumps({"installed": installed}), encoding="utf-8")
            self.assertEqual(read_desktop_client(path), CLIENT)
            path.write_text(json.dumps({"web": installed}), encoding="utf-8")
            with self.assertRaisesRegex(DriveAuthError, "Web application"):
                read_desktop_client(path)
            installed["token_uri"] = "https://untrusted.example/token"
            path.write_text(json.dumps({"installed": installed}), encoding="utf-8")
            with self.assertRaises(DriveAuthError) as raised:
                read_desktop_client(path)
            self.assertNotIn(CLIENT["client_secret"], str(raised.exception))
            path.write_text('{"secret": "DO_NOT_PRINT', encoding="utf-8")
            with self.assertRaises(DriveAuthError) as raised:
                read_desktop_client(path)
            self.assertNotIn("DO_NOT_PRINT", str(raised.exception))

    def test_authorization_uses_pkce_and_only_file_scope_without_client_secret(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        url = authorization_url(CLIENT, "http://127.0.0.1:50000/oauth2callback", "TEST-STATE", verifier)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["code_challenge"], ["E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], [DRIVE_SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertNotIn(CLIENT["client_secret"], url)
        self.assertNotIn(verifier, url)

    def test_loopback_rejects_wrong_state_host_and_duplicate_code(self):
        threads, outcomes, browser_query = [], [], {}

        def open_browser(url):
            query = parse_qs(urlsplit(url).query)
            browser_query.update(query)
            callback = query["redirect_uri"][0]
            self.assertEqual(urlsplit(callback).hostname, "127.0.0.1")

            def send_callbacks():
                cases = [
                    ({"state": "WRONG", "code": "TEST-CODE"}, {}, 400),
                    ({"state": query["state"][0], "code": "TEST-CODE"}, {"Host": "evil.example"}, 404),
                    ({"state": query["state"][0], "code": ["first", "second"]}, {}, 400),
                    ({"state": query["state"][0], "code": "TEST-CODE"}, {}, 200),
                ]
                try:
                    for values, headers, expected in cases:
                        request = Request(callback + "?" + urlencode(values, doseq=True), headers=headers)
                        try:
                            response = urlopen(request, timeout=3)
                        except HTTPError as exc:
                            response = exc
                        with response:
                            outcomes.append((response.status, expected, response.read().decode("utf-8")))
                except Exception as exc:
                    outcomes.append(type(exc).__name__)

            thread = threading.Thread(target=send_callbacks, daemon=True)
            threads.append(thread)
            thread.start()
            return True

        with patch("cloud.drive_auth.requests.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.post.return_value.status_code = 200
            session.post.return_value.json.return_value = {"refresh_token": CREDENTIALS["refresh_token"], "scope": DRIVE_SCOPE}
            actual = authorize_owner(CLIENT, timeout=10, open_browser=open_browser)
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(actual, CREDENTIALS)
        self.assertEqual(len(outcomes), 4)
        for status, expected, body in outcomes:
            self.assertEqual(status, expected)
            self.assertNotIn("TEST-CODE", body)
        kwargs = session.post.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(session.post.call_args.args, (TOKEN_URL,))
        self.assertEqual(kwargs["data"]["code"], "TEST-CODE")
        self.assertEqual(kwargs["data"]["redirect_uri"], browser_query["redirect_uri"][0])
        digest = hashlib.sha256(kwargs["data"]["code_verifier"].encode("ascii")).digest()
        self.assertEqual(base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"), browser_query["code_challenge"][0])

    def test_timeout_and_browser_failure_do_not_exchange_tokens(self):
        with patch("cloud.drive_auth.requests.Session") as session:
            with self.assertRaises(DriveAuthError):
                authorize_owner(CLIENT, timeout=0, open_browser=lambda _url: True)
            with self.assertRaises(DriveAuthError):
                authorize_owner(CLIENT, open_browser=lambda _url: False)
            session.assert_not_called()

    def test_missing_refresh_token_wrong_scope_and_http_errors_never_echo_secrets(self):
        session = MagicMock()
        for status, payload in [
            (200, {"access_token": "DO_NOT_PRINT"}),
            (200, {"refresh_token": "DO_NOT_PRINT", "scope": "openid"}),
            (302, {"refresh_token": "DO_NOT_PRINT"}),
            (400, {"error_description": "DO_NOT_PRINT"}),
        ]:
            with self.subTest(status=status, payload_type=tuple(payload)):
                session.post.return_value.status_code = status
                session.post.return_value.json.return_value = payload
                with self.assertRaises(DriveAuthError) as raised:
                    exchange_code(CLIENT, "TEST-CODE", "http://127.0.0.1:1/oauth2callback", "TEST-VERIFIER", session=session)
                self.assertNotIn("DO_NOT_PRINT", str(raised.exception))

    def test_save_preserves_every_non_drive_setting_and_detects_concurrent_edit(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "secrets.toml"
                path.write_bytes(SETTINGS.replace("\n", newline).encode("utf-8"))
                original, before = read_settings_snapshot(path)
                save_drive_settings(path, CREDENTIALS, original)
                after_bytes, after = read_settings_snapshot(path)
                self.assertEqual(after["drive"], {**before["drive"], **CREDENTIALS})
                for group in ("auth", "cloud", "access"):
                    self.assertEqual(after[group], before[group])
                self.assertTrue(after_bytes.startswith(original.split(b"[drive]")[0]))
                self.assertTrue(after_bytes.endswith(original[original.index(b"[access]"):]))
                path.write_bytes(after_bytes + b"\n# Owner changed the file\n")
                edited = path.read_bytes()
                with self.assertRaises(DriveAuthError):
                    save_drive_settings(path, {**CREDENTIALS, "refresh_token": "OTHER"}, after_bytes)
                self.assertEqual(path.read_bytes(), edited)
                self.assertEqual(list(Path(folder).glob("secrets.oauth-*.toml")), [])

    def test_save_can_add_drive_section_and_quotes_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secrets.toml"
            original = b'[auth]\nclient_secret = "KEEP"\n'
            path.write_bytes(original)
            values = {**CREDENTIALS, "refresh_token": 'TEST-"-\\-TOKEN'}
            save_drive_settings(path, values, original)
            self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8")), {"auth": {"client_secret": "KEEP"}, "drive": values})


if __name__ == "__main__":
    unittest.main()
