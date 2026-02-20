import subprocess
import tempfile
import unittest
from pathlib import Path


class SyncTargetSchemaTests(unittest.TestCase):
    def test_sync_copies_schema_to_both_repos(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "sync_target_schema.py"
        self.assertTrue(script.exists())

        with tempfile.TemporaryDirectory() as fixture_tmp, tempfile.TemporaryDirectory() as bridge_tmp:
            fixture_repo = Path(fixture_tmp)
            bridge_repo = Path(bridge_tmp)

            cmd = [
                "python",
                str(script),
                "--fixture-repo",
                str(fixture_repo),
                "--bridge-repo",
                str(bridge_repo),
            ]
            completed = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)

            fixture_schema = fixture_repo / "config" / "gui" / "monitor.target.v2.schema.json"
            bridge_schema = bridge_repo / "config" / "gui" / "monitor.target.v2.schema.json"
            self.assertTrue(fixture_schema.exists())
            self.assertTrue(bridge_schema.exists())

    def test_sync_copies_schema_with_generic_repo_flag(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "sync_target_schema.py"
        self.assertTrue(script.exists())

        with tempfile.TemporaryDirectory() as app_a_tmp, tempfile.TemporaryDirectory() as app_b_tmp:
            app_repo_a = Path(app_a_tmp)
            app_repo_b = Path(app_b_tmp)

            cmd = [
                "python",
                str(script),
                "--repo",
                str(app_repo_a),
                "--repo",
                str(app_repo_b),
            ]
            completed = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)

            schema_a = app_repo_a / "config" / "gui" / "monitor.target.v2.schema.json"
            schema_b = app_repo_b / "config" / "gui" / "monitor.target.v2.schema.json"
            self.assertTrue(schema_a.exists())
            self.assertTrue(schema_b.exists())


if __name__ == "__main__":
    unittest.main()
