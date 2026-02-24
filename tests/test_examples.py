import py_compile
import unittest
from pathlib import Path

import monitor


class ExampleAssetsTests(unittest.TestCase):
    def test_example_monitor_config_loads(self):
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "examples" / "monitor_config.example.json"
        self.assertTrue(config_path.exists(), f"missing example config: {config_path}")

        loaded = monitor.load_monitor_config(config_path)
        targets = loaded.get("targets", [])
        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(str(target.get("id") or ""), "sample-app")

    def test_example_ipc_server_script_compiles(self):
        repo_root = Path(__file__).resolve().parents[1]
        script_path = repo_root / "examples" / "minimal_ipc_server.py"
        self.assertTrue(script_path.exists(), f"missing example server: {script_path}")
        py_compile.compile(str(script_path), doraise=True)


if __name__ == "__main__":
    unittest.main()
