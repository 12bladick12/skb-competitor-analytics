import json
import unittest
from unittest.mock import MagicMock

from cloud.storage_probe import check_drive_write, check_neon_write


CONFIG = {"cloud": {"database_url": "postgresql://user:SECRET@ep-example.neon.tech/db"},
          "drive": {"client_id": "client", "client_secret": "SECRET", "refresh_token": "REFRESH"}}
FILE_ID = "allocated_test_file_123"


class DriveSession:
    def __init__(self, *, fail_upload=False, corrupt=False, fail_delete=False, invalid_id=False):
        self.fail_upload, self.corrupt = fail_upload, corrupt
        self.fail_delete, self.invalid_id = fail_delete, invalid_id
        self.calls = []
        self.content = b""
        self.deleted = False

    def response(self, status=200, payload=None, content=b""):
        result = MagicMock(status_code=status, content=content)
        result.json.return_value = payload
        return result

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/token"):
            return self.response(payload={"access_token": "ACCESS"})
        # Parse the actual multipart envelope; no implementation-specific token.
        parts = kwargs["data"].split(b"\r\n\r\n")
        self.content = parts[2].rsplit(b"\r\n--", 1)[0]
        if self.fail_upload:
            raise OSError("DO_NOT_PRINT REFRESH")
        return self.response(payload={"id": FILE_ID})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/generateIds"):
            return self.response(payload={"ids": ["../../outside" if self.invalid_id else FILE_ID]})
        if self.deleted:
            return self.response(status=404)
        return self.response(content=b"corrupt" if self.corrupt else self.content)

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        if self.fail_delete:
            return self.response(status=503)
        self.deleted = True
        return self.response(status=204)


class StorageProbeTests(unittest.TestCase):
    def test_drive_upload_readback_and_verified_cleanup(self):
        session = DriveSession()
        result = check_drive_write(CONFIG, session=session)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.details, {"readback_verified": True, "cleanup_verified": True})
        self.assertTrue(session.deleted)
        self.assertEqual([c[0] for c in session.calls], ["POST", "GET", "POST", "GET", "DELETE", "GET"])
        for _, url, kwargs in session.calls:
            self.assertTrue(url.startswith("https://"))
            self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("ACCESS", json.dumps(result.to_dict()))

    def test_drive_integrity_failure_is_not_success_and_still_cleans_up(self):
        session = DriveSession(corrupt=True)
        result = check_drive_write(CONFIG, session=session)
        self.assertEqual(result.status, "error")
        self.assertTrue(session.deleted)
        self.assertFalse(result.details["readback_verified"])

    def test_ambiguous_upload_timeout_removes_only_preallocated_file(self):
        session = DriveSession(fail_upload=True)
        result = check_drive_write(CONFIG, session=session)
        self.assertEqual(result.status, "error")
        self.assertTrue(session.deleted)
        deletes = [url for method, url, _ in session.calls if method == "DELETE"]
        self.assertEqual(deletes, ["https://www.googleapis.com/drive/v3/files/" + FILE_ID])
        self.assertNotIn("DO_NOT_PRINT", json.dumps(result.to_dict()))

    def test_failed_cleanup_identifies_only_our_test_file(self):
        result = check_drive_write(CONFIG, session=DriveSession(fail_delete=True))
        self.assertEqual(result.status, "warning")
        self.assertTrue(result.details["readback_verified"])
        self.assertFalse(result.details["cleanup_verified"])
        self.assertIn("skb-storage-check-", result.message)

    def test_malformed_allocated_id_never_used_as_url_or_deleted(self):
        session = DriveSession(invalid_id=True)
        self.assertEqual(check_drive_write(CONFIG, session=session).status, "error")
        self.assertEqual([c[0] for c in session.calls], ["POST", "GET"])

    def test_missing_config_does_not_access_network(self):
        connect, session = MagicMock(), MagicMock()
        self.assertEqual(check_neon_write({}, connect=connect).status, "pending")
        self.assertEqual(check_drive_write({}, session=session).status, "pending")
        connect.assert_not_called()
        self.assertEqual(session.mock_calls, [])

    def test_neon_reads_committed_value_in_new_connection_then_drops_only_probe(self):
        state = {}
        connections = []

        def connect(**params):
            self.assertNotIn("options", params)
            conn = MagicMock()
            conn.__enter__.return_value = conn
            def execute(sql, args=None):
                if sql.startswith("INSERT"):
                    state["value"] = args[0]
                if sql.startswith("SELECT"):
                    self.assertEqual(len(connections), 2)
                    return MagicMock(fetchone=lambda: (state["value"],))
                if sql.startswith("CREATE"):
                    state["table"] = sql.split()[2]
                if sql.startswith("DROP"):
                    self.assertEqual(sql.split()[-1], state["table"])
                    state["deleted"] = True
                return MagicMock()
            conn.execute.side_effect = execute
            connections.append(conn)
            return conn

        result = check_neon_write(CONFIG, connect=connect)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(connections), 3)
        self.assertTrue(state["deleted"])
        self.assertTrue(connections[1].read_only)

    def test_neon_read_failure_still_cleans_up_without_exposing_error(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        def execute(sql, *args):
            if sql.startswith("SELECT"):
                raise RuntimeError("password=DO_NOT_PRINT")
        connection.execute.side_effect = execute
        result = check_neon_write(CONFIG, connect=MagicMock(return_value=connection))
        self.assertEqual(result.status, "error")
        self.assertTrue(any(call.args[0].startswith("DROP TABLE") for call in connection.execute.call_args_list))
        self.assertNotIn("DO_NOT_PRINT", json.dumps(result.to_dict()))

    def test_neon_cleanup_failure_cannot_be_reported_as_success(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        def execute(sql, *args):
            if sql.startswith("DROP"):
                raise RuntimeError("SECRET")
        conn.execute.side_effect = execute
        result = check_neon_write(CONFIG, connect=MagicMock(return_value=conn))
        self.assertEqual(result.status, "warning")
        self.assertIn("skb_storage_check_", result.message)
        self.assertNotIn("SECRET", result.message)
