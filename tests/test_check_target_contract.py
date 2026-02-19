import subprocess
import unittest
from pathlib import Path


class CheckTargetContractTests(unittest.TestCase):
    def test_accepts_ipc_target_without_local_actions(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "check_target_contract.py"
        target = repo_root / "contract" / "golden" / "target.v2.config_widgets.json"

        completed = subprocess.run(
            ["python", str(script), "--target", str(target)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=f"stdout={completed.stdout}\nstderr={completed.stderr}")

    def test_top_tab_policy_is_opt_in(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "check_target_contract.py"
        target = repo_root / "contract" / "golden" / "target.v2.ipc.min.json"

        default_run = subprocess.run(
            ["python", str(script), "--target", str(target)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(default_run.returncode, 0, msg=f"stdout={default_run.stdout}\nstderr={default_run.stderr}")

        strict_run = subprocess.run(
            ["python", str(script), "--target", str(target), "--enforce-top-tabs"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(strict_run.returncode, 0)
        self.assertIn("missing required top-level tabs", strict_run.stdout)


if __name__ == "__main__":
    unittest.main()
