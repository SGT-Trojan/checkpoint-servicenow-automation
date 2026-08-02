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
    "docs/SERVICENOW_TICKET_EXAMPLE.md",
    "docs/SERVICENOW_BUILD_GUIDE.md",
    "docs/START_HERE.md",
    "docs/STANDALONE_PYTHON_WORKFLOW.md",
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
        text_suffixes = machine_suffixes | {".md", ".txt"}
        for path in EXAMPLES.rglob("*"):
            if path.is_file() and path.suffix in text_suffixes:
                self.assertNotIn("--execute", path.read_text(), str(path))

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

    def test_runner_cli_walkthrough_is_safe_and_complete(self):
        root = EXAMPLES / "runner_cli"
        guide = (root / "README.md").read_text()
        fixture = root / "cpuse-package.csv"
        gate_output = (root / "expected/gate-stop.txt").read_text()
        resume_output = (root / "expected/resume.txt").read_text()

        self.assertIn("Without ServiceNow, every human gate is self-attested", guide)
        self.assertIn("Production use must keep the manual tester", guide)
        self.assertRegex(guide, r"Lab only.*`--simulate-gates`")
        self.assertIn("--start-at second-member", guide)
        self.assertGreaterEqual(guide.count('--chg-number "$RUN_ID"'), 2)
        self.assertIn("cluster_initial_state_${RUN_ID}.json", guide)
        self.assertIn("test_inputs/cpuse_install_take91.csv", guide)
        self.assertIn("test_inputs/cpuse_remove_take91.csv", guide)
        self.assertNotIn("--execute", guide)

        with self.assertRaisesRegex(ValueError, "invalid SHA"):
            runner.package_steps_from_rows(runner.parse_tabular_file(fixture))

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text()
            self.assertNotRegex(text, r"(?:REQ|RITM|SCTASK|CHG|CTASK)\d", str(path))
            self.assertNotRegex(text, r"(?i)\b[0-9a-f]{40}\b|\b[0-9a-f]{64}\b", str(path))
        self.assertIn("SYNTHETIC OUTPUT SHAPE", gate_output)
        self.assertIn("Shell return code: 20", gate_output)
        self.assertIn("SYNTHETIC OUTPUT SHAPE", resume_output)
        self.assertIn("Shell return code: 0", resume_output)

    def test_standalone_python_examples_are_complete_and_fail_closed(self):
        root = EXAMPLES / "standalone_python"
        guide = (root / "README.md").read_text()
        plans = {
            "take76-install.json": "install",
            "take76-remove.json": "remove",
            "r8120-to-r82.json": "upgrade",
        }
        for name, action in plans.items():
            plan = json.loads((root / name).read_text())
            self.assertEqual(
                plan["execution"]["deployment_backend"], "standalone"
            )
            self.assertIs(plan["execution"]["tester_pause"], True)
            self.assertEqual(plan["package_steps"][0]["action"], action)
        for phase in (
            "capture-state",
            "baseline-capture",
            "stage-files",
            "first-member",
            "simulate-tester-gate",
            "second-member",
            "restore-original-active",
            "postcheck",
            "mixed-version-policy",
            "mvc-on",
            "final-policy",
            "mvc-off",
        ):
            self.assertIn(phase, guide)
        self.assertIn("--samples 3", guide)
        self.assertIn("umask 077", guide)
        self.assertIn("mode `0600`", guide)
        self.assertIn("positive increasing sample", guide)
        self.assertIn("timezone-aware increasing timestamps", guide)
        self.assertIn("`failover-to-first` completion", guide)
        self.assertIn("--host-key-evidence", guide)
        self.assertIn("without ServiceNow", guide)
        self.assertIn("without\n`ansible-playbook`", guide)
        self.assertIn("`$RUN_DIR/activity-plan.locked.json`", guide)
        self.assertIn("Keep passing\n`$PLAN`", guide)
        self.assertIn("Take 76 install", guide)
        self.assertIn("Take 76 removal", guide)
        self.assertIn("R81.20 to R82 build 777 with embedded Take 60", guide)
        major_order = (
            "capture-state -> baseline-capture -> stage-files -> first-member\n"
            "mixed-version-policy -> mvc-on -> failover-to-first\n"
            "simulate-tester-gate -> second-member -> final-policy -> mvc-off\n"
            "restore-original-active -> final-capture -> postcheck"
        )
        self.assertIn(major_order, guide)
        ordered_major_phases = (
            "mixed-version-policy",
            "mvc-on",
            "failover-to-first",
            "second-member",
            "final-policy",
            "mvc-off",
        )
        positions = [major_order.index(phase) for phase in ordered_major_phases]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("two-choice `yes/no` confirmation", guide)
        self.assertIn("never answer Blink", guide)
        self.assertIn("same stopped member phase", guide)
        self.assertIn("reconciliation-only", guide)
        self.assertIn("does not prove independent human approval", guide)

    def test_standalone_live_recertification_guidance_is_pinned(self):
        guide_path = ROOT / "STANDALONE_PYTHON_WORKFLOW.md"
        if not guide_path.is_file():
            guide_path = ROOT / "docs" / "STANDALONE_PYTHON_WORKFLOW.md"
        guide = guide_path.read_text()
        normalized = " ".join(guide.split())

        self.assertIn("## August 2026 live recertification", guide)
        self.assertIn("without ServiceNow and without Ansible", normalized)
        self.assertIn("R81.20 Take 76 install and removal", normalized)
        self.assertIn("R82 build 777 with embedded Take 60", normalized)
        self.assertIn(
            "operator self-attestation, not independent human approval",
            normalized,
        )

        major_section = guide.split("## August 2026 live recertification", 1)[1]
        major_section = major_section.split("## Before you begin", 1)[0]
        expected_order = [
            "validate",
            "capture-state",
            "baseline-capture",
            "stage-files",
            "first-member",
            "mixed-version-policy",
            "mvc-on",
            "failover-to-first",
            "simulate-tester-gate",
            "second-member",
            "final-policy",
            "mvc-off",
            "restore-original-active",
            "final-capture",
            "postcheck",
        ]
        positions = [
            major_section.index(f"\n{phase}\n") for phase in expected_order
        ]
        self.assertEqual(positions, sorted(positions))

        self.assertIn("Blink presents a two-choice yes/no confirmation", guide)
        self.assertIn("Never answer the Blink", guide)
        self.assertIn("that suppress-reboot choice exists", guide)
        self.assertIn("`HA module not started`", guide)
        self.assertIn("handoff to the mandatory new-version policy phase", normalized)
        self.assertIn("failover remains blocked", normalized)
        self.assertIn("bounded SSH keepalives", normalized)
        self.assertIn("trusted fingerprint verification", normalized)
        self.assertIn("reconciliation-only retry", normalized)
        self.assertIn("cannot redispatch the package mutation", normalized)
        self.assertIn("did not weaken or replace any of those controls", normalized)

    def test_completed_governed_walkthroughs_are_safe_and_complete(self):
        root = EXAMPLES / "governed"
        index = (root / "README.md").read_text()
        major = (root / "r8120-to-r82-t60.md").read_text()
        take91 = (root / "r82-take91-install.md").read_text()
        removal = (root / "r82-take91-remove.md").read_text()

        self.assertIn("What The Operator Runs", index)
        self.assertIn("What The Worker Runs Internally", index)
        self.assertIn("snow-checkpoint-readiness-worker", index)
        self.assertIn("--chg-sys-id '<change-sys-id>'", index)
        self.assertIn("--start-at second-member", index)
        self.assertIn("They are not operator commands", index)
        self.assertIn("does not use ServiceNow", index)
        self.assertIn("[R82 Take 91 removal](r82-take91-remove.md)", index)
        self.assertIn("The worker invokes the governed playbooks", index)
        self.assertIn("Do not invoke a mutating playbook directly", index)
        self.assertNotIn("checkpoint_execute_upgrade", index)

        for guide in (index, major, take91, removal):
            self.assertIn("../../docs/START_HERE.md", guide)
            self.assertIn("../../docs/COMPONENT_REFERENCE.md", guide)
            self.assertIn("../../docs/SERVICENOW_TICKET_EXAMPLE.md", guide)
            self.assertIn("../runner_cli/README.md", guide)
        self.assertEqual(index.count("--simulate-gates"), 1)
        self.assertNotIn("--simulate-gates", major)
        self.assertNotIn("--simulate-gates", take91)
        self.assertEqual(removal.count("--simulate-gates"), 1)
        self.assertIn("Do not use `--simulate-gates`", removal)

        major_phases = (
            "discover-targets",
            "validate-plan",
            "init",
            "deployment-agent-readiness",
            "cluster-state-capture",
            "baseline-capture",
            "stage-files",
            "first-member",
            "mixed-version-policy-gate",
            "mvc-on",
            "failover-to-first",
            "approve-testers",
            "second-member",
            "final-policy-install",
            "mvc-off",
            "restore-original-active",
            "final-support-capture",
            "support-diff",
            "postcheck",
        )
        take91_phases = (
            "discover-targets",
            "validate-plan",
            "init",
            "deployment-agent-readiness",
            "cluster-state-capture",
            "baseline-capture",
            "stage-files",
            "first-member",
            "failover-to-first",
            "approve-testers",
            "second-member",
            "restore-original-active",
            "final-support-capture",
            "support-diff",
            "postcheck",
        )
        removal_phases = (
            "discover-targets",
            "validate-plan",
            "init",
            "deployment-agent-readiness",
            "cluster-state-capture",
            "baseline-capture",
            "first-member",
            "failover-to-first",
            "approve-testers",
            "second-member",
            "restore-original-active",
            "final-support-capture",
            "support-diff",
            "postcheck",
        )
        phase_pattern = re.compile(r"^\d+\. `([^ `]+)`$", re.MULTILINE)
        self.assertEqual(phase_pattern.findall(major), list(major_phases))
        self.assertEqual(phase_pattern.findall(take91), list(take91_phases))
        self.assertEqual(phase_pattern.findall(removal), list(removal_phases))

        corrected_tester_text = (
            "waits for the\nexisting tester CTASK created by the ServiceNow "
            "business rule"
        )
        self.assertIn(corrected_tester_text, major)
        stale_tester_text = "creates or " "waits for"
        for path in root.glob("*.md"):
            self.assertNotIn(stale_tester_text, path.read_text(), str(path))

        self.assertIn("ICAP disabled", major)
        self.assertIn("no evidence about ICAP health", major)
        self.assertIn("ICAP-disabled mode", take91)
        self.assertIn("cannot support an\nICAP-health claim", take91)
        self.assertIn("Tester evidence checklist - EMPTY TEMPLATE", major)
        self.assertIn("Tester evidence checklist - EMPTY TEMPLATE", take91)
        self.assertNotRegex(
            major + take91 + removal, r"Decision.*(?:pass|approve)"
        )

        for path in root.glob("*.csv"):
            rows = runner.parse_tabular_file(path)
            if path.name == "r82-take91-remove.csv":
                steps = runner.package_steps_from_rows(rows)
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0]["action"], "remove")
                self.assertTrue(steps[0]["package_name"].endswith(".tar"))
                self.assertEqual(steps[0]["checksum_sha1"], "")
                self.assertEqual(steps[0]["checksum_sha256"], "")
            else:
                if path.name == "r8120-to-r82-t60.csv":
                    self.assertEqual(rows[0]["target_build"], "777")
                with self.subTest(path=path), self.assertRaisesRegex(
                    ValueError, "invalid SHA"
                ):
                    runner.package_steps_from_rows(rows)

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text()
            self.assertNotRegex(
                text, r"(?:REQ|RITM|SCTASK|CHG|CTASK)\d", str(path)
            )
            self.assertNotRegex(
                text, r"(?i)\b[0-9a-f]{40}\b|\b[0-9a-f]{64}\b", str(path)
            )

        for guide in (major, take91, removal):
            lines = guide.splitlines()
            for position, line in enumerate(lines):
                if line != "```text":
                    continue
                following = "\n".join(lines[position + 1 : position + 3])
                if "EMPTY TEMPLATE" in following:
                    continue
                previous = next(
                    (
                        candidate
                        for candidate in reversed(lines[:position])
                        if candidate
                    ),
                    "",
                )
                self.assertEqual(
                    previous,
                    "SYNTHETIC OUTPUT SHAPE - NOT LAB EVIDENCE",
                )

        normalized_removal = " ".join(removal.split())
        for statement in (
            "Removal must be its own approved catalog submission",
            "blank hashes, and a placeholder `.tar` identity",
            "The exact resolved `.tgz` identity",
            "Zero matches or more than one match fails closed",
            "only the originally standby member B",
            "guarded CDT removal",
            "required stabilization interval",
            "return code `20`",
            "existing dedicated tester CTASK created by the ServiceNow business rule",
            "same CHG with the internal `--start-at second-member` boundary",
            "restores A ACTIVE / B STANDBY",
            "captures final support data, generates the support diff, and runs postcheck",
            "successful terminal states",
            "ICAP was disabled, so this run makes no ICAP-health claim",
            "There is no first-member summary",
            "The first Take 60 observation appears only in resume discovery",
            "Postcheck enforces Take 91 absence; it does not enforce that the fallback Take equals 60",
            "no intermediate support-capture phase despite that plan flag",
            "Local evidence lacks a sanitized final ServiceNow snapshot",
            "Normal expected output omits the lab mail-notification error",
        ):
            self.assertIn(statement, normalized_removal)
        self.assertIn("There is no `stage-files` phase for removal", removal)

        expected_root = root / "expected/r82-take91-remove"
        expected_names = (
            "01-readiness.txt",
            "02-identity-resolution.txt",
            "03-standby-candidate.txt",
            "04-first-member.txt",
            "05-tester-gate.txt",
            "06-second-member.txt",
            "07-terminal-records.txt",
        )
        self.assertEqual(
            tuple(path.name for path in sorted(expected_root.glob("*.txt"))),
            expected_names,
        )
        expected_text = ""
        for name in expected_names:
            text = (expected_root / name).read_text()
            self.assertEqual(
                text.splitlines()[0],
                "SYNTHETIC OUTPUT SHAPE - NOT LAB EVIDENCE",
                name,
            )
            self.assertEqual(
                text.count("SYNTHETIC OUTPUT SHAPE - NOT LAB EVIDENCE"),
                1,
                name,
            )
            self.assertIn(f"(expected/r82-take91-remove/{name})", removal)
            expected_text += text
        self.assertNotIn("mail", expected_text.lower())
        self.assertIn(
            "Resolved installed identity: "
            "REPLACE_WITH_EXACT_INSTALLED_R82_TAKE91_PACKAGE.tgz",
            expected_text,
        )
        self.assertIn("Selected candidate: EXAMPLE-GW-B only", expected_text)
        self.assertIn("Reboot observed: yes", expected_text)
        self.assertIn("Stabilization interval: 300 seconds completed", expected_text)
        self.assertIn("Runner return code: 20", expected_text)
        self.assertIn("Resume change identity: <same-change>", expected_text)
        self.assertIn("CHG after approved closure: Closed Successful", expected_text)
        self.assertIn("RITM terminal state: Complete", expected_text)
        self.assertNotIn("RITM terminal state: Closed Complete", expected_text)
        self.assertIn(
            "Local sanitized final ServiceNow snapshot: not present",
            expected_text,
        )


if __name__ == "__main__":
    unittest.main()
