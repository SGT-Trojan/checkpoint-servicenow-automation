from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
GENERATED_PUBLIC_PATHS = {
    "docs/CDT_AND_MANAGEMENT_API.md",
    "docs/CERTIFIED_SCENARIOS.md",
    "docs/COMPONENT_REFERENCE.md",
    "docs/SERVICENOW_BUILD_GUIDE.md",
    "docs/START_HERE.md",
}
sys.path.insert(0, str(ROOT))
import servicenow_checkpoint_runner as runner  # noqa: E402


class ExampleTests(unittest.TestCase):
    def test_json_and_yaml_parse(self):
        for path in EXAMPLES.rglob("*.json"):
            json.loads(path.read_text())
        for path in EXAMPLES.rglob("*.jsonl"):
            for line in path.read_text().splitlines():
                if line.strip():
                    json.loads(line)
        for path in EXAMPLES.rglob("*.yml"):
            list(yaml.safe_load_all(path.read_text()))

    def test_local_markdown_links_resolve(self):
        pattern = re.compile(r"(?<!!)\[[^]]*\]\(([^) #]+)(?:#[^)]+)?\)")
        missing = []
        for path in EXAMPLES.rglob("*.md"):
            for target in pattern.findall(path.read_text()):
                if re.match(r"(?:https?|mailto):", target):
                    continue
                resolved = (path.parent / target).resolve()
                if resolved.exists():
                    continue
                try:
                    public_target = resolved.relative_to(ROOT).as_posix()
                except ValueError:
                    public_target = ""
                if public_target not in GENERATED_PUBLIC_PATHS:
                    missing.append(f"{path.relative_to(ROOT)} -> {target}")
        self.assertEqual(missing, [])

    def test_mutating_plans_fail_closed_on_placeholders(self):
        for path in EXAMPLES.rglob("activity-plan.json"):
            plan = json.loads(path.read_text())
            install_steps = [
                row
                for row in plan.get("package_steps", [])
                if row.get("action") in {"install", "upgrade"}
            ]
            if not install_steps:
                continue
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "invalid SHA"):
                runner.package_steps_from_rows(install_steps)

    def test_examples_cannot_enable_execution(self):
        machine_suffixes = {".yml", ".yaml", ".json", ".py", ".csv"}
        for path in EXAMPLES.rglob("*"):
            if not path.is_file() or path.suffix not in machine_suffixes:
                continue
            text = path.read_text()
            self.assertNotRegex(text, r"checkpoint_execute_[a-z_]+\s*:\s*true", str(path))
            self.assertNotIn("--execute", text, str(path))
            self.assertIsNone(
                re.search(r"(?i)\b[0-9a-f]{40}\b|\b[0-9a-f]{64}\b", text),
                str(path),
            )

    def test_certification_language_is_disclaimed(self):
        for path in EXAMPLES.rglob("*"):
            if not path.is_file():
                continue
            for line in path.read_text(errors="replace").splitlines():
                visible_text = re.sub(r"\]\([^)]+\)", "]", line.lower())
                if "certif" in visible_text:
                    self.assertRegex(
                        visible_text,
                        r"not .*certif|does not certif",
                        str(path),
                    )

    def test_scenario_boundaries_match_runner(self):
        self.assertFalse((EXAMPLES / "jhf_remove_api").exists())
        removal_guide = (EXAMPLES / "jhf_remove_cdt/README.md").read_text()
        normalized_removal_guide = " ".join(removal_guide.split())
        self.assertIn("CPInstLog", removal_guide)
        self.assertIn("missing or ambiguous", removal_guide)
        self.assertIn(
            "`source_path`, `package_name`, `display_name`, `name`, and the step name",
            normalized_removal_guide,
        )
        self.assertIn(
            "prerequisite checks; they are never removal identities",
            normalized_removal_guide,
        )
        self.assertNotIn(
            "aliases in `package_name`, `name`, and `requires_present`",
            normalized_removal_guide,
        )

        api_plan = json.loads((EXAMPLES / "jhf_install_api/activity-plan.json").read_text())
        api_playbooks = [row[1] for row in runner.workflow_steps(api_plan)]
        self.assertIn("39_api_repository_package.yml", api_playbooks)
        self.assertEqual(api_playbooks.count("41_api_execute_package.yml"), 2)
        self.assertNotIn("10_cdt_generate_candidates.yml", api_playbooks)
        self.assertNotIn("20_cdt_execute_guarded.yml", api_playbooks)

        agent_plan = json.loads((EXAMPLES / "deployment_agent/activity-plan.json").read_text())
        agent_phases = [row[0] for row in runner.workflow_steps(agent_plan)]
        self.assertIn("install-deployment-agent", agent_phases)
        self.assertNotIn("approve-testers", agent_phases)

        major_plan = json.loads((EXAMPLES / "major_upgrade/activity-plan.json").read_text())
        major_phases = [row[0] for row in runner.workflow_steps(major_plan)]
        expected = [
            "first-member",
            "mixed-version-policy-gate",
            "mvc-on",
            "failover-to-first",
            "approve-testers",
            "second-member",
            "final-policy-install",
            "mvc-off",
        ]
        positions = [major_phases.index(phase) for phase in expected]
        self.assertEqual(positions, sorted(positions))
        major_guide = (EXAMPLES / "major_upgrade/README.md").read_text()
        self.assertIn("plan-and-validate example", major_guide)
        self.assertIn("not a certified combination", major_guide)

    def test_expected_failures_execute(self):
        result = subprocess.run(
            [sys.executable, str(EXAMPLES / "expected_failures/check_failures.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Expected failure examples passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
