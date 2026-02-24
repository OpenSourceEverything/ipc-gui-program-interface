import json
import tempfile
import unittest
from pathlib import Path

import monitor


FIXTURE_TARGET = Path(r"\\H3FT06-40318\c\40318-SOFT\config\gui\monitor.fixture.target.json")
BRIDGE_TARGET = Path(r"C:\repos\test-fixture-data-bridge\config\gui\monitor.bridge.target.json")


class MonitorSmokeConfigTests(unittest.TestCase):
    def test_loads_real_fixture_and_bridge_targets_when_available(self):
        if not FIXTURE_TARGET.exists() or not BRIDGE_TARGET.exists():
            self.skipTest("fixture/bridge target files are not present on this machine")

        with tempfile.TemporaryDirectory() as tmp:
            root_config = Path(tmp) / "monitor.root.json"
            payload = {
                "refreshSeconds": 1.0,
                "commandTimeoutSeconds": 10.0,
                "includeFiles": [str(FIXTURE_TARGET), str(BRIDGE_TARGET)],
            }
            root_config.write_text(json.dumps(payload), encoding="utf-8")

            config = monitor.load_monitor_config(root_config)
            targets = config.get("targets", [])
            self.assertGreaterEqual(len(targets), 2)

            ids = {str(item.get("id") or "") for item in targets if isinstance(item, dict)}
            self.assertTrue("fixture" in ids or "fixture-40318" in ids)
            self.assertIn("bridge", ids)


if __name__ == "__main__":
    unittest.main()
