from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import servicenow_checkpoint_readiness_worker as readiness


class ReadinessWorkerLockTests(unittest.TestCase):
    def test_readiness_captures_cluster_state_before_package_prerequisites(self) -> None:
        checks = readiness.readiness_checks({"package_steps": [{"name": "install_take"}]})
        playbooks = [playbook for _phase, playbook, _step, _extra in checks]

        capture = playbooks.index("11_capture_cluster_state.yml")
        prerequisites = playbooks.index("08_validate_package_prerequisites.yml")
        self.assertLess(capture, prerequisites)

    def test_second_worker_is_rejected_until_lock_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "readiness.lock"
            first = readiness.acquire_worker_lock(lock_path)
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    readiness.acquire_worker_lock(lock_path)
            finally:
                first.close()

            replacement = readiness.acquire_worker_lock(lock_path)
            replacement.close()


if __name__ == "__main__":
    unittest.main()
