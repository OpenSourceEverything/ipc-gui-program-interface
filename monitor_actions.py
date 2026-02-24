"""Pure action helper functions used by monitor runtime."""

from __future__ import annotations

import re
from typing import Any

from monitor_ipc import json_path_get


def _normalize_cmd(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = [str(part) for part in value if str(part).strip()]
    return result


def _action_primary_arg(action: dict[str, Any]) -> dict[str, Any] | None:
    args_raw = action.get("args")
    if not isinstance(args_raw, list) or not args_raw:
        return None
    first = args_raw[0]
    if not isinstance(first, dict):
        return None
    name = str(first.get("name") or "").strip()
    if not name:
        return None
    return first


def _action_arg_options(arg_spec: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    options_raw = arg_spec.get("options")
    if isinstance(options_raw, list):
        return [str(item) for item in options_raw if str(item).strip()]
    options_path = str(arg_spec.get("optionsJsonpath") or "").strip()
    if options_path:
        options_value = json_path_get(payload, options_path)
        if isinstance(options_value, list):
            return [str(item) for item in options_value if str(item).strip()]
    return []


def _validate_action_arg_value(raw_value: str, arg_spec: dict[str, Any], options: list[str]) -> tuple[str | None, str | None]:
    name = str(arg_spec.get("name") or "value").strip() or "value"
    required = bool(arg_spec.get("required", False))
    arg_type = str(arg_spec.get("type") or "string").strip().lower()
    pattern = str(arg_spec.get("pattern") or "").strip()
    text = str(raw_value or "").strip()

    if required and not text:
        return None, f"{name}: value is required."
    if not text:
        return "", None

    if options and text not in options:
        return None, f"{name}: value must be one of available options."

    if arg_type == "int":
        try:
            return str(int(text)), None
        except Exception:
            return None, f"{name}: value must be an integer."
    if arg_type == "float":
        try:
            return str(float(text)), None
        except Exception:
            return None, f"{name}: value must be a number."
    if arg_type == "bool":
        lowered = text.lower()
        if lowered in {"true", "1", "yes", "y"}:
            return "true", None
        if lowered in {"false", "0", "no", "n"}:
            return "false", None
        return None, f"{name}: value must be true/false."

    if pattern:
        try:
            if re.search(pattern, text) is None:
                return None, f"{name}: value does not match required pattern."
        except re.error:
            return None, f"{name}: invalid regex pattern in action metadata."

    return text, None


def _apply_action_placeholders(parts: list[str], values: dict[str, str]) -> list[str]:
    result = list(parts)
    for key, value in values.items():
        token = "{" + str(key) + "}"
        result = [part.replace(token, str(value)) for part in result]
    return result
