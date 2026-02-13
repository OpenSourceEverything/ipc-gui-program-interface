import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class LaunchMonitorTests(unittest.TestCase):
    def test_generates_root_config_from_repo_paths(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "launch_monitor.py"
        self.assertTrue(script.exists())

        with tempfile.TemporaryDirectory() as fixture_tmp, tempfile.TemporaryDirectory() as bridge_tmp, tempfile.TemporaryDirectory() as out_tmp:
            fixture_repo = Path(fixture_tmp)
            bridge_repo = Path(bridge_tmp)
            fixture_target = fixture_repo / "config" / "gui" / "monitor.fixture.target.json"
            bridge_target = bridge_repo / "config" / "gui" / "monitor.bridge.target.json"
            fixture_target.parent.mkdir(parents=True, exist_ok=True)
            bridge_target.parent.mkdir(parents=True, exist_ok=True)
            fixture_target.write_text('{"configVersion":2,"id":"fixture","title":"Fixture","status":{"cwd":".","cmd":["python","-V"]},"ui":{"tabs":[]}}', encoding="utf-8")
            bridge_target.write_text('{"configVersion":2,"id":"bridge","title":"Bridge","status":{"cwd":".","cmd":["python","-V"]},"ui":{"tabs":[]}}', encoding="utf-8")

            config_out = Path(out_tmp) / "generated.json"
            cmd = [
                "python",
                str(script),
                "--fixture-repo",
                str(fixture_repo),
                "--bridge-repo",
                str(bridge_repo),
                "--config-out",
                str(config_out),
                "--no-launch",
            ]
            completed = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            self.assertTrue(config_out.exists())

            payload = json.loads(config_out.read_text(encoding="utf-8"))
            includes = payload.get("includeFiles", [])
            self.assertEqual(len(includes), 2)
            self.assertTrue(any("monitor.fixture.target.json" in item for item in includes))
            self.assertTrue(any("monitor.bridge.target.json" in item for item in includes))

    def test_can_generate_fixture_only_config(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "launch_monitor.py"
        self.assertTrue(script.exists())

        with tempfile.TemporaryDirectory() as fixture_tmp, tempfile.TemporaryDirectory() as bridge_tmp, tempfile.TemporaryDirectory() as out_tmp:
            fixture_repo = Path(fixture_tmp)
            bridge_repo = Path(bridge_tmp)
            fixture_target = fixture_repo / "config" / "gui" / "monitor.fixture.target.json"
            fixture_target.parent.mkdir(parents=True, exist_ok=True)
            fixture_target.write_text('{"configVersion":2,"id":"fixture","title":"Fixture","status":{"cwd":".","cmd":["python","-V"]},"ui":{"tabs":[]}}', encoding="utf-8")

            config_out = Path(out_tmp) / "generated-fixture-only.json"
            cmd = [
                "python",
                str(script),
                "--fixture-repo",
                str(fixture_repo),
                "--bridge-repo",
                str(bridge_repo),
                "--no-include-bridge",
                "--config-out",
                str(config_out),
                "--no-launch",
            ]
            completed = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            self.assertTrue(config_out.exists())

            payload = json.loads(config_out.read_text(encoding="utf-8"))
            includes = payload.get("includeFiles", [])
            self.assertEqual(len(includes), 1)
            self.assertTrue(any("monitor.fixture.target.json" in item for item in includes))


if __name__ == "__main__":
    unittest.main()
