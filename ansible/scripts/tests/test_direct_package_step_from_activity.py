from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "direct_package_step_from_activity.py"
SPEC = importlib.util.spec_from_file_location("direct_package_step_from_activity", SCRIPT)
assert SPEC and SPEC.loader
direct = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = direct
SPEC.loader.exec_module(direct)


class Result:
    def __init__(self, output: str):
        self.output = output


class Session:
    def __init__(self, output: str):
        self.output = output

    def run(self, command: str, timeout: int = 0) -> Result:
        return Result(self.output)


class DirectRemoveIdentityTests(unittest.TestCase):
    def test_take_alias_resolves_from_local_cpinstlog(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        history = f"Install,CPUpdates,{package},BUNDLE_R81_20_JUMBO_HF_MAIN#76"
        self.assertEqual(
            direct.resolve_remove_package_name(
                Session(history),
                {"name": "remove_Take_76", "action": "remove", "package_name": "Take 76", "package_type": "jhf"},
            ),
            package,
        )

    def test_ambiguous_local_history_fails_closed(self) -> None:
        history = "\n".join([
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_SPECIAL_FULL.tgz",
        ])
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            direct.resolve_remove_package_name(
                Session(history),
                {"name": "remove_Take_76", "action": "remove", "package_name": "Take 76", "package_type": "jhf"},
            )


if __name__ == "__main__":
    unittest.main()
