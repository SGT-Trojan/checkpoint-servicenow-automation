from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import cpuse_da_fetch as da


class FakeContext:
    def storage_state(self, *, path: str) -> None:
        Path(path).write_text("authenticated session")


class DeploymentAgentIntegrityTests(unittest.TestCase):
    def test_missing_published_sha256_fails_closed(self) -> None:
        for value in (None, "", "not-a-hash", "a" * 63):
            with self.subTest(value=value), self.assertRaisesRegex(
                SystemExit, "valid published SHA256"
            ):
                da.require_published_sha256(value)

    def test_hash_mismatch_fails_and_removes_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "deployment-agent.tgz"
            package.write_bytes(b"untrusted package")
            with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                da.verify_download(package, "0" * 64)
            self.assertFalse(package.exists())

    def test_matching_hash_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "deployment-agent.tgz"
            payload = b"trusted synthetic package"
            package.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            self.assertEqual(da.verify_download(package, expected.upper()), expected)
            self.assertTrue(package.exists())

    def test_session_state_chmod_failure_is_surfaced_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "session.json"
            with mock.patch.object(da.os, "chmod", side_effect=OSError("denied")):
                with self.assertRaisesRegex(SystemExit, "could not secure"):
                    da.persist_session_state(FakeContext(), state_file)
            self.assertFalse(state_file.exists())


if __name__ == "__main__":
    unittest.main()
