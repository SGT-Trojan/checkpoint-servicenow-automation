from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scan_evidence_hygiene.py"
SPEC = importlib.util.spec_from_file_location("scan_evidence_hygiene", SCRIPT)
assert SPEC and SPEC.loader
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


class EvidenceSecretScannerTests(unittest.TestCase):
    def scan(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.log"
            path.write_text(text)
            return scanner.findings(path)

    def test_check_point_session_fields_are_rejected(self) -> None:
        for field in scanner.TOKEN_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(self.scan(f'"{field}": "0123456789abcdef"'))

    def test_redacted_and_empty_session_fields_are_allowed(self) -> None:
        text = "\n".join([
            '"authToken": "<REDACTED>"',
            '"clientSessionId": ""',
            "fwmSessionId: null",
        ])
        self.assertEqual(self.scan(text), [])

    def test_common_secret_forms_are_rejected(self) -> None:
        for value in (
            "Authorization: Bearer live-token",
            "sshpass -p unsafe-password",
            "password=unsafe-password",
            "api_key: unsafe-key",
        ):
            with self.subTest(value=value):
                self.assertTrue(self.scan(value))


if __name__ == "__main__":
    unittest.main()
