from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PlaybookTemplateTests(unittest.TestCase):
    def test_jinja_defaults_do_not_contain_nested_jinja_expressions(self) -> None:
        bad: list[str] = []
        pattern = re.compile(r"default\(\s*['\"]\s*\{\{")
        for path in sorted((ROOT / "playbooks").glob("*.yml")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    bad.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(bad, [], "nested Jinja in default() is not recursively rendered")

    def test_disruptive_playbooks_have_no_lab_member_or_take_defaults(self) -> None:
        forbidden = ("default('192.0.2.20')", "default('192.0.2.21')", "default('91')")
        findings: list[str] = []
        for path in sorted((ROOT / "playbooks").glob("*.yml")):
            text = path.read_text()
            for token in forbidden:
                if token in text:
                    findings.append(f"{path.name}: {token}")
        self.assertEqual(findings, [])

    def test_install_and_upgrade_plan_steps_require_a_checksum(self) -> None:
        text = (ROOT / "playbooks" / "01_validate_activity_plan.yml").read_text()
        self.assertIn("item.checksum_sha1", text)
        self.assertIn("item.checksum_sha256", text)



if __name__ == "__main__":
    unittest.main()
