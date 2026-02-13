#!/usr/bin/env python3
"""Generic JSON-driven monitor for fixture/bridge status and logs."""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any


DEFAULT_REFRESH_SECONDS = 1.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 10.0
DEFAULT_ACTION_OUTPUT_MAX_LINES = 1200
DEFAULT_ACTION_OUTPUT_MAX_BYTES = 1_000_000
MIN_REFRESH_TICK_SECONDS = 0.2


def _assert_allowed_keys(
    obj: dict[str, Any],
    allowed: set[str],
    context: str,
    *,
    allow_prefixes: tuple[str, ...] = ("x-",),
) -> None:
    extras: list[str] = []
    for key in obj.keys():
        if key in allowed:
            continue
        if key == "$schema":
            continue
        if any(str(key).startswith(prefix) for prefix in allow_prefixes):
            continue
        extras.append(str(key))
    extras = sorted(set(extras))
    if extras:
        raise ValueError(f"{context} has unsupported keys: {', '.join(extras)}")


def _require_string_list(value: Any, context: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list.")
    result: list[str] = []
    for index, item in enumerate(value, 1):
        text = str(item).strip()
        if not text:
            raise ValueError(f"{context}[{index}] must be a non-empty string.")
        result.append(text)
    if not result:
        raise ValueError(f"{context} must contain at least one item.")
    return result


def _validate_root_config_payload(base: dict[str, Any], source_path: Path) -> None:
    _assert_allowed_keys(
        base,
        {"refreshSeconds", "commandTimeoutSeconds", "actionOutput", "includeFiles"},
        f"Root config {source_path}",
    )
    _require_string_list(base.get("includeFiles"), f"{source_path} includeFiles")

    action_output = base.get("actionOutput")
    if action_output is None:
        return
    if not isinstance(action_output, dict):
        raise ValueError(f"{source_path} actionOutput must be an object.")
    _assert_allowed_keys(action_output, {"maxLines", "maxBytes"}, f"{source_path} actionOutput")


def _validate_v2_widget(widget: dict[str, Any], context: str) -> None:
    widget_type = str(widget.get("type") or "").strip().lower()
    if widget_type == "kv":
        _assert_allowed_keys(widget, {"type", "title", "items"}, context)
        items = widget.get("items")
        if not isinstance(items, list):
            raise ValueError(f"{context}.items must be a list.")
        for idx, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise ValueError(f"{context}.items[{idx}] must be an object.")
            _assert_allowed_keys(item, {"label", "jsonpath"}, f"{context}.items[{idx}]")
        return

    if widget_type == "table":
        _assert_allowed_keys(widget, {"type", "title", "columns"}, context)
        columns = widget.get("columns")
        if not isinstance(columns, list):
            raise ValueError(f"{context}.columns must be a list.")
        for idx, item in enumerate(columns, 1):
            if not isinstance(item, dict):
                raise ValueError(f"{context}.columns[{idx}] must be an object.")
            _assert_allowed_keys(item, {"label", "jsonpath"}, f"{context}.columns[{idx}]")
        return

    if widget_type == "log":
        _assert_allowed_keys(
            widget,
            {"type", "title", "stream", "showPath", "openPathButton", "copyPathButton"},
            context,
        )
        return

    if widget_type == "button":
        _assert_allowed_keys(widget, {"type", "label", "action"}, context)
        return

    if widget_type == "profile_select":
        _assert_allowed_keys(
            widget,
            {"type", "title", "action", "optionsJsonpath", "currentJsonpath", "emptyLabel", "applyLabel"},
            context,
        )
        return

    if widget_type == "action_map":
        _assert_allowed_keys(widget, {"type", "title", "includeCommands", "showActionName", "includePrefix"}, context)
        return

    if widget_type == "action_select":
        _assert_allowed_keys(
            widget,
            {"type", "title", "includePrefix", "includeRegex", "emptyLabel", "runLabel", "showCommand"},
            context,
        )
        return

    if widget_type == "file_view":
        _assert_allowed_keys(
            widget,
            {"type", "title", "pathJsonpath", "pathLiteral", "maxBytes", "encoding"},
            context,
        )
        return

    raise ValueError(f"{context} has unsupported widget type '{widget_type or '(blank)'}'.")


def _validate_v2_tab(tab: dict[str, Any], source_path: Path, context: str) -> None:
    _assert_allowed_keys(tab, {"id", "title", "widgets", "children"}, f"{context} in {source_path}")
    widgets = tab.get("widgets")
    children = tab.get("children")

    if widgets is None and children is None:
        raise ValueError(f"{context} in {source_path} must define widgets or children.")

    if widgets is not None:
        if not isinstance(widgets, list):
            raise ValueError(f"{context}.widgets in {source_path} must be a list.")
        for widget_index, widget in enumerate(widgets, 1):
            if not isinstance(widget, dict):
                raise ValueError(f"{context}.widgets[{widget_index}] in {source_path} must be an object.")
            _validate_v2_widget(widget, f"{context}.widgets[{widget_index}] in {source_path}")

    if children is not None:
        if not isinstance(children, list):
            raise ValueError(f"{context}.children in {source_path} must be a list.")
        for child_index, child in enumerate(children, 1):
            if not isinstance(child, dict):
                raise ValueError(f"{context}.children[{child_index}] in {source_path} must be an object.")
            _validate_v2_tab(child, source_path, f"{context}.children[{child_index}]")


def _validate_v2_target_payload(target: dict[str, Any], source_path: Path, context: str) -> None:
    _assert_allowed_keys(
        target,
        {"configVersion", "id", "title", "refreshSeconds", "status", "logs", "actions", "ui", "actionOutput"},
        f"{context} in {source_path}",
    )

    status = target.get("status")
    if not isinstance(status, dict):
        raise ValueError(f"{context} in {source_path} is missing status object.")
    _assert_allowed_keys(status, {"cwd", "cmd", "timeoutSeconds"}, f"{context}.status in {source_path}")

    logs = target.get("logs")
    if isinstance(logs, list):
        for idx, log in enumerate(logs, 1):
            if not isinstance(log, dict):
                raise ValueError(f"{context}.logs[{idx}] in {source_path} must be an object.")
            _assert_allowed_keys(
                log,
                {"stream", "title", "glob", "tailLines", "maxLineBytes", "pollMs", "encoding", "allowMissing"},
                f"{context}.logs[{idx}] in {source_path}",
            )

    actions = target.get("actions")
    if isinstance(actions, list):
        for idx, action in enumerate(actions, 1):
            if not isinstance(action, dict):
                raise ValueError(f"{context}.actions[{idx}] in {source_path} must be an object.")
            _assert_allowed_keys(
                action,
                {"name", "label", "cwd", "cmd", "timeoutSeconds", "confirm", "showOutputPanel", "mutex", "detached"},
                f"{context}.actions[{idx}] in {source_path}",
            )

    ui = target.get("ui")
    if not isinstance(ui, dict):
        raise ValueError(f"{context} in {source_path} is missing ui object.")
    _assert_allowed_keys(ui, {"tabs"}, f"{context}.ui in {source_path}")
    tabs = ui.get("tabs")
    if not isinstance(tabs, list):
        raise ValueError(f"{context}.ui.tabs in {source_path} must be a list.")

    for tab_index, tab in enumerate(tabs, 1):
        if not isinstance(tab, dict):
            raise ValueError(f"{context}.ui.tabs[{tab_index}] in {source_path} must be an object.")
        _validate_v2_tab(tab, source_path, f"{context}.ui.tabs[{tab_index}]")

    action_output = target.get("actionOutput")
    if action_output is not None:
        if not isinstance(action_output, dict):
            raise ValueError(f"{context}.actionOutput in {source_path} must be an object.")
        _assert_allowed_keys(action_output, {"maxLines", "maxBytes"}, f"{context}.actionOutput in {source_path}")


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object JSON: {path}")
    return payload


def resolve_path(base_path: Path, path_value: str) -> Path:
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return (base_path.parent / candidate).resolve()


def slugify(text: str, fallback: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
    compact = "-".join(part for part in cleaned.split("-") if part)
    return compact or fallback


def dot_key_to_jsonpath(key: str) -> str:
    parts = [part.strip() for part in str(key).split(".") if part.strip()]
    if not parts:
        return "$"
    return "$." + ".".join(parts)


def as_target_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    target = payload.get("target")
    if isinstance(target, dict):
        items.append(target)
    targets = payload.get("targets")
    if isinstance(targets, list):
        for entry in targets:
            if isinstance(entry, dict):
                items.append(entry)
    return items


def as_log_panel_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    panels = payload.get("logPanels")
    if not isinstance(panels, list):
        return []
    return [entry for entry in panels if isinstance(entry, dict)]


def try_extract_json_object(output: str) -> tuple[dict[str, Any] | None, str]:
    text = (output or "").strip()
    if not text:
        return None, "empty status output"

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload, ""
        return None, "status output is not a JSON object"
    except Exception:
        pass

    first_brace = text.find("{")
    if first_brace >= 0:
        decoder = json.JSONDecoder()
        best: dict[str, Any] | None = None
        best_span = -1
        for index, char in enumerate(text[first_brace:]):
            if char != "{":
                continue
            try:
                payload, end_index = decoder.raw_decode(text[first_brace + index :])
            except Exception:
                continue
            if isinstance(payload, dict):
                span = int(end_index)
                if span > best_span:
                    best = payload
                    best_span = span
        if best is not None:
            return best, ""

    return None, "failed to parse JSON object from status output"


def run_cmd(cmd: list[str], cwd: Path | None, timeout_seconds: float) -> tuple[int, str, str]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    return int(completed.returncode), completed.stdout or "", completed.stderr or ""


def _iter_jsonpath_tokens(path: str) -> list[str | int] | None:
    text = str(path or "").strip()
    if not text.startswith("$"):
        return None
    if text == "$":
        return []

    tokens: list[str | int] = []
    index = 1
    length = len(text)
    while index < length:
        current = text[index]
        if current == ".":
            index += 1
            start = index
            while index < length and text[index] not in ".[":
                index += 1
            key = text[start:index]
            if not key:
                return None
            tokens.append(key)
            continue

        if current == "[":
            index += 1
            start = index
            while index < length and text[index].isdigit():
                index += 1
            if start == index or index >= length or text[index] != "]":
                return None
            tokens.append(int(text[start:index]))
            index += 1
            continue

        return None

    return tokens


def json_path_get(payload: Any, path: str) -> Any | None:
    tokens = _iter_jsonpath_tokens(path)
    if tokens is None:
        return None

    node: Any = payload
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(node, list):
                return None
            if token < 0 or token >= len(node):
                return None
            node = node[token]
            continue

        if not isinstance(node, dict):
            return None
        if token not in node:
            return None
        node = node[token]
    return node


def render_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        return text if len(text) <= 200 else text[:197] + "..."
    return str(value)


def resolve_latest_file(path_expr: str) -> Path | None:
    expression = str(path_expr or "").strip()
    if not expression:
        return None

    has_glob = any(token in expression for token in ("*", "?", "["))
    if not has_glob:
        candidate = Path(expression)
        if candidate.exists() and candidate.is_file():
            return candidate
        return None

    newest: tuple[int, str, Path] | None = None
    for item in glob.glob(expression, recursive=True):
        candidate = Path(item)
        if not candidate.is_file():
            continue
        try:
            mtime_ns = candidate.stat().st_mtime_ns
        except OSError:
            continue
        key = (int(mtime_ns), str(candidate))
        if newest is None or key > (newest[0], newest[1]):
            newest = (key[0], key[1], candidate)
    return newest[2] if newest else None


def tail_lines(path: Path, max_lines: int, encoding: str = "utf-8") -> str:
    if not path.exists() or not path.is_file():
        return ""

    wanted = max(1, int(max_lines))
    chunk_size = 8192
    max_bytes = 2 * 1024 * 1024

    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            buffer = b""
            while position > 0 and buffer.count(b"\n") <= wanted and len(buffer) < max_bytes:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
    except Exception:
        return ""

    text = buffer.decode(encoding, errors="ignore")
    lines = text.splitlines()
    return "\n".join(lines[-wanted:]).strip()


def _normalize_cmd(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = [str(part) for part in value if str(part).strip()]
    return result


def _normalize_v1_include(
    payload: dict[str, Any],
    source_path: Path,
    *,
    default_refresh_seconds: float,
    default_timeout_seconds: float,
    default_action_output_max_lines: int,
    default_action_output_max_bytes: int,
) -> list[dict[str, Any]]:
    targets = as_target_list(payload)
    if not targets:
        return []

    log_panels = as_log_panel_list(payload)
    normalized_targets: list[dict[str, Any]] = []

    for index, target in enumerate(targets, 1):
        tid = str(target.get("id") or f"{source_path.stem}-{index}")
        title = str(target.get("name") or target.get("title") or tid)
        cwd_value = str(target.get("cwd") or "").strip()
        status_cmd = _normalize_cmd(target.get("statusCommand"))
        if not status_cmd:
            raise ValueError(f"v1 target '{tid}' in {source_path} is missing statusCommand.")

        logs: list[dict[str, Any]] = []
        for log_index, panel in enumerate(log_panels, 1):
            stream = slugify(str(panel.get("name") or f"log-{log_index}"), f"log-{log_index}")
            logs.append(
                {
                    "stream": stream,
                    "title": str(panel.get("name") or stream),
                    "glob": str(panel.get("path") or ""),
                    "tailLines": int(panel.get("tailLines", 120)),
                    "maxLineBytes": 4096,
                    "pollMs": 500,
                    "encoding": "utf-8",
                    "allowMissing": True,
                }
            )

        actions: list[dict[str, Any]] = []
        commands = target.get("commands")
        if isinstance(commands, list):
            for action_index, command in enumerate(commands, 1):
                if not isinstance(command, dict):
                    continue
                label = str(command.get("label") or f"Action {action_index}")
                name = slugify(str(command.get("name") or label), f"action-{action_index}")
                action_cmd = _normalize_cmd(command.get("command"))
                if not action_cmd:
                    continue
                action_cwd = str(command.get("cwd") or cwd_value).strip()
                actions.append(
                    {
                        "name": name,
                        "label": label,
                        "cwd": action_cwd,
                        "cmd": action_cmd,
                        "timeoutSeconds": float(command.get("timeoutSeconds", 120.0)),
                        "confirm": str(command.get("confirm") or ""),
                        "showOutputPanel": bool(command.get("showOutputPanel", True)),
                        "mutex": str(command.get("mutex") or ""),
                        "detached": bool(command.get("detached", False)),
                    }
                )

        status_items: list[dict[str, str]] = []
        fields = target.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                key = str(field.get("key") or "").strip()
                if not key:
                    continue
                status_items.append(
                    {
                        "label": str(field.get("label") or key),
                        "jsonpath": dot_key_to_jsonpath(key),
                    }
                )

        ui_tabs: list[dict[str, Any]] = []
        if status_items:
            ui_tabs.append(
                {
                    "id": "status",
                    "title": "Status",
                    "widgets": [{"type": "kv", "title": "Status", "items": status_items}],
                }
            )
        if logs:
            ui_tabs.append(
                {
                    "id": "logs",
                    "title": "Logs",
                    "widgets": [
                        {"type": "log", "title": str(log["title"]), "stream": str(log["stream"])} for log in logs
                    ],
                }
            )
        if actions:
            ui_tabs.append(
                {
                    "id": "actions",
                    "title": "Actions",
                    "widgets": [
                        {"type": "button", "label": str(action["label"]), "action": str(action["name"])}
                        for action in actions
                    ],
                }
            )
        if not ui_tabs:
            ui_tabs.append({"id": "status", "title": "Status", "widgets": []})

        normalized_targets.append(
            {
                "configVersion": 1,
                "id": tid,
                "title": title,
                "refreshSeconds": float(target.get("refreshSeconds", default_refresh_seconds)),
                "status": {
                    "cwd": cwd_value,
                    "cmd": status_cmd,
                    "timeoutSeconds": float(target.get("statusTimeoutSeconds", default_timeout_seconds)),
                },
                "logs": logs,
                "actions": actions,
                "ui": {"tabs": ui_tabs},
                "actionOutput": {
                    "maxLines": int(default_action_output_max_lines),
                    "maxBytes": int(default_action_output_max_bytes),
                },
                "sourcePath": str(source_path),
            }
        )

    return normalized_targets


def _normalize_v2_target(
    target: dict[str, Any],
    source_path: Path,
    *,
    default_refresh_seconds: float,
    default_timeout_seconds: float,
    default_action_output_max_lines: int,
    default_action_output_max_bytes: int,
) -> dict[str, Any]:
    tid = str(target.get("id") or "").strip()
    if not tid:
        raise ValueError(f"v2 target in {source_path} is missing id.")
    title = str(target.get("title") or tid)

    status = target.get("status")
    if not isinstance(status, dict):
        raise ValueError(f"v2 target '{tid}' in {source_path} is missing status object.")
    status_cmd = _normalize_cmd(status.get("cmd"))
    if not status_cmd:
        raise ValueError(f"v2 target '{tid}' in {source_path} has empty status.cmd.")

    status_cwd = str(status.get("cwd") or "").strip()
    status_timeout = float(status.get("timeoutSeconds", default_timeout_seconds))

    logs: list[dict[str, Any]] = []
    logs_raw = target.get("logs")
    if isinstance(logs_raw, list):
        for idx, log in enumerate(logs_raw, 1):
            if not isinstance(log, dict):
                continue
            stream = str(log.get("stream") or "").strip()
            if not stream:
                raise ValueError(f"v2 target '{tid}' in {source_path} has logs[{idx}] without stream.")
            logs.append(
                {
                    "stream": stream,
                    "title": str(log.get("title") or stream),
                    "glob": str(log.get("glob") or ""),
                    "tailLines": int(log.get("tailLines", 300)),
                    "maxLineBytes": int(log.get("maxLineBytes", 4096)),
                    "pollMs": int(log.get("pollMs", 500)),
                    "encoding": str(log.get("encoding") or "utf-8"),
                    "allowMissing": bool(log.get("allowMissing", True)),
                }
            )

    actions: list[dict[str, Any]] = []
    actions_raw = target.get("actions")
    if isinstance(actions_raw, list):
        for idx, action in enumerate(actions_raw, 1):
            if not isinstance(action, dict):
                continue
            name = str(action.get("name") or "").strip()
            if not name:
                raise ValueError(f"v2 target '{tid}' in {source_path} has actions[{idx}] without name.")
            cmd = _normalize_cmd(action.get("cmd"))
            if not cmd:
                raise ValueError(f"v2 target '{tid}' in {source_path} action '{name}' has empty cmd.")
            action_cwd = str(action.get("cwd") or status_cwd).strip()
            actions.append(
                {
                    "name": name,
                    "label": str(action.get("label") or name),
                    "cwd": action_cwd,
                    "cmd": cmd,
                    "timeoutSeconds": float(action.get("timeoutSeconds", 120.0)),
                    "confirm": str(action.get("confirm") or ""),
                    "showOutputPanel": bool(action.get("showOutputPanel", True)),
                    "mutex": str(action.get("mutex") or ""),
                    "detached": bool(action.get("detached", False)),
                }
            )

    ui = target.get("ui")
    if not isinstance(ui, dict):
        raise ValueError(f"v2 target '{tid}' in {source_path} is missing ui object.")
    tabs = ui.get("tabs")
    if not isinstance(tabs, list):
        raise ValueError(f"v2 target '{tid}' in {source_path} ui.tabs must be a list.")

    action_output = target.get("actionOutput")
    action_output_obj = action_output if isinstance(action_output, dict) else {}

    return {
        "configVersion": 2,
        "id": tid,
        "title": title,
        "refreshSeconds": float(target.get("refreshSeconds", default_refresh_seconds)),
        "status": {
            "cwd": status_cwd,
            "cmd": status_cmd,
            "timeoutSeconds": status_timeout,
        },
        "logs": logs,
        "actions": actions,
        "ui": {"tabs": tabs},
        "actionOutput": {
            "maxLines": int(action_output_obj.get("maxLines", default_action_output_max_lines)),
            "maxBytes": int(action_output_obj.get("maxBytes", default_action_output_max_bytes)),
        },
        "sourcePath": str(source_path),
    }


def _normalize_v2_include(
    payload: dict[str, Any],
    source_path: Path,
    *,
    default_refresh_seconds: float,
    default_timeout_seconds: float,
    default_action_output_max_lines: int,
    default_action_output_max_bytes: int,
) -> list[dict[str, Any]]:
    candidates = as_target_list(payload)
    if candidates:
        _assert_allowed_keys(
            payload,
            {"configVersion", "target", "targets"},
            f"v2 include container {source_path}",
        )
        for index, candidate in enumerate(candidates, 1):
            _validate_v2_target_payload(candidate, source_path, f"target[{index}]")
    else:
        _validate_v2_target_payload(payload, source_path, "target")
    if not candidates:
        candidates = [payload]

    result: list[dict[str, Any]] = []
    for target in candidates:
        result.append(
            _normalize_v2_target(
                target,
                source_path,
                default_refresh_seconds=default_refresh_seconds,
                default_timeout_seconds=default_timeout_seconds,
                default_action_output_max_lines=default_action_output_max_lines,
                default_action_output_max_bytes=default_action_output_max_bytes,
            )
        )
    return result


def load_monitor_config(path: Path) -> dict[str, Any]:
    base = load_json(path)
    _validate_root_config_payload(base, path)
    include_files = _require_string_list(base.get("includeFiles"), f"{path} includeFiles")

    default_refresh_seconds = float(base.get("refreshSeconds", DEFAULT_REFRESH_SECONDS))
    default_timeout_seconds = float(base.get("commandTimeoutSeconds", DEFAULT_COMMAND_TIMEOUT_SECONDS))

    root_action_output = base.get("actionOutput")
    root_action_output_obj = root_action_output if isinstance(root_action_output, dict) else {}
    default_action_output_max_lines = int(root_action_output_obj.get("maxLines", DEFAULT_ACTION_OUTPUT_MAX_LINES))
    default_action_output_max_bytes = int(root_action_output_obj.get("maxBytes", DEFAULT_ACTION_OUTPUT_MAX_BYTES))

    normalized_targets: list[dict[str, Any]] = []

    for include in include_files:
        include_path = resolve_path(path, include)
        payload = load_json(include_path)
        include_version = payload.get("configVersion")
        if include_version is None:
            include_version = 1
        include_version_int = int(include_version)

        if include_version_int == 1:
            normalized_targets.extend(
                _normalize_v1_include(
                    payload,
                    include_path,
                    default_refresh_seconds=default_refresh_seconds,
                    default_timeout_seconds=default_timeout_seconds,
                    default_action_output_max_lines=default_action_output_max_lines,
                    default_action_output_max_bytes=default_action_output_max_bytes,
                )
            )
            continue

        if include_version_int == 2:
            normalized_targets.extend(
                _normalize_v2_include(
                    payload,
                    include_path,
                    default_refresh_seconds=default_refresh_seconds,
                    default_timeout_seconds=default_timeout_seconds,
                    default_action_output_max_lines=default_action_output_max_lines,
                    default_action_output_max_bytes=default_action_output_max_bytes,
                )
            )
            continue

        raise ValueError(f"Unsupported configVersion={include_version_int} in {include_path}.")

    return {
        "refreshSeconds": default_refresh_seconds,
        "commandTimeoutSeconds": default_timeout_seconds,
        "targets": normalized_targets,
        "actionOutput": {
            "maxLines": default_action_output_max_lines,
            "maxBytes": default_action_output_max_bytes,
        },
        "includeFiles": include_files,
    }


class ActionOutputBuffer:
    def __init__(self, *, max_lines: int, max_bytes: int) -> None:
        self.max_lines = max(1, int(max_lines))
        self.max_bytes = max(1024, int(max_bytes))
        self._lines: deque[tuple[int, str]] = deque()
        self._total_bytes = 0
        self._lock = threading.Lock()

    def append(self, stream: str, text: str) -> tuple[str, str]:
        line = f"[{stream}] {text}".rstrip("\r\n")
        size = len(line.encode("utf-8", errors="ignore")) + 1
        with self._lock:
            self._lines.append((size, line))
            self._total_bytes += size
            while self._lines and (len(self._lines) > self.max_lines or self._total_bytes > self.max_bytes):
                removed_size, _ = self._lines.popleft()
                self._total_bytes -= removed_size
            return "\n".join(item for _, item in self._lines), line

    def snapshot(self) -> str:
        with self._lock:
            return "\n".join(item for _, item in self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._total_bytes = 0


class LogTailWorker(threading.Thread):
    def __init__(
        self,
        app: "MonitorApp",
        target_id: str,
        log_config: dict[str, Any],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.app = app
        self.target_id = target_id
        self.log_config = log_config
        self.stop_event = stop_event
        self.stream = str(log_config.get("stream") or "")
        self.glob_expr = str(log_config.get("glob") or "")
        self.tail_lines_count = max(1, int(log_config.get("tailLines", 300)))
        self.max_line_bytes = max(64, int(log_config.get("maxLineBytes", 4096)))
        self.poll_seconds = max(0.1, int(log_config.get("pollMs", 500)) / 1000.0)
        self.encoding = str(log_config.get("encoding") or "utf-8")
        self.allow_missing = bool(log_config.get("allowMissing", True))
        self._buffer: deque[str] = deque(maxlen=self.tail_lines_count)
        self._active_file: Path | None = None
        self._offset = 0
        self._remainder = ""
        self._last_render_key: tuple[str, str] | None = None

    def run(self) -> None:
        while not self.stop_event.wait(self.poll_seconds):
            try:
                self._tick()
            except Exception as ex:
                self._publish(f"(log worker error) {ex}", None)

    def _tick(self) -> None:
        latest = resolve_latest_file(self.glob_expr)
        if latest is None:
            self._active_file = None
            self._offset = 0
            self._remainder = ""
            if not self.allow_missing:
                self._publish(f"(missing) {self.glob_expr}", None)
            else:
                self._publish("", None)
            return

        if self._active_file is None or str(latest) != str(self._active_file):
            self._active_file = latest
            self._offset = 0
            self._remainder = ""
            self._buffer.clear()
            seeded = tail_lines(latest, self.tail_lines_count, encoding=self.encoding)
            if seeded:
                for line in seeded.splitlines():
                    self._append_line(line)
            try:
                self._offset = latest.stat().st_size
            except OSError:
                self._offset = 0
            self._publish("\n".join(self._buffer), latest)
            return

        try:
            size = self._active_file.stat().st_size
        except OSError:
            self._publish("\n".join(self._buffer), self._active_file)
            return

        if size < self._offset:
            self._offset = 0
            self._remainder = ""

        if size > self._offset:
            try:
                with self._active_file.open("rb") as handle:
                    handle.seek(self._offset)
                    chunk = handle.read(size - self._offset)
                self._offset = size
                text = self._remainder + chunk.decode(self.encoding, errors="ignore")
                lines = text.split("\n")
                self._remainder = lines.pop() if lines else ""
                for line in lines:
                    self._append_line(line.rstrip("\r"))
            except OSError:
                pass

        self._publish("\n".join(self._buffer), self._active_file)

    def _append_line(self, line: str) -> None:
        encoded = line.encode("utf-8", errors="ignore")
        if len(encoded) > self.max_line_bytes:
            encoded = encoded[: self.max_line_bytes]
            line = encoded.decode("utf-8", errors="ignore") + "...[truncated]"
        self._buffer.append(line)

    def _publish(self, content: str, active_file: Path | None) -> None:
        header_path = str(active_file) if active_file else self.glob_expr
        render = f"(stream={self.stream} file={header_path})"
        if content:
            render = render + "\n" + content
        render_key = (header_path, render)
        if self._last_render_key == render_key:
            return
        self._last_render_key = render_key
        self.app.root.after(
            0,
            lambda tid=self.target_id, stream=self.stream, text=render, active=header_path: self.app._apply_log_render(
                tid, stream, text, active
            ),
        )


class MonitorApp:
    def __init__(self, root: tk.Tk, config_path: Path) -> None:
        self.root = root
        self.config_path = config_path
        self.config = load_monitor_config(config_path)
        self.default_refresh_seconds = float(self.config.get("refreshSeconds", DEFAULT_REFRESH_SECONDS))
        self.default_command_timeout_seconds = float(
            self.config.get("commandTimeoutSeconds", DEFAULT_COMMAND_TIMEOUT_SECONDS)
        )
        self.targets: list[dict[str, Any]] = list(self.config.get("targets") or [])
        self.target_runtime: dict[str, dict[str, Any]] = {}
        self.console_var = tk.StringVar(value="ready")
        self.refresh_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.log_workers: list[LogTailWorker] = []
        self.action_mutexes: dict[str, threading.Lock] = {}

        self._build_ui()
        self._start_log_workers()
        self._schedule_refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.root.title("Fixture / Bridge Monitor")
        self.root.geometry("1440x900")

        top = ttk.Frame(self.root)
        top.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        target_notebook = ttk.Notebook(top)
        target_notebook.pack(fill=tk.BOTH, expand=True)

        for target in self.targets:
            tid = str(target.get("id") or "")
            title = str(target.get("title") or tid)
            frame = ttk.Frame(target_notebook)
            target_notebook.add(frame, text=title)

            banner_var = tk.StringVar(value="")
            banner = ttk.Label(frame, textvariable=banner_var, foreground="#b00020")
            banner.pack(fill=tk.X, padx=8, pady=(6, 0))

            tabs = ttk.Notebook(frame)
            tabs.pack(fill=tk.BOTH, expand=True, padx=4, pady=6)

            runtime = {
                "target": target,
                "bannerVar": banner_var,
                "bindings": [],
                "profileSelectors": [],
                "fileViewers": [],
                "logWidgetsByStream": {},
                "actionOutputWidget": None,
                "actionOutputPath": None,
                "lastGoodStatus": {},
                "lastStatusError": None,
                "nextRefreshAt": 0.0,
                "tabsWidget": tabs,
                "actionOutputTab": None,
            }
            self.target_runtime[tid] = runtime

            ui = target.get("ui") if isinstance(target.get("ui"), dict) else {}
            ui_tabs = ui.get("tabs") if isinstance(ui.get("tabs"), list) else []
            self._build_tabs(tabs, runtime, ui_tabs)

            action_output_tab = ttk.Frame(tabs)
            tabs.add(action_output_tab, text="Action Output")
            runtime["actionOutputTab"] = action_output_tab
            action_output_root = self.config_path.parent / "action-output"
            action_output_root.mkdir(parents=True, exist_ok=True)
            action_output_path = (action_output_root / f"{tid}.log").resolve()
            runtime["actionOutputPath"] = action_output_path

            toolbar = ttk.Frame(action_output_tab)
            toolbar.pack(fill=tk.X, padx=6, pady=(6, 2))
            action_output_path_var = tk.StringVar(value=str(action_output_path))
            ttk.Label(toolbar, text="Source:").pack(side=tk.LEFT)
            ttk.Label(toolbar, textvariable=action_output_path_var).pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)
            ttk.Button(toolbar, text="Open", command=lambda var=action_output_path_var: self._open_file_path(var.get())).pack(
                side=tk.RIGHT, padx=(6, 0)
            )
            ttk.Button(toolbar, text="Copy", command=lambda var=action_output_path_var: self._copy_to_clipboard(var.get())).pack(
                side=tk.RIGHT
            )
            ttk.Button(toolbar, text="Clear", command=lambda target_id=tid: self._clear_action_output(target_id)).pack(
                side=tk.RIGHT, padx=(0, 6)
            )

            action_text = tk.Text(action_output_tab, wrap=tk.NONE, height=10)
            action_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 6))
            runtime["actionOutputWidget"] = action_text
            action_output_cfg = target.get("actionOutput") if isinstance(target.get("actionOutput"), dict) else {}
            runtime["actionOutputBuffer"] = ActionOutputBuffer(
                max_lines=int(action_output_cfg.get("maxLines", DEFAULT_ACTION_OUTPUT_MAX_LINES)),
                max_bytes=int(action_output_cfg.get("maxBytes", DEFAULT_ACTION_OUTPUT_MAX_BYTES)),
            )

        footer = ttk.Frame(self.root)
        footer.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Label(footer, text="Console:").pack(side=tk.LEFT)
        ttk.Label(footer, textvariable=self.console_var).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(footer, text="Refresh Now", command=self._refresh_async).pack(side=tk.RIGHT)

    def _build_widgets(self, parent: ttk.Frame, runtime: dict[str, Any], widgets: list[dict[str, Any]]) -> None:
        widget_items = [item for item in widgets if isinstance(item, dict)]
        if not widget_items:
            return

        if len(widget_items) == 1:
            for widget in widget_items:
                self._build_one_widget(parent, runtime, widget)
            return

        splitter_widget_types = {"log", "action_map", "file_view"}
        uses_splitter = any(str(item.get("type") or "").strip().lower() in splitter_widget_types for item in widget_items)
        if uses_splitter:
            pane = ttk.Panedwindow(parent, orient=tk.VERTICAL)
            pane.pack(fill=tk.BOTH, expand=True)
            for widget in widget_items:
                slot = ttk.Frame(pane)
                pane.add(slot, weight=1)
                self._build_one_widget(slot, runtime, widget)
            return

        index = 0
        while index < len(widget_items):
            current = widget_items[index]
            current_type = str(current.get("type") or "").strip().lower()
            if current_type == "profile_select" and index + 1 < len(widget_items):
                next_widget = widget_items[index + 1]
                next_type = str(next_widget.get("type") or "").strip().lower()
                if next_type == "profile_select":
                    row = ttk.Frame(parent)
                    row.pack(fill=tk.X)
                    left = ttk.Frame(row)
                    left.pack(side=tk.LEFT, fill=tk.X, expand=True)
                    right = ttk.Frame(row)
                    right.pack(side=tk.LEFT, fill=tk.X, expand=True)
                    self._build_one_widget(left, runtime, current)
                    self._build_one_widget(right, runtime, next_widget)
                    index += 2
                    continue
            self._build_one_widget(parent, runtime, current)
            index += 1

    def _build_tabs(self, tabs_widget: ttk.Notebook, runtime: dict[str, Any], tabs: list[dict[str, Any]]) -> None:
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            self._build_single_tab(tabs_widget, runtime, tab)

    def _build_single_tab(self, tabs_widget: ttk.Notebook, runtime: dict[str, Any], tab: dict[str, Any]) -> None:
        tab_frame = ttk.Frame(tabs_widget)
        tabs_widget.add(tab_frame, text=str(tab.get("title") or tab.get("id") or "Tab"))
        widgets = tab.get("widgets") if isinstance(tab.get("widgets"), list) else []
        children = tab.get("children") if isinstance(tab.get("children"), list) else []

        if widgets and children:
            split = ttk.Panedwindow(tab_frame, orient=tk.VERTICAL)
            split.pack(fill=tk.BOTH, expand=True, padx=4, pady=6)
            widgets_slot = ttk.Frame(split)
            split.add(widgets_slot, weight=1)
            self._build_widgets(widgets_slot, runtime, widgets)

            child_slot = ttk.Frame(split)
            split.add(child_slot, weight=1)
            child_tabs = ttk.Notebook(child_slot)
            child_tabs.pack(fill=tk.BOTH, expand=True)
            self._build_tabs(child_tabs, runtime, children)
            return

        if widgets:
            self._build_widgets(tab_frame, runtime, widgets)
            return

        if children:
            child_tabs = ttk.Notebook(tab_frame)
            child_tabs.pack(fill=tk.BOTH, expand=True, padx=4, pady=6)
            self._build_tabs(child_tabs, runtime, children)
            return

        ttk.Label(tab_frame, text="No widgets configured.").pack(fill=tk.X, padx=8, pady=8)

    def _build_one_widget(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        widget_type = str(widget.get("type") or "").strip().lower()
        if widget_type == "kv":
            self._build_widget_kv(parent, runtime, widget)
            return
        if widget_type == "table":
            self._build_widget_table(parent, runtime, widget)
            return
        if widget_type == "log":
            self._build_widget_log(parent, runtime, widget)
            return
        if widget_type == "button":
            self._build_widget_button(parent, runtime, widget)
            return
        if widget_type == "profile_select":
            self._build_widget_profile_select(parent, runtime, widget)
            return
        if widget_type == "action_map":
            self._build_widget_action_map(parent, runtime, widget)
            return
        if widget_type == "action_select":
            self._build_widget_action_select(parent, runtime, widget)
            return
        if widget_type == "file_view":
            self._build_widget_file_view(parent, runtime, widget)
            return

        unknown = ttk.Label(parent, text=f"Unsupported widget type: {widget_type or '(blank)'}")
        unknown.pack(fill=tk.X, padx=8, pady=4)

    def _build_widget_kv(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Values"))
        frame.pack(fill=tk.X, padx=8, pady=6, anchor="n")
        items = widget.get("items")
        if not isinstance(items, list):
            return
        for row, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("jsonpath") or "")
            path = str(item.get("jsonpath") or "")
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            value_var = tk.StringVar(value="-")
            ttk.Label(frame, textvariable=value_var).grid(row=row, column=1, sticky="w", padx=8, pady=4)
            runtime["bindings"].append((path, value_var))

    def _build_widget_table(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Table"))
        frame.pack(fill=tk.X, padx=8, pady=6, anchor="n")
        columns = widget.get("columns")
        if not isinstance(columns, list):
            return
        for col, item in enumerate(columns):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("jsonpath") or "")
            path = str(item.get("jsonpath") or "")
            ttk.Label(frame, text=label).grid(row=0, column=col, sticky="w", padx=6, pady=(6, 2))
            value_var = tk.StringVar(value="-")
            ttk.Label(frame, textvariable=value_var).grid(row=1, column=col, sticky="w", padx=6, pady=(2, 6))
            runtime["bindings"].append((path, value_var))

    def _build_widget_log(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or widget.get("stream") or "Log"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        show_path = bool(widget.get("showPath", True))
        open_path_button = bool(widget.get("openPathButton", True))
        copy_path_button = bool(widget.get("copyPathButton", True))
        path_var = tk.StringVar(value="-")
        if show_path:
            toolbar = ttk.Frame(frame)
            toolbar.pack(fill=tk.X, padx=4, pady=(4, 2))
            ttk.Label(toolbar, text="File:").pack(side=tk.LEFT, padx=(0, 6))
            ttk.Label(toolbar, textvariable=path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
            if open_path_button:
                ttk.Button(toolbar, text="Open", command=lambda var=path_var: self._open_file_path(var.get())).pack(
                    side=tk.RIGHT, padx=(6, 0)
                )
            if copy_path_button:
                ttk.Button(toolbar, text="Copy", command=lambda var=path_var: self._copy_to_clipboard(var.get())).pack(
                    side=tk.RIGHT
                )
        text = tk.Text(frame, wrap=tk.NONE, height=14)
        text.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 4))
        stream = str(widget.get("stream") or "").strip()
        if stream:
            runtime["logWidgetsByStream"].setdefault(stream, []).append({"text": text, "pathVar": path_var})

    def _build_widget_button(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, padx=8, pady=4, anchor="w")
        label = str(widget.get("label") or widget.get("action") or "Run")
        action_name = str(widget.get("action") or "").strip()
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "")
        button = ttk.Button(frame, text=label, command=lambda: self._invoke_action(target_id, action_name))
        button.pack(side=tk.LEFT)

    def _build_widget_profile_select(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Profile"))
        frame.pack(fill=tk.X, padx=8, pady=6, anchor="n")
        options_path = str(widget.get("optionsJsonpath") or "")
        current_path = str(widget.get("currentJsonpath") or "")
        action_name = str(widget.get("action") or "").strip()
        empty_label = str(widget.get("emptyLabel") or "Select profile")
        apply_label = str(widget.get("applyLabel") or "Apply")

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, padx=8, pady=6)
        current_var = tk.StringVar(value="-")
        ttk.Label(row, text="Current:").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(row, textvariable=current_var).pack(side=tk.LEFT, padx=(0, 12))

        selected_var = tk.StringVar(value="")
        combo = ttk.Combobox(row, textvariable=selected_var, state="readonly", width=24)
        combo.pack(side=tk.LEFT, padx=(0, 8))
        combo["values"] = [empty_label]
        combo.set(empty_label)

        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "")

        def apply_selected() -> None:
            selected = str(selected_var.get() or "").strip()
            if not selected or selected == empty_label:
                self.console_var.set("No profile selected.")
                return
            self._invoke_action(target_id, action_name, selected)

        ttk.Button(row, text=apply_label, command=apply_selected).pack(side=tk.LEFT)
        runtime["profileSelectors"].append(
            {
                "optionsPath": options_path,
                "currentPath": current_path,
                "emptyLabel": empty_label,
                "selectedVar": selected_var,
                "currentVar": current_var,
                "combo": combo,
            }
        )

    def _build_widget_action_map(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Commands"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        include_commands = bool(widget.get("includeCommands", True))
        show_action_name = bool(widget.get("showActionName", True))
        include_prefix = str(widget.get("includePrefix") or "").strip()
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        actions = target.get("actions") if isinstance(target.get("actions"), list) else []

        text = tk.Text(frame, wrap=tk.NONE, height=12)
        text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        lines: list[str] = []
        for item in actions:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if include_prefix and not name.startswith(include_prefix):
                continue
            label = str(item.get("label") or name).strip()
            cmd = _normalize_cmd(item.get("cmd"))
            head = label
            if show_action_name and name:
                head = f"{label} ({name})"
            lines.append(head)
            if include_commands and cmd:
                lines.append("  " + " ".join(cmd))
            lines.append("")
        render = "\n".join(lines).strip() or "(no actions configured)"
        text.insert(tk.END, render + "\n")
        text.configure(state=tk.DISABLED)

    def _build_widget_action_select(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Select Action"))
        frame.pack(fill=tk.X, padx=8, pady=6, anchor="n")
        include_prefix = str(widget.get("includePrefix") or "").strip()
        include_regex = str(widget.get("includeRegex") or "").strip()
        empty_label = str(widget.get("emptyLabel") or "Select action")
        run_label = str(widget.get("runLabel") or "Run")
        show_command = bool(widget.get("showCommand", True))

        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "")
        actions = target.get("actions") if isinstance(target.get("actions"), list) else []
        matcher = re.compile(include_regex) if include_regex else None
        eligible: list[dict[str, Any]] = []
        for item in actions:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if include_prefix and not name.startswith(include_prefix):
                continue
            if matcher and matcher.search(name) is None:
                continue
            eligible.append(item)

        label_to_name: dict[str, str] = {}
        options: list[str] = []
        for item in eligible:
            name = str(item.get("name") or "").strip()
            label = str(item.get("label") or name).strip()
            display = f"{label} ({name})" if name else label
            options.append(display)
            label_to_name[display] = name

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, padx=8, pady=6)
        selected_var = tk.StringVar(value=empty_label)
        combo = ttk.Combobox(row, textvariable=selected_var, state="readonly", width=50)
        combo["values"] = options if options else [empty_label]
        combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        combo.set(empty_label if not options else options[0])
        if options:
            selected_var.set(options[0])

        def run_selected() -> None:
            selected = str(selected_var.get() or "").strip()
            action_name = label_to_name.get(selected, "")
            if not action_name:
                self.console_var.set("No action selected.")
                return
            self._invoke_action(target_id, action_name)

        ttk.Button(row, text=run_label, command=run_selected).pack(side=tk.LEFT, padx=(8, 0))
        if show_command:
            command_var = tk.StringVar(value="")
            ttk.Label(frame, textvariable=command_var).pack(fill=tk.X, padx=8, pady=(0, 6))

            def update_preview(*_: Any) -> None:
                selected = str(selected_var.get() or "").strip()
                action_name = label_to_name.get(selected, "")
                action = next((item for item in eligible if str(item.get("name") or "") == action_name), None)
                if not isinstance(action, dict):
                    command_var.set("-")
                    return
                cmd = _normalize_cmd(action.get("cmd"))
                command_var.set(" ".join(cmd) if cmd else "-")

            selected_var.trace_add("write", update_preview)
            update_preview()

    def _build_widget_file_view(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "File"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        path_var = tk.StringVar(value="-")
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Label(toolbar, text="Path:").pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=path_var).pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)
        ttk.Button(toolbar, text="Open", command=lambda var=path_var: self._open_file_path(var.get())).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(toolbar, text="Copy", command=lambda var=path_var: self._copy_to_clipboard(var.get())).pack(
            side=tk.RIGHT
        )
        text = tk.Text(frame, wrap=tk.NONE, height=16)
        text.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 4))
        text.configure(state=tk.DISABLED)
        runtime["fileViewers"].append(
            {
                "pathJsonpath": str(widget.get("pathJsonpath") or ""),
                "pathLiteral": str(widget.get("pathLiteral") or ""),
                "pathVar": path_var,
                "textWidget": text,
                "maxBytes": int(widget.get("maxBytes", 512000)),
                "encoding": str(widget.get("encoding") or "utf-8"),
                "lastSignature": None,
            }
        )

    def _open_file_path(self, path_text: str) -> None:
        candidate = str(path_text or "").strip()
        if not candidate or candidate == "-":
            self.console_var.set("No file path available.")
            return
        path = Path(candidate)
        if not path.exists():
            self.console_var.set(f"path not found: {path}")
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
            self.console_var.set(f"opened: {path}")
        except Exception as ex:
            self.console_var.set(f"open failed: {ex}")

    def _copy_to_clipboard(self, value: str) -> None:
        text = str(value or "").strip()
        if not text:
            self.console_var.set("Nothing to copy.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.console_var.set("Copied to clipboard.")

    def _read_file_for_view(self, path: Path, *, max_bytes: int, encoding: str) -> str:
        if not path.exists() or not path.is_file():
            return "(missing file)"
        cap = max(1024, int(max_bytes))
        try:
            with path.open("rb") as handle:
                raw = handle.read(cap + 1)
        except Exception as ex:
            return f"(read error) {ex}"
        truncated = len(raw) > cap
        if truncated:
            raw = raw[:cap]
        text = raw.decode(encoding, errors="ignore")
        if truncated:
            text += "\n...[truncated]"
        return text

    def _refresh_file_viewers(self, runtime: dict[str, Any], payload: dict[str, Any]) -> None:
        viewers = runtime.get("fileViewers")
        if not isinstance(viewers, list):
            return
        for viewer in viewers:
            if not isinstance(viewer, dict):
                continue
            path_json = str(viewer.get("pathJsonpath") or "").strip()
            path_literal = str(viewer.get("pathLiteral") or "").strip()
            path_value = path_literal
            if path_json:
                resolved = json_path_get(payload, path_json)
                if resolved is not None:
                    path_value = str(resolved)
            path_var = viewer.get("pathVar")
            if isinstance(path_var, tk.StringVar):
                path_var.set(path_value or "-")
            widget = viewer.get("textWidget")
            if not isinstance(widget, tk.Text):
                continue
            path_obj = Path(path_value) if path_value else None
            signature = None
            if path_obj is not None and path_obj.exists() and path_obj.is_file():
                try:
                    stat = path_obj.stat()
                    signature = (str(path_obj), int(stat.st_mtime_ns), int(stat.st_size))
                except Exception:
                    signature = (str(path_obj), 0, 0)
            if signature == viewer.get("lastSignature"):
                continue
            viewer["lastSignature"] = signature
            if path_obj is None:
                content = "(no path)"
            else:
                content = self._read_file_for_view(
                    path_obj,
                    max_bytes=int(viewer.get("maxBytes", 512000)),
                    encoding=str(viewer.get("encoding") or "utf-8"),
                )
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, content + "\n")
            widget.configure(state=tk.DISABLED)

    def _start_log_workers(self) -> None:
        for target in self.targets:
            tid = str(target.get("id") or "")
            logs = target.get("logs")
            if not isinstance(logs, list):
                continue
            for log in logs:
                if not isinstance(log, dict):
                    continue
                stream = str(log.get("stream") or "").strip()
                if not stream:
                    continue
                worker = LogTailWorker(self, tid, log, self.stop_event)
                worker.start()
                self.log_workers.append(worker)

    def _schedule_refresh(self) -> None:
        self._refresh_async()
        delay_ms = int(max(MIN_REFRESH_TICK_SECONDS, 0.25) * 1000)
        self.root.after(delay_ms, self._schedule_refresh)

    def _refresh_async(self) -> None:
        if self.refresh_lock.locked():
            return
        thread = threading.Thread(target=self._refresh, daemon=True)
        thread.start()

    def _refresh(self) -> None:
        if not self.refresh_lock.acquire(blocking=False):
            return
        try:
            due_targets: list[dict[str, Any]] = []
            now = time.time()
            for target in self.targets:
                tid = str(target.get("id") or "")
                runtime = self.target_runtime.get(tid)
                if runtime is None:
                    continue
                if now >= float(runtime.get("nextRefreshAt") or 0.0):
                    due_targets.append(target)
                    refresh_interval = max(MIN_REFRESH_TICK_SECONDS, float(target.get("refreshSeconds") or 1.0))
                    runtime["nextRefreshAt"] = now + refresh_interval

            if not due_targets:
                return

            with ThreadPoolExecutor(max_workers=max(1, len(due_targets))) as executor:
                futures = [executor.submit(self._refresh_target, target) for target in due_targets]
                for future in futures:
                    future.result()

            self.root.after(0, lambda: self.console_var.set(time.strftime("%H:%M:%S") + " refreshed"))
        except Exception as ex:
            self.root.after(0, lambda: self.console_var.set(f"refresh error: {ex}"))
        finally:
            self.refresh_lock.release()

    def _refresh_target(self, target: dict[str, Any]) -> None:
        tid = str(target.get("id") or "")
        runtime = self.target_runtime.get(tid)
        if runtime is None:
            return

        status = target.get("status")
        if not isinstance(status, dict):
            self._set_status_error(tid, "status provider missing")
            self._render_target_status(tid)
            return

        cmd = _normalize_cmd(status.get("cmd"))
        cwd_text = str(status.get("cwd") or "").strip()
        cwd = Path(cwd_text) if cwd_text else None
        timeout_seconds = float(status.get("timeoutSeconds") or self.default_command_timeout_seconds)
        if not cmd:
            self._set_status_error(tid, "status.cmd is empty")
            self._render_target_status(tid)
            return

        payload: dict[str, Any] | None = None
        error_message = ""
        try:
            rc, stdout, stderr = run_cmd(cmd, cwd, timeout_seconds=timeout_seconds)
            if rc == 0:
                parsed, parse_error = try_extract_json_object(stdout)
                if parsed is not None:
                    payload = parsed
                else:
                    error_message = parse_error
            else:
                error_message = stderr.strip() or f"status command exited rc={rc}"
        except subprocess.TimeoutExpired:
            error_message = f"status timeout after {timeout_seconds:.1f}s"
        except Exception as ex:
            error_message = str(ex)

        if payload is not None:
            runtime["lastGoodStatus"] = payload
            runtime["lastStatusError"] = None
        else:
            self._set_status_error(tid, error_message or "status command failed")

        self._render_target_status(tid)

    def _set_status_error(self, target_id: str, message: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return
        runtime["lastStatusError"] = {"ts": utc_now_iso(), "message": message}

    def _render_target_status(self, target_id: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return

        status_payload = runtime.get("lastGoodStatus")
        payload = status_payload if isinstance(status_payload, dict) else {}
        bindings = list(runtime.get("bindings") or [])
        profile_selectors = list(runtime.get("profileSelectors") or [])
        error_obj = runtime.get("lastStatusError")

        def update() -> None:
            for path, var in bindings:
                value = json_path_get(payload, str(path))
                var.set(render_value(value))
            for selector in profile_selectors:
                if not isinstance(selector, dict):
                    continue
                options_path = str(selector.get("optionsPath") or "")
                current_path = str(selector.get("currentPath") or "")
                options_raw = json_path_get(payload, options_path)
                options = [str(item) for item in options_raw] if isinstance(options_raw, list) else []
                combo = selector.get("combo")
                empty_label = str(selector.get("emptyLabel") or "Select profile")
                if isinstance(combo, ttk.Combobox):
                    combo["values"] = options if options else [empty_label]
                current_value = json_path_get(payload, current_path)
                current_text = str(current_value) if current_value is not None else "-"
                current_var = selector.get("currentVar")
                if isinstance(current_var, tk.StringVar):
                    current_var.set(current_text)
                selected_var = selector.get("selectedVar")
                if isinstance(selected_var, tk.StringVar) and options:
                    selected = str(selected_var.get() or "").strip()
                    if (not selected or selected == empty_label) and current_text in options:
                        selected_var.set(current_text)
            banner_var = runtime.get("bannerVar")
            if isinstance(banner_var, tk.StringVar):
                if isinstance(error_obj, dict):
                    ts = str(error_obj.get("ts") or "")
                    msg = str(error_obj.get("message") or "")
                    banner_var.set(f"[{ts}] {msg}")
                else:
                    banner_var.set("")
            self._refresh_file_viewers(runtime, payload)

        self.root.after(0, update)

    def _apply_log_render(self, target_id: str, stream: str, content: str, active_path: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return
        widgets = runtime.get("logWidgetsByStream")
        if not isinstance(widgets, dict):
            return
        stream_widgets = widgets.get(stream)
        if not isinstance(stream_widgets, list):
            return
        for widget_entry in stream_widgets:
            widget = None
            path_var = None
            if isinstance(widget_entry, dict):
                widget = widget_entry.get("text")
                path_var = widget_entry.get("pathVar")
            elif isinstance(widget_entry, tk.Text):
                widget = widget_entry
            if not isinstance(widget, tk.Text):
                continue
            widget.delete("1.0", tk.END)
            if content:
                widget.insert(tk.END, content + "\n")
            else:
                widget.insert(tk.END, "(no data)\n")
            if isinstance(path_var, tk.StringVar):
                path_var.set(active_path or "-")

    def _invoke_action(self, target_id: str, action_name: str, action_value: str | None = None) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return
        target = runtime.get("target")
        if not isinstance(target, dict):
            return
        actions = target.get("actions")
        if not isinstance(actions, list):
            self.console_var.set("No actions configured.")
            return
        action = next((item for item in actions if isinstance(item, dict) and str(item.get("name")) == action_name), None)
        if action is None:
            self.console_var.set(f"Action not found: {action_name}")
            return

        confirm_text = str(action.get("confirm") or "").strip()
        if confirm_text and not messagebox.askyesno("Confirm Action", confirm_text):
            return

        tabs_widget = runtime.get("tabsWidget")
        action_output_tab = runtime.get("actionOutputTab")
        if isinstance(tabs_widget, ttk.Notebook) and isinstance(action_output_tab, ttk.Frame):
            try:
                tabs_widget.select(action_output_tab)
            except Exception:
                pass

        thread = threading.Thread(target=self._run_action, args=(target_id, action, action_value), daemon=True)
        thread.start()

    def _run_action(self, target_id: str, action: dict[str, Any], action_value: str | None = None) -> None:
        action_name = str(action.get("name") or "")
        action_label = str(action.get("label") or action_name)
        mutex_name = str(action.get("mutex") or "").strip()
        lock: threading.Lock | None = None
        if mutex_name:
            lock = self.action_mutexes.setdefault(mutex_name, threading.Lock())

        if lock is not None:
            lock.acquire()
        try:
            cmd = _normalize_cmd(action.get("cmd"))
            if action_value is not None:
                cmd = [part.replace("{value}", str(action_value)) for part in cmd]
            if not cmd:
                self._append_action_output(target_id, "system", f"{action_label}: empty command")
                return

            cwd_text = str(action.get("cwd") or "").strip()
            if action_value is not None and cwd_text:
                cwd_text = cwd_text.replace("{value}", str(action_value))
            cwd = Path(cwd_text) if cwd_text else None
            timeout_seconds = float(action.get("timeoutSeconds") or 120.0)
            detached = bool(action.get("detached", False))

            self._append_action_output(target_id, "system", f"running {action_label}: {' '.join(cmd)}")
            self.root.after(0, lambda: self.console_var.set(f"running action: {action_label}"))

            if detached:
                subprocess.Popen(cmd, cwd=str(cwd) if cwd else None)
                self._append_action_output(target_id, "system", f"{action_label}: detached process started")
                self.root.after(0, lambda: self.console_var.set(f"started(detached): {action_label}"))
                return

            process = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            stdout_thread = threading.Thread(
                target=self._drain_action_stream,
                args=(target_id, "stdout", process.stdout),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=self._drain_action_stream,
                args=(target_id, "stderr", process.stderr),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            timed_out = False
            try:
                rc = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                rc = process.wait()

            stdout_thread.join(timeout=2.0)
            stderr_thread.join(timeout=2.0)

            if timed_out:
                self._append_action_output(
                    target_id,
                    "system",
                    f"{action_label}: timeout after {timeout_seconds:.1f}s (rc={rc})",
                )
                self.root.after(0, lambda: self.console_var.set(f"action timeout: {action_label}"))
            else:
                self._append_action_output(target_id, "system", f"{action_label}: finished rc={rc}")
                self.root.after(0, lambda: self.console_var.set(f"action done: {action_label} rc={rc}"))
        except Exception as ex:
            self._append_action_output(target_id, "system", f"{action_label}: failed: {ex}")
            self.root.after(0, lambda: self.console_var.set(f"action failed: {action_label}"))
        finally:
            if lock is not None:
                lock.release()

    def _drain_action_stream(self, target_id: str, stream_name: str, handle: Any) -> None:
        if handle is None:
            return
        try:
            for line in iter(handle.readline, ""):
                if not line:
                    break
                self._append_action_output(target_id, stream_name, line.rstrip("\r\n"))
        finally:
            try:
                handle.close()
            except Exception:
                pass

    def _append_action_output(self, target_id: str, stream: str, text: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return
        buffer = runtime.get("actionOutputBuffer")
        if not isinstance(buffer, ActionOutputBuffer):
            return
        snapshot, line = buffer.append(stream, text)
        output_path = runtime.get("actionOutputPath")
        if isinstance(output_path, Path):
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with output_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except Exception:
                pass
        widget = runtime.get("actionOutputWidget")
        if not isinstance(widget, tk.Text):
            return

        def update() -> None:
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, snapshot + "\n")
            widget.see(tk.END)

        self.root.after(0, update)

    def _clear_action_output(self, target_id: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return
        buffer = runtime.get("actionOutputBuffer")
        if isinstance(buffer, ActionOutputBuffer):
            buffer.clear()
        output_path = runtime.get("actionOutputPath")
        if isinstance(output_path, Path):
            try:
                output_path.write_text("", encoding="utf-8")
            except Exception:
                pass
        widget = runtime.get("actionOutputWidget")
        if isinstance(widget, tk.Text):
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, "(cleared)\n")
        self.console_var.set("Action output cleared.")

    def _on_close(self) -> None:
        self.stop_event.set()
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="JSON-driven monitor GUI.")
    parser.add_argument("--config", default="monitor_config.json")
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Parse and validate configuration, print summary, then exit.",
    )
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}")

    config = load_monitor_config(config_path)
    if args.validate_config:
        print(f"config={config_path}")
        print(f"targets={len(config.get('targets') or [])}")
        for target in config.get("targets") or []:
            tid = str(target.get("id") or "")
            version = int(target.get("configVersion") or 1)
            logs_count = len(target.get("logs") or [])
            actions_count = len(target.get("actions") or [])
            print(f"- {tid} v{version} logs={logs_count} actions={actions_count}")
        return 0

    root = tk.Tk()
    MonitorApp(root, config_path=config_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
