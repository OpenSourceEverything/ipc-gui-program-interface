#!/usr/bin/env python3
"""Validate monitor target contract shape and runtime-facing adapters."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


REQUIRED_TOP_TAB_TITLES = {"status", "config", "actions", "logs"}
REQUIRED_BASE_STATUS_KEYS = ("appId", "appTitle", "running", "pid", "hostRunning", "hostPid")


def normalize_cmd(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def try_extract_json_object(output: str) -> tuple[dict[str, Any] | None, str]:
    text = (output or "").strip()
    if not text:
        return None, "empty command output"
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, ""
        return None, "output is not a JSON object"
    except Exception:
        pass

    decoder = json.JSONDecoder()
    first_brace = text.find("{")
    if first_brace < 0:
        return None, "failed to parse JSON object"
    best: dict[str, Any] | None = None
    best_span = -1
    for index, ch in enumerate(text[first_brace:]):
        if ch != "{":
            continue
        try:
            candidate, end_index = decoder.raw_decode(text[first_brace + index :])
        except Exception:
            continue
        if isinstance(candidate, dict) and int(end_index) > best_span:
            best = candidate
            best_span = int(end_index)
    if best is None:
        return None, "failed to parse JSON object"
    return best, ""


def collect_config_widgets(tabs: list[Any], prefix: str = "ui.tabs") -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for index, tab in enumerate(tabs, 1):
        if not isinstance(tab, dict):
            continue
        tab_id = str(tab.get("id") or f"tab-{index}")
        tab_path = f"{prefix}[{index}]({tab_id})"
        widgets = tab.get("widgets")
        if isinstance(widgets, list):
            for w_index, widget in enumerate(widgets, 1):
                if not isinstance(widget, dict):
                    continue
                widget_type = str(widget.get("type") or "").strip()
                if widget_type not in {"config_editor", "config_file_select"}:
                    continue
                result.append(
                    {
                        "tabPath": tab_path,
                        "widgetIndex": str(w_index),
                        "type": widget_type,
                        "showAction": str(widget.get("showAction") or "").strip(),
                        "setAction": str(widget.get("setAction") or "").strip(),
                        "pathKey": str(widget.get("pathKey") or "").strip(),
                        "key": str(widget.get("key") or "").strip(),
                    }
                )
        children = tab.get("children")
        if isinstance(children, list):
            result.extend(collect_config_widgets(children, prefix=f"{tab_path}.children"))
    return result


def collect_action_widgets(tabs: list[Any], prefix: str = "ui.tabs") -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, tab in enumerate(tabs, 1):
        if not isinstance(tab, dict):
            continue
        tab_id = str(tab.get("id") or f"tab-{index}")
        tab_path = f"{prefix}[{index}]({tab_id})"
        widgets = tab.get("widgets")
        if isinstance(widgets, list):
            for w_index, widget in enumerate(widgets, 1):
                if not isinstance(widget, dict):
                    continue
                widget_type = str(widget.get("type") or "").strip()
                if widget_type not in {"action_select", "action_map"}:
                    continue
                include_commands_raw = widget.get("includeCommands")
                include_commands: list[str] = []
                if isinstance(include_commands_raw, list):
                    include_commands = [str(item).strip() for item in include_commands_raw if str(item).strip()]
                result.append(
                    {
                        "tabPath": tab_path,
                        "widgetIndex": str(w_index),
                        "type": widget_type,
                        "includePrefix": str(widget.get("includePrefix") or "").strip(),
                        "includeRegex": str(widget.get("includeRegex") or "").strip(),
                        "includeCommands": include_commands,
                    }
                )
        children = tab.get("children")
        if isinstance(children, list):
            result.extend(collect_action_widgets(children, prefix=f"{tab_path}.children"))
    return result


def top_tab_titles(tabs: list[Any]) -> set[str]:
    titles: set[str] = set()
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        text = str(tab.get("title") or "").strip().lower()
        if text:
            titles.add(text)
    return titles


def _matches_widget_filters(action_name: str, widget: dict[str, Any]) -> bool:
    include_commands = widget.get("includeCommands")
    if isinstance(include_commands, list) and include_commands:
        return action_name in include_commands

    include_prefix = str(widget.get("includePrefix") or "").strip()
    include_regex = str(widget.get("includeRegex") or "").strip()

    if include_prefix and not action_name.startswith(include_prefix):
        return False

    if include_regex:
        pattern = re.compile(include_regex)
        if pattern.search(action_name) is None:
            return False

    return True


def run_action(action: dict[str, Any]) -> tuple[int, str, str, str]:
    cmd = normalize_cmd(action.get("cmd"))
    if not cmd:
        return 2, "", "", "empty cmd"
    if any("{" in part and "}" in part for part in cmd):
        return 2, "", "", "cmd contains placeholders and is not callable for contract check"
    cwd_text = str(action.get("cwd") or "").strip()
    cwd = Path(cwd_text) if cwd_text else None
    timeout = float(action.get("timeoutSeconds") or 30.0)
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 2, "", "", f"timeout after {timeout:.1f}s"
    except Exception as ex:
        return 2, "", "", str(ex)
    return int(completed.returncode), completed.stdout or "", completed.stderr or "", ""


def validate_status_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_BASE_STATUS_KEYS:
        if key not in payload:
            errors.append(f"status payload missing key: {key}")

    app_id = payload.get("appId")
    app_title = payload.get("appTitle")
    if not isinstance(app_id, str) or not app_id.strip():
        errors.append("status.appId must be a non-empty string")
    if not isinstance(app_title, str) or not app_title.strip():
        errors.append("status.appTitle must be a non-empty string")

    for bool_key in ("running", "hostRunning"):
        if bool_key in payload and not isinstance(payload.get(bool_key), bool):
            errors.append(f"status.{bool_key} must be bool")

    for int_key in ("pid", "hostPid"):
        value = payload.get(int_key)
        if value is not None and not isinstance(value, int):
            errors.append(f"status.{int_key} must be int|null")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Path to monitor.<name>.target.json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--skip-status-check", action="store_true")
    args = parser.parse_args()

    target_path = Path(args.target).resolve()
    if not target_path.exists():
        print(f"missing target: {target_path}", file=sys.stderr)
        return 2
    try:
        target = json.loads(target_path.read_text(encoding="utf-8-sig"))
    except Exception as ex:
        print(f"invalid JSON {target_path}: {ex}", file=sys.stderr)
        return 2
    if not isinstance(target, dict):
        print(f"target must be an object: {target_path}", file=sys.stderr)
        return 2

    actions_list = target.get("actions")
    actions: dict[str, dict[str, Any]] = {}
    action_names_in_order: list[str] = []
    if isinstance(actions_list, list):
        for action in actions_list:
            if not isinstance(action, dict):
                continue
            name = str(action.get("name") or "").strip()
            if name:
                action_names_in_order.append(name)
                actions[name] = action

    ui = target.get("ui")
    tabs = ui.get("tabs") if isinstance(ui, dict) and isinstance(ui.get("tabs"), list) else []
    config_widgets = collect_config_widgets(tabs)
    action_widgets = collect_action_widgets(tabs)

    errors: list[str] = []

    present_top_tabs = top_tab_titles(tabs)
    missing_top_tabs = sorted(REQUIRED_TOP_TAB_TITLES - present_top_tabs)
    if missing_top_tabs:
        errors.append(f"missing required top-level tabs: {', '.join(missing_top_tabs)}")

    duplicates = sorted({name for name in action_names_in_order if action_names_in_order.count(name) > 1})
    if duplicates:
        errors.append(f"duplicate action names: {', '.join(duplicates)}")

    for widget in action_widgets:
        include_commands = widget.get("includeCommands")
        if isinstance(include_commands, list) and include_commands:
            missing_cmd = [name for name in include_commands if name not in actions]
            if missing_cmd:
                errors.append(
                    f"{widget['tabPath']}[{widget['widgetIndex']}] includeCommands missing actions: {', '.join(missing_cmd)}"
                )
            continue

        include_regex = str(widget.get("includeRegex") or "").strip()
        if include_regex:
            try:
                re.compile(include_regex)
            except re.error as ex:
                errors.append(
                    f"{widget['tabPath']}[{widget['widgetIndex']}] invalid includeRegex '{include_regex}': {ex}"
                )
                continue

        matched = [name for name in actions.keys() if _matches_widget_filters(name, widget)]
        if not matched:
            errors.append(f"{widget['tabPath']}[{widget['widgetIndex']}] action filter resolves to zero actions")

    if not args.skip_status_check:
        status_cfg = target.get("status")
        if not isinstance(status_cfg, dict):
            errors.append("status section missing or invalid")
        else:
            status_action = {
                "name": "_status_check",
                "cmd": status_cfg.get("cmd"),
                "cwd": status_cfg.get("cwd"),
                "timeoutSeconds": status_cfg.get("timeoutSeconds", 30.0),
            }
            rc, stdout, stderr, err = run_action(status_action)
            if err:
                errors.append(f"status.cmd: {err}")
            elif rc != 0:
                message = (stderr or stdout or f"rc={rc}").strip().splitlines()[0]
                errors.append(f"status.cmd failed ({message})")
            else:
                payload, parse_error = try_extract_json_object(stdout)
                if payload is None:
                    errors.append(f"status.cmd: {parse_error}")
                else:
                    errors.extend(validate_status_payload(payload))

    for widget in config_widgets:
        show_action = widget["showAction"]
        set_action = widget["setAction"]
        if show_action not in actions:
            errors.append(f"{widget['tabPath']}[{widget['widgetIndex']}] showAction missing: {show_action}")
        if set_action not in actions:
            errors.append(f"{widget['tabPath']}[{widget['widgetIndex']}] setAction missing: {set_action}")

    show_payloads: dict[str, dict[str, Any]] = {}
    show_action_names = sorted({item["showAction"] for item in config_widgets if item["showAction"] in actions})
    for show_action in show_action_names:
        rc, stdout, stderr, err = run_action(actions[show_action])
        if err:
            errors.append(f"showAction {show_action}: {err}")
            continue
        payload, parse_error = try_extract_json_object(stdout)
        if payload is None:
            if rc != 0:
                message = (stderr or stdout or f"rc={rc}").strip().splitlines()[0]
                errors.append(f"showAction {show_action}: failed ({message})")
            else:
                errors.append(f"showAction {show_action}: {parse_error}")
            continue
        show_payloads[show_action] = payload

    for widget in config_widgets:
        show_action = widget["showAction"]
        payload = show_payloads.get(show_action)
        if not isinstance(payload, dict):
            continue
        entries_raw = payload.get("entries")
        paths_raw = payload.get("paths")
        entries = [item for item in entries_raw if isinstance(item, dict)] if isinstance(entries_raw, list) else []
        paths = [item for item in paths_raw if isinstance(item, dict)] if isinstance(paths_raw, list) else []
        entry_by_key = {str(item.get("key") or "").strip(): item for item in entries if str(item.get("key") or "").strip()}
        path_keys = {str(item.get("key") or "").strip() for item in paths if str(item.get("key") or "").strip()}

        path_key = widget["pathKey"]
        if path_key and path_key not in path_keys:
            errors.append(f"{widget['tabPath']}[{widget['widgetIndex']}] pathKey not in showAction paths: {path_key}")

        if widget["type"] == "config_file_select":
            key = widget["key"]
            if not key:
                errors.append(f"{widget['tabPath']}[{widget['widgetIndex']}] key is required for config_file_select")
                continue
            entry = entry_by_key.get(key)
            if entry is None:
                errors.append(f"{widget['tabPath']}[{widget['widgetIndex']}] key not in showAction entries: {key}")
                continue
            allowed = entry.get("allowed")
            if not isinstance(allowed, list) or len(allowed) == 0:
                errors.append(
                    f"{widget['tabPath']}[{widget['widgetIndex']}] key {key} must expose non-empty allowed[] list"
                )

    if errors:
        print(f"FAIL: {target_path}")
        for item in errors:
            print(f"- {item}")
        return 2

    print(
        "OK: "
        f"{target_path} "
        f"configWidgets={len(config_widgets)} "
        f"showActions={len(show_payloads)} "
        f"actionWidgets={len(action_widgets)}"
    )
    if args.verbose:
        for widget in config_widgets:
            print(
                f"- {widget['type']} {widget['tabPath']}[{widget['widgetIndex']}] "
                f"show={widget['showAction']} set={widget['setAction']} "
                f"pathKey={widget['pathKey']} key={widget['key']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
