import subprocess
import tempfile
import unittest
from pathlib import Path


FULL_TABS_TARGET = (
    '{"configVersion":2,"id":"fixture","title":"Fixture","control":{"mode":"ipc","endpoint":"127.0.0.1:8766","appId":"fixture"},'
    '"ui":{"tabs":['
    '{"id":"status","title":"Status","widgets":[{"type":"text_block","title":"Status","text":"ok"}]},'
    '{"id":"config","title":"Config","widgets":[{"type":"text_block","title":"Config","text":"ok"}]},'
    '{"id":"actions","title":"Actions","widgets":[{"type":"text_block","title":"Actions","text":"ok"}]},'
    '{"id":"logs","title":"Logs","widgets":[{"type":"text_block","title":"Logs","text":"ok"}]}'
    ']}}'
)

MISSING_TABS_TARGET = (
    '{"configVersion":2,"id":"fixture","title":"Fixture","control":{"mode":"ipc","endpoint":"127.0.0.1:8766","appId":"fixture"},'
    '"ui":{"tabs":[{"id":"status","title":"Status","widgets":[{"type":"text_block","title":"Status","text":"ok"}]}]}}'
)


class CiTargetPolicyTests(unittest.TestCase):
    def test_passes_when_required_top_tabs_exist(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "ci_target_policy.py"

        with tempfile.TemporaryDirectory() as fixture_tmp:
            fixture_repo = Path(fixture_tmp)
            target = fixture_repo / "config" / "gui" / "monitor.fixture.target.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(FULL_TABS_TARGET, encoding="utf-8")

            completed = subprocess.run(
                [
                    "python",
                    str(script),
                    "--fixture-repo",
                    str(fixture_repo),
                    "--no-include-bridge",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, msg=f"stdout={completed.stdout}\nstderr={completed.stderr}")
        self.assertIn("OK: strict policy check passed", completed.stdout)

    def test_fails_when_required_top_tabs_missing(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "ci_target_policy.py"

        with tempfile.TemporaryDirectory() as fixture_tmp:
            fixture_repo = Path(fixture_tmp)
            target = fixture_repo / "config" / "gui" / "monitor.fixture.target.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(MISSING_TABS_TARGET, encoding="utf-8")

            completed = subprocess.run(
                [
                    "python",
                    str(script),
                    "--fixture-repo",
                    str(fixture_repo),
                    "--no-include-bridge",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("missing required top-level tabs", completed.stdout)

    def test_passes_with_generic_repo_flag(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "ci_target_policy.py"

        with tempfile.TemporaryDirectory() as app_tmp:
            app_repo = Path(app_tmp)
            target = app_repo / "config" / "gui" / "monitor.sample.target.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(FULL_TABS_TARGET, encoding="utf-8")

            completed = subprocess.run(
                [
                    "python",
                    str(script),
                    "--repo",
                    str(app_repo),
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, msg=f"stdout={completed.stdout}\nstderr={completed.stderr}")
        self.assertIn("OK: strict policy check passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
