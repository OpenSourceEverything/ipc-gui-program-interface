import json
import os
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
            fixture_target.write_text('{"configVersion":2,"id":"fixture","title":"Fixture","control":{"mode":"ipc","endpoint":"127.0.0.1:8766","appId":"fixture"},"ui":{"tabs":[]}}', encoding="utf-8")
            bridge_target.write_text('{"configVersion":2,"id":"bridge","title":"Bridge","control":{"mode":"ipc","endpoint":"127.0.0.1:8765","appId":"bridge"},"ui":{"tabs":[]}}', encoding="utf-8")

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
            fixture_target.write_text('{"configVersion":2,"id":"fixture","title":"Fixture","control":{"mode":"ipc","endpoint":"127.0.0.1:8766","appId":"fixture"},"ui":{"tabs":[]}}', encoding="utf-8")

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

    def test_requires_explicit_paths_when_env_is_empty(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "launch_monitor.py"
        self.assertTrue(script.exists())

        env = dict(os.environ)
        env.pop("FIXTURE_REPO", None)
        env.pop("BRIDGE_REPO", None)
        env.pop("FIXTURE_TARGET", None)
        env.pop("BRIDGE_TARGET", None)

        completed = subprocess.run(
            ["python", str(script), "--no-launch"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("fixture target requested", completed.stderr)

    def test_can_generate_config_from_generic_repo_flag(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "launch_monitor.py"
        self.assertTrue(script.exists())

        with tempfile.TemporaryDirectory() as app_tmp, tempfile.TemporaryDirectory() as out_tmp:
            app_repo = Path(app_tmp)
            target_a = app_repo / "config" / "gui" / "monitor.alpha.target.json"
            target_b = app_repo / "config" / "gui" / "monitor.beta.target.json"
            target_a.parent.mkdir(parents=True, exist_ok=True)
            target_a.write_text(
                '{"configVersion":2,"id":"alpha","title":"Alpha","control":{"mode":"ipc","endpoint":"127.0.0.1:8761","appId":"alpha"},"ui":{"tabs":[]}}',
                encoding="utf-8",
            )
            target_b.write_text(
                '{"configVersion":2,"id":"beta","title":"Beta","control":{"mode":"ipc","endpoint":"127.0.0.1:8762","appId":"beta"},"ui":{"tabs":[]}}',
                encoding="utf-8",
            )

            config_out = Path(out_tmp) / "generated-generic.json"
            cmd = [
                "python",
                str(script),
                "--repo",
                str(app_repo),
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
            self.assertTrue(any("monitor.alpha.target.json" in item for item in includes))
            self.assertTrue(any("monitor.beta.target.json" in item for item in includes))


if __name__ == "__main__":
    unittest.main()
