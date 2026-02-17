import os
import tempfile
import unittest
from pathlib import Path

import monitor


def _write_json(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class MonitorConfigValidationTests(unittest.TestCase):
    def test_rejects_unknown_root_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            _write_json(
                target,
                '{"configVersion":2,"id":"bridge","title":"Bridge","status":{"cwd":".","cmd":["python","-V"]},"ui":{"tabs":[]}}',
            )
            root_config = root / "monitor_config.json"
            _write_json(
                root_config,
                '{"refreshSeconds":1.0,"commandTimeoutSeconds":10.0,"includeFiles":["target.json"],"unknownKey":true}',
            )

            with self.assertRaises(ValueError):
                monitor.load_monitor_config(root_config)

    def test_allows_schema_metadata_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            _write_json(
                target,
                '{"$schema":"./monitor.target.v2.schema.json","configVersion":2,"id":"bridge","title":"Bridge","status":{"cwd":".","cmd":["python","-V"]},"ui":{"tabs":[]}}',
            )
            root_config = root / "monitor_config.json"
            _write_json(
                root_config,
                '{"$schema":"./schemas/monitor.root.schema.json","refreshSeconds":1.0,"commandTimeoutSeconds":10.0,"includeFiles":["target.json"]}',
            )

            loaded = monitor.load_monitor_config(root_config)
            targets = loaded.get("targets", [])
            self.assertEqual(len(targets), 1)
            self.assertEqual(str(targets[0].get("id") or ""), "bridge")

    def test_rejects_unknown_v2_target_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            _write_json(
                target,
                '{"configVersion":2,"id":"bridge","title":"Bridge","status":{"cwd":".","cmd":["python","-V"]},"ui":{"tabs":[]},"unknownKey":123}',
            )
            root_config = root / "monitor_config.json"
            _write_json(
                root_config,
                '{"refreshSeconds":1.0,"commandTimeoutSeconds":10.0,"includeFiles":["target.json"]}',
            )

            with self.assertRaises(ValueError):
                monitor.load_monitor_config(root_config)

    def test_accepts_config_editor_widget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            _write_json(
                target,
                (
                    '{"configVersion":2,"id":"bridge","title":"Bridge",'
                    '"status":{"cwd":".","cmd":["python","-V"]},'
                    '"ui":{"tabs":[{"id":"cfg","title":"Config","widgets":['
                    '{"type":"config_editor","title":"Editor","showAction":"config_show","setAction":"config_set_key"}'
                    ']}]},'
                    '"actions":[{"name":"config_show","label":"Show","cwd":".","cmd":["python","-V"]},'
                    '{"name":"config_set_key","label":"Set","cwd":".","cmd":["python","-V"]}]}'
                ),
            )
            root_config = root / "monitor_config.json"
            _write_json(
                root_config,
                '{"refreshSeconds":1.0,"commandTimeoutSeconds":10.0,"includeFiles":["target.json"]}',
            )

            loaded = monitor.load_monitor_config(root_config)
            targets = loaded.get("targets", [])
            self.assertEqual(len(targets), 1)

    def test_accepts_action_output_widget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            _write_json(
                target,
                (
                    '{"configVersion":2,"id":"bridge","title":"Bridge",'
                    '"status":{"cwd":".","cmd":["python","-V"]},'
                    '"ui":{"tabs":[{"id":"actions","title":"Actions","widgets":['
                    '{"type":"action_output","title":"Output"}'
                    ']}]}}'
                ),
            )
            root_config = root / "monitor_config.json"
            _write_json(
                root_config,
                '{"refreshSeconds":1.0,"commandTimeoutSeconds":10.0,"includeFiles":["target.json"]}',
            )

            loaded = monitor.load_monitor_config(root_config)
            targets = loaded.get("targets", [])
            self.assertEqual(len(targets), 1)

    def test_accepts_ipc_control_without_target_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            _write_json(
                target,
                (
                    '{"configVersion":2,"id":"bridge","title":"Bridge",'
                    '"status":{"cwd":".","cmd":["python","-V"]},'
                    '"control":{"mode":"ipc","endpoint":"127.0.0.1:8765","appId":"bridge"},'
                    '"ui":{"tabs":[{"id":"actions","title":"Actions","widgets":['
                    '{"type":"action_select","title":"Run Action"},'
                    '{"type":"action_output","title":"Output"}'
                    ']}]}}'
                ),
            )
            root_config = root / "monitor_config.json"
            _write_json(
                root_config,
                '{"refreshSeconds":1.0,"commandTimeoutSeconds":10.0,"includeFiles":["target.json"]}',
            )
            loaded = monitor.load_monitor_config(root_config)
            targets = loaded.get("targets", [])
            self.assertEqual(len(targets), 1)
            control = targets[0].get("control")
            self.assertIsInstance(control, dict)
            self.assertEqual(str(control.get("mode") or ""), "ipc")

    def test_rejects_ipc_control_missing_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            _write_json(
                target,
                (
                    '{"configVersion":2,"id":"bridge","title":"Bridge",'
                    '"status":{"cwd":".","cmd":["python","-V"]},'
                    '"control":{"mode":"ipc","appId":"bridge"},'
                    '"ui":{"tabs":[{"id":"actions","title":"Actions","widgets":[{"type":"action_output","title":"Output"}]}]}}'
                ),
            )
            root_config = root / "monitor_config.json"
            _write_json(
                root_config,
                '{"refreshSeconds":1.0,"commandTimeoutSeconds":10.0,"includeFiles":["target.json"]}',
            )
            with self.assertRaises(ValueError):
                monitor.load_monitor_config(root_config)

    def test_missing_jsonpath_returns_none(self):
        payload = {"a": {"b": 1}}
        self.assertIsNone(monitor.json_path_get(payload, "$.a.c"))
        self.assertIsNone(monitor.json_path_get(payload, "$.a.b[0]"))

    def test_latest_file_prefers_name_when_mtime_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a_file = root / "a.log"
            b_file = root / "b.log"
            a_file.write_text("a", encoding="utf-8")
            b_file.write_text("b", encoding="utf-8")

            mtime = 1_700_000_000
            os.utime(a_file, (mtime, mtime))
            os.utime(b_file, (mtime, mtime))

            selected = monitor.resolve_latest_file(str(root / "*.log"))
            self.assertIsNotNone(selected)
            self.assertEqual(selected.name, "b.log")


if __name__ == "__main__":
    unittest.main()
