import json
import unittest
from pathlib import Path


class CliSchemaTests(unittest.TestCase):
    def test_cli_schema_has_core_keys(self):
        schema_path = Path(__file__).resolve().parents[1] / "cli.schema.json"
        self.assertTrue(schema_path.exists(), "cli.schema.json must exist")
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIn("$schema", payload)
        self.assertIn("$defs", payload)
        self.assertEqual(payload.get("properties", {}).get("root", {}).get("const"), "dev")
        commands_items = payload.get("properties", {}).get("commands", {}).get("items", {})
        self.assertEqual(commands_items.get("$ref"), "#/$defs/command")

    def test_cli_schema_copies_are_identical(self):
        repo_root = Path(__file__).resolve().parents[1]
        root_schema = repo_root / "cli.schema.json"
        nested_schema = repo_root / "schemas" / "cli.schema.json"
        self.assertTrue(root_schema.exists())
        self.assertTrue(nested_schema.exists())
        self.assertEqual(
            json.loads(root_schema.read_text(encoding="utf-8")),
            json.loads(nested_schema.read_text(encoding="utf-8")),
        )

    def test_cli_schema_command_requires_expected_fields(self):
        schema_path = Path(__file__).resolve().parents[1] / "cli.schema.json"
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
        command_required = payload.get("$defs", {}).get("command", {}).get("required", [])
        self.assertEqual(command_required, ["id", "scope", "syntax", "brief"])
        scopes = payload.get("$defs", {}).get("command", {}).get("properties", {}).get("scope", {}).get("enum", [])
        self.assertEqual(scopes, ["universal", "fixture-only", "bridge-only"])


if __name__ == "__main__":
    unittest.main()
