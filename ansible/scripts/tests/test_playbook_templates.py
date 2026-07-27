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


if __name__ == "__main__":
    unittest.main()
