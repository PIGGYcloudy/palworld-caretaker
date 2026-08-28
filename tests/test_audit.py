from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.audit import AuditLog, sanitize


class AuditLogTests(unittest.TestCase):
    def test_records_structured_manager_owned_secret_free_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = AuditLog(Path(directory) / "state", secrets=("server-secret", "Bearer top-secret"))
            entry = audit.record(
                source="Web", who="palworld-manager", action="restore", status="success",
                details={"snapshot": "palworld-20260828-010101", "password": "server-secret",
                         "headers": {"Authorization": "Bearer top-secret"},
                         "message": "Authorization: Bearer top-secret"},
            )
            self.assertEqual(entry["source"], "Web")
            self.assertEqual(entry["action"], "restore")
            self.assertEqual(entry["details"]["password"], "***")
            raw = audit.path.read_text(encoding="utf-8")
            self.assertNotIn("server-secret", raw)
            self.assertNotIn("top-secret", raw)
            self.assertEqual(stat.S_IMODE(audit.path.stat().st_mode), 0o640)
            self.assertEqual(audit.path.stat().st_uid, os.getuid())
            self.assertEqual(audit.recent(), [entry])

    def test_reading_historical_entries_sanitizes_unsafe_content_again(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = AuditLog(Path(directory), secrets=("saved-secret",))
            audit.path.write_text(
                '{"source":"CLI","action":"update","status":"failed",'
                '"details":{"token":"saved-secret","note":"password=saved-secret"}}\n',
                encoding="utf-8",
            )
            audit.path.chmod(0o640)
            entry = audit.recent()[0]
            self.assertEqual(entry["details"]["token"], "***")
            self.assertNotIn("saved-secret", str(entry))

    def test_sanitize_recurses_through_headers_and_lists(self):
        result = sanitize({"headers": {"X-Api-Key": "key"}, "events": ["token=key"]})
        self.assertEqual(result["headers"]["X-Api-Key"], "***")
        self.assertEqual(result["events"], ["token=***"])

    def test_sanitize_redacts_operator_supplied_messages_and_reasons(self):
        result = sanitize({"message": "private ban rationale", "reason": "support token abc"})
        self.assertEqual(result, {"message": "***", "reason": "***"})

    def test_sanitize_covers_wildcard_credential_key_fragments_and_rejects_nan(self):
        result = sanitize({
            "deployKeyId": "a", "clientSecretValue": "b", "refreshToken": "c",
            "dbPassword": "d", "customAuthHeader": "e", "cloudCredentialRef": "f",
        })
        self.assertEqual(set(result.values()), {"***"})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                AuditLog(directory).record(source="Web", action="test", status="success",
                                           details={"value": float("nan")})


if __name__ == "__main__":
    unittest.main()
