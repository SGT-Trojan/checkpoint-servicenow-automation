from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "cluster_phase_control.py"


class ClusterPhaseCliTests(unittest.TestCase):
    def test_assert_member_take_requires_explicit_take(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "assert-member-take",
                "--members",
                "192.0.2.20",
                "192.0.2.21",
                "--state-file",
                "/tmp/nonexistent-state.json",
                "--target-host",
                "192.0.2.20",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--target-take is required", result.stderr)

    def test_no_historical_take_default_remains(self) -> None:
        text = SCRIPT.read_text()
        self.assertNotIn('parser.add_argument("--target-take", default="91")', text)


if __name__ == "__main__":
    unittest.main()
