import json
import tempfile
import unittest
from pathlib import Path

import monitor


class ContractGoldenExamplesTests(unittest.TestCase):
    def test_loads_all_target_golden_examples(self):
        repo_root = Path(__file__).resolve().parents[1]
        golden_dir = repo_root / "contract" / "golden"
        target_files = sorted(golden_dir.glob("target.v2.*.json"))
        self.assertGreaterEqual(len(target_files), 2)

        with tempfile.TemporaryDirectory() as tmp:
            root_config = Path(tmp) / "monitor_config.json"
            payload = {
                "refreshSeconds": 1.0,
                "commandTimeoutSeconds": 10.0,
                "includeFiles": [str(path.resolve()) for path in target_files],
            }
            root_config.write_text(json.dumps(payload), encoding="utf-8")

            loaded = monitor.load_monitor_config(root_config)
            targets = loaded.get("targets", [])
            ids = {str(item.get("id") or "") for item in targets if isinstance(item, dict)}

        self.assertIn("sample-ipc", ids)
        self.assertIn("sample-config", ids)

    def test_canonical_config_show_payload_shape_is_supported(self):
        repo_root = Path(__file__).resolve().parents[1]
        payload_path = repo_root / "contract" / "golden" / "config_show_payload.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8-sig"))

        normalized = monitor._normalize_config_show_payload(payload)
        self.assertIsInstance(normalized.get("paths"), list)
        self.assertIsInstance(normalized.get("entries"), list)

        entries = normalized.get("entries") or []
        profile = next((item for item in entries if isinstance(item, dict) and item.get("key") == "profile"), None)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.get("allowed"), ["sim", "lab"])

    def test_legacy_config_show_payload_shape_is_supported(self):
        legacy_payload = {
            "paths": {
                "profile": "C:/repos/sample/config/profiles/sim.json"
            },
            "entries": [
                {
                    "key": "profile",
                    "value": "sim",
                    "allowedValues": ["sim", "lab"]
                }
            ],
        }

        normalized = monitor._normalize_config_show_payload(legacy_payload)
        paths = normalized.get("paths")
        entries = normalized.get("entries")

        self.assertIsInstance(paths, list)
        self.assertIsInstance(entries, list)
        self.assertEqual(paths[0].get("key"), "profile")
        self.assertEqual(entries[0].get("allowed"), ["sim", "lab"])
        self.assertEqual(entries[0].get("path"), "C:/repos/sample/config/profiles/sim.json")


if __name__ == "__main__":
    unittest.main()
