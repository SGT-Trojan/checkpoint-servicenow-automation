#!/usr/bin/env python3
"""Run synthetic fail-closed examples without a network connection."""
from __future__ import annotations
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import servicenow_checkpoint_runner as runner  # noqa: E402

def expect(command: list[str], code: int, text: str) -> None:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    output = result.stdout + result.stderr
    if result.returncode != code or text not in output:
        raise RuntimeError(f"expected rc={code} and {text!r}; got rc={result.returncode}: {output}")

def main() -> int:
    with (Path(__file__).parent / "invalid-install.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    try:
        runner.package_steps_from_rows(rows)
    except ValueError as exc:
        assert "invalid SHA256" in str(exc)
    else:
        raise RuntimeError("placeholder hash was accepted")

    python = sys.executable
    resolver = ROOT / "ansible/scripts/discover_checkpoint_targets.py"
    phase = ROOT / "ansible/scripts/cluster_phase_control.py"
    direct = ROOT / "ansible/scripts/direct_package_step_from_activity.py"
    expect([python, str(resolver), "--mds-host", "192.0.2.10", "--target-ips", "not-an-ip"], 5, "ERROR[INVALID_INPUT]")
    expect([python, str(resolver)], 64, "required")
    expect([python, str(phase), "assert-member-take", "--members", "192.0.2.20", "192.0.2.21", "--state-file", "synthetic.json", "--target-host", "192.0.2.20"], 2, "--target-take is required")

    plan = json.loads((ROOT / "examples/activity_plans/patch-install.json").read_text())
    with tempfile.TemporaryDirectory() as directory:
        reports = Path(directory)
        state = {"original_active_host": "192.0.2.20", "original_standby_host": "192.0.2.21"}
        (reports / "cluster_initial_state_MANUAL_EXAMPLE.json").write_text(json.dumps(state))
        plan_path = reports / "plan.json"
        plan_path.write_text(json.dumps(plan))
        expect([python, str(direct), "--activity-plan-file", str(plan_path), "--reports-dir", str(reports), "--phase", "first-member", "--step", "install_approved_jhf"], 3, "Execution disabled")
    print("Expected failure examples passed")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
