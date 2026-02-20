#!/usr/bin/env python3
"""Generic JSON-driven monitor for fixture/bridge status and logs."""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from monitor_config_payload import (
    _normalize_config_entries_payload,
    _normalize_config_paths_payload,
    _normalize_config_show_payload,
)
from monitor_ipc import (
    _iter_jsonpath_tokens,
    _parse_endpoint,
    _request_ipc_v0,
    json_path_get,
    render_value,
    try_extract_json_object,
)


DEFAULT_REFRESH_SECONDS = 1.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 10.0
DEFAULT_ACTION_OUTPUT_MAX_LINES = 1200
DEFAULT_ACTION_OUTPUT_MAX_BYTES = 1_000_000
MIN_REFRESH_TICK_SECONDS = 0.2
DEFAULT_CONTROL_TIMEOUT_SECONDS = 8.0
DEFAULT_CONTROL_JOB_POLL_MS = 200
DEFAULT_CONTROL_JOB_TIMEOUT_SECONDS = 120.0
def _no_window_creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


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


def _order_top_level_tabs(tabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Top-level order is defined by ui.tabs[] sequence in the target config.
    return [tab for tab in tabs if isinstance(tab, dict)]


def _normalize_control_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    mode = str(value.get("mode") or "").strip().lower()
    if mode != "ipc":
        return {}

    endpoint = str(value.get("endpoint") or "").strip()
    app_id = str(value.get("appId") or "").strip()
    if not endpoint or not app_id:
        return {}

    timeout_seconds = float(value.get("timeoutSeconds", DEFAULT_CONTROL_TIMEOUT_SECONDS))
    job_poll_ms = int(value.get("jobPollMs", DEFAULT_CONTROL_JOB_POLL_MS))
    job_timeout_seconds = float(value.get("jobTimeoutSeconds", DEFAULT_CONTROL_JOB_TIMEOUT_SECONDS))
    return {
        "mode": "ipc",
        "endpoint": endpoint,
        "appId": app_id,
        "timeoutSeconds": max(0.1, timeout_seconds),
        "jobPollMs": max(50, job_poll_ms),
        "jobTimeoutSeconds": max(1.0, job_timeout_seconds),
    }


def _target_control(target: dict[str, Any]) -> dict[str, Any]:
    return _normalize_control_payload(target.get("control"))


def _is_ipc_control(target: dict[str, Any]) -> bool:
    control = _target_control(target)
    return str(control.get("mode") or "") == "ipc"


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

    if widget_type == "rows_table":
        _assert_allowed_keys(widget, {"type", "title", "rowsJsonpath", "columns", "emptyText", "maxRows"}, context)
        rows_path = str(widget.get("rowsJsonpath") or "").strip()
        if not rows_path:
            raise ValueError(f"{context}.rowsJsonpath must be a non-empty string.")
        columns = widget.get("columns")
        if not isinstance(columns, list):
            raise ValueError(f"{context}.columns must be a list.")
        for idx, item in enumerate(columns, 1):
            if not isinstance(item, dict):
                raise ValueError(f"{context}.columns[{idx}] must be an object.")
            _assert_allowed_keys(item, {"label", "key", "jsonpath"}, f"{context}.columns[{idx}]")
            label = str(item.get("label") or "").strip()
            key = str(item.get("key") or "").strip()
            jsonpath = str(item.get("jsonpath") or "").strip()
            if not label:
                raise ValueError(f"{context}.columns[{idx}].label must be a non-empty string.")
            if not key and not jsonpath:
                raise ValueError(f"{context}.columns[{idx}] requires key or jsonpath.")
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
        _assert_allowed_keys(
            widget,
            {"type", "title", "includeCommands", "showActionName", "includePrefix", "includeRegex"},
            context,
        )
        return

    if widget_type == "action_select":
        _assert_allowed_keys(
            widget,
            {"type", "title", "includePrefix", "includeRegex", "emptyLabel", "runLabel", "showCommand"},
            context,
        )
        return

    if widget_type == "action_output":
        _assert_allowed_keys(widget, {"type", "title"}, context)
        return

    if widget_type == "text_block":
        _assert_allowed_keys(widget, {"type", "title", "text", "height"}, context)
        return

    if widget_type == "file_view":
        _assert_allowed_keys(
            widget,
            {"type", "title", "pathJsonpath", "pathLiteral", "maxBytes", "encoding", "showContent"},
            context,
        )
        return

    if widget_type == "config_editor":
        _assert_allowed_keys(
            widget,
            {
                "type",
                "title",
                "showAction",
                "setAction",
                "pathJsonpath",
                "pathLiteral",
                "pathKey",
                "includePrefix",
                "includeKeys",
                "excludeKeys",
                "settableOnly",
                "reloadLabel",
            },
            context,
        )
        show_action = str(widget.get("showAction") or "").strip()
        set_action = str(widget.get("setAction") or "").strip()
        if not show_action:
            raise ValueError(f"{context}.showAction must be a non-empty string.")
        if not set_action:
            raise ValueError(f"{context}.setAction must be a non-empty string.")
        for list_key in ("includeKeys", "excludeKeys"):
            raw_list = widget.get(list_key)
            if raw_list is None:
                continue
            if not isinstance(raw_list, list):
                raise ValueError(f"{context}.{list_key} must be a list.")
            for item_index, item in enumerate(raw_list, 1):
                if not str(item).strip():
                    raise ValueError(f"{context}.{list_key}[{item_index}] must be a non-empty string.")
        return

    if widget_type == "config_file_select":
        _assert_allowed_keys(
            widget,
            {
                "type",
                "title",
                "showAction",
                "setAction",
                "key",
                "pathKey",
                "emptyLabel",
                "applyLabel",
                "reloadLabel",
            },
            context,
        )
        show_action = str(widget.get("showAction") or "").strip()
        set_action = str(widget.get("setAction") or "").strip()
        key = str(widget.get("key") or "").strip()
        path_key = str(widget.get("pathKey") or "").strip()
        if not show_action:
            raise ValueError(f"{context}.showAction must be a non-empty string.")
        if not set_action:
            raise ValueError(f"{context}.setAction must be a non-empty string.")
        if not key:
            raise ValueError(f"{context}.key must be a non-empty string.")
        if not path_key:
            raise ValueError(f"{context}.pathKey must be a non-empty string.")
        return

    raise ValueError(f"{context} has unsupported widget type '{widget_type or '(blank)'}'.")


def _validate_action_arg(arg: dict[str, Any], context: str) -> None:
    _assert_allowed_keys(
        arg,
        {
            "name",
            "label",
            "required",
            "type",
            "placeholder",
            "pattern",
            "optionsJsonpath",
            "options",
        },
        context,
    )
    name = str(arg.get("name") or "").strip()
    if not name:
        raise ValueError(f"{context}.name must be a non-empty string.")
    arg_type = str(arg.get("type") or "string").strip().lower()
    if arg_type not in {"string", "int", "float", "bool"}:
        raise ValueError(f"{context}.type must be one of string|int|float|bool.")
    options_jsonpath = str(arg.get("optionsJsonpath") or "").strip()
    if options_jsonpath and not options_jsonpath.startswith("$"):
        raise ValueError(f"{context}.optionsJsonpath must be a JSONPath starting with '$'.")
    options_raw = arg.get("options")
    if options_raw is not None:
        if not isinstance(options_raw, list):
            raise ValueError(f"{context}.options must be a list when provided.")
        for idx, item in enumerate(options_raw, 1):
            if not str(item).strip():
                raise ValueError(f"{context}.options[{idx}] must be a non-empty value.")


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


def _iter_v2_widgets(tab: dict[str, Any], context: str) -> list[tuple[str, dict[str, Any]]]:
    results: list[tuple[str, dict[str, Any]]] = []
    widgets = tab.get("widgets")
    if isinstance(widgets, list):
        for widget_index, widget in enumerate(widgets, 1):
            if isinstance(widget, dict):
                results.append((f"{context}.widgets[{widget_index}]", widget))
    children = tab.get("children")
    if isinstance(children, list):
        for child_index, child in enumerate(children, 1):
            if not isinstance(child, dict):
                continue
            results.extend(_iter_v2_widgets(child, f"{context}.children[{child_index}]"))
    return results


def _validate_v2_control_payload(value: Any, source_path: Path, context: str) -> dict[str, Any]:
    if value is None:
        raise ValueError(f"{context} in {source_path} is missing required control object.")
    if not isinstance(value, dict):
        raise ValueError(f"{context} in {source_path} must be an object.")

    _assert_allowed_keys(
        value,
        {"mode", "endpoint", "appId", "timeoutSeconds", "jobPollMs", "jobTimeoutSeconds"},
        f"{context} in {source_path}",
    )
    mode = str(value.get("mode") or "").strip().lower()
    if mode != "ipc":
        raise ValueError(f"{context}.mode in {source_path} must be 'ipc'.")
    endpoint = str(value.get("endpoint") or "").strip()
    app_id = str(value.get("appId") or "").strip()
    if not endpoint:
        raise ValueError(f"{context}.endpoint in {source_path} must be a non-empty string when mode=ipc.")
    if not app_id:
        raise ValueError(f"{context}.appId in {source_path} must be a non-empty string when mode=ipc.")
    return _normalize_control_payload(value)


def _validate_v2_bootstrap_payload(value: Any, source_path: Path, context: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} in {source_path} must be an object when provided.")
    _assert_allowed_keys(value, {"configPath"}, f"{context} in {source_path}")
    config_path = str(value.get("configPath") or "").strip()
    if not config_path:
        raise ValueError(f"{context}.configPath in {source_path} must be a non-empty string when provided.")
    return {"configPath": config_path}


def _validate_v2_target_payload(target: dict[str, Any], source_path: Path, context: str) -> None:
    _assert_allowed_keys(
        target,
        {
            "configVersion",
            "id",
            "title",
            "refreshSeconds",
            "status",
            "logs",
            "actions",
            "ui",
            "actionOutput",
            "control",
            "bootstrap",
        },
        f"{context} in {source_path}",
    )
    control = _validate_v2_control_payload(target.get("control"), source_path, f"{context}.control")
    _validate_v2_bootstrap_payload(target.get("bootstrap"), source_path, f"{context}.bootstrap")
    ipc_mode = str(control.get("mode") or "") == "ipc"

    status = target.get("status")
    if status is not None:
        if not isinstance(status, dict):
            raise ValueError(f"{context}.status in {source_path} must be an object when provided.")
        _assert_allowed_keys(status, {"timeoutSeconds"}, f"{context}.status in {source_path}")

    log_streams: set[str] = set()
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
            stream_name = str(log.get("stream") or "").strip()
            if not stream_name:
                raise ValueError(f"{context}.logs[{idx}].stream in {source_path} must be a non-empty string.")
            if stream_name in log_streams:
                raise ValueError(
                    f"{context}.logs[{idx}].stream in {source_path} duplicates stream '{stream_name}'."
                )
            log_streams.add(stream_name)

    action_names: set[str] = set()
    actions = target.get("actions")
    if isinstance(actions, list):
        for idx, action in enumerate(actions, 1):
            if not isinstance(action, dict):
                raise ValueError(f"{context}.actions[{idx}] in {source_path} must be an object.")
            _assert_allowed_keys(
                action,
                {
                    "name",
                    "label",
                    "cwd",
                    "cmd",
                    "timeoutSeconds",
                    "confirm",
                    "showOutputPanel",
                    "mutex",
                    "detached",
                    "args",
                },
                f"{context}.actions[{idx}] in {source_path}",
            )
            action_name = str(action.get("name") or "").strip()
            if not action_name:
                raise ValueError(f"{context}.actions[{idx}].name in {source_path} must be a non-empty string.")
            if action_name in action_names:
                raise ValueError(
                    f"{context}.actions[{idx}].name in {source_path} duplicates action '{action_name}'."
                )
            action_names.add(action_name)
            args_raw = action.get("args")
            if args_raw is not None:
                if not isinstance(args_raw, list):
                    raise ValueError(f"{context}.actions[{idx}].args in {source_path} must be a list.")
                for arg_index, arg in enumerate(args_raw, 1):
                    if not isinstance(arg, dict):
                        raise ValueError(
                            f"{context}.actions[{idx}].args[{arg_index}] in {source_path} must be an object."
                        )
                    _validate_action_arg(
                        arg,
                        f"{context}.actions[{idx}].args[{arg_index}] in {source_path}",
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
        for widget_context, widget in _iter_v2_widgets(tab, f"{context}.ui.tabs[{tab_index}]"):
            widget_type = str(widget.get("type") or "").strip().lower()
            if widget_type == "log":
                stream = str(widget.get("stream") or "").strip()
                if not stream:
                    raise ValueError(f"{widget_context}.stream in {source_path} must be a non-empty string.")
                if stream not in log_streams:
                    raise ValueError(
                        f"{widget_context}.stream in {source_path} references unknown log stream '{stream}'."
                    )
            elif widget_type == "button":
                action_name = str(widget.get("action") or "").strip()
                if not ipc_mode and action_name and action_name not in action_names:
                    raise ValueError(
                        f"{widget_context}.action in {source_path} references unknown action '{action_name}'."
                    )
            elif widget_type == "profile_select":
                action_name = str(widget.get("action") or "").strip()
                if not ipc_mode and action_name and action_name not in action_names:
                    raise ValueError(
                        f"{widget_context}.action in {source_path} references unknown action '{action_name}'."
                    )
            elif widget_type == "config_editor":
                show_action = str(widget.get("showAction") or "").strip()
                set_action = str(widget.get("setAction") or "").strip()
                if not ipc_mode and show_action and show_action not in action_names:
                    raise ValueError(
                        f"{widget_context}.showAction in {source_path} references unknown action '{show_action}'."
                    )
                if not ipc_mode and set_action and set_action not in action_names:
                    raise ValueError(
                        f"{widget_context}.setAction in {source_path} references unknown action '{set_action}'."
                    )
            elif widget_type == "config_file_select":
                show_action = str(widget.get("showAction") or "").strip()
                set_action = str(widget.get("setAction") or "").strip()
                if not ipc_mode and show_action and show_action not in action_names:
                    raise ValueError(
                        f"{widget_context}.showAction in {source_path} references unknown action '{show_action}'."
                    )
                if not ipc_mode and set_action and set_action not in action_names:
                    raise ValueError(
                        f"{widget_context}.setAction in {source_path} references unknown action '{set_action}'."
                    )

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


def run_cmd(cmd: list[str], cwd: Path | None, timeout_seconds: float) -> tuple[int, str, str]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        creationflags=_no_window_creationflags(),
    )
    return int(completed.returncode), completed.stdout or "", completed.stderr or ""


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
                        "showOutputPanel": bool(command.get("showOutputPanel", False)),
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
    status_timeout = float(default_timeout_seconds)
    if isinstance(status, dict):
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
            action_cwd = str(action.get("cwd") or "").strip()
            normalized_args: list[dict[str, Any]] = []
            args_raw = action.get("args")
            if isinstance(args_raw, list):
                for arg in args_raw:
                    if not isinstance(arg, dict):
                        continue
                    arg_name = str(arg.get("name") or "").strip()
                    if not arg_name:
                        continue
                    options_raw = arg.get("options")
                    normalized_options = None
                    if isinstance(options_raw, list):
                        normalized_options = [str(item) for item in options_raw if str(item).strip()]
                    normalized_args.append(
                        {
                            "name": arg_name,
                            "label": str(arg.get("label") or arg_name),
                            "required": bool(arg.get("required", False)),
                            "type": str(arg.get("type") or "string").strip().lower(),
                            "placeholder": str(arg.get("placeholder") or ""),
                            "pattern": str(arg.get("pattern") or ""),
                            "optionsJsonpath": str(arg.get("optionsJsonpath") or ""),
                            "options": normalized_options,
                        }
                    )
            actions.append(
                {
                    "name": name,
                    "label": str(action.get("label") or name),
                    "cwd": action_cwd,
                    "cmd": cmd,
                    "timeoutSeconds": float(action.get("timeoutSeconds", 120.0)),
                    "confirm": str(action.get("confirm") or ""),
                    "showOutputPanel": bool(action.get("showOutputPanel", False)),
                    "mutex": str(action.get("mutex") or ""),
                    "detached": bool(action.get("detached", False)),
                    "args": normalized_args,
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
    control = _normalize_control_payload(target.get("control"))

    return {
        "configVersion": 2,
        "id": tid,
        "title": title,
        "refreshSeconds": float(target.get("refreshSeconds", default_refresh_seconds)),
        "status": {
            "timeoutSeconds": status_timeout,
        },
        "logs": logs,
        "actions": actions,
        "ui": {"tabs": tabs},
        "control": control,
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

    def _window_title(self) -> str:
        explicit_title = str(self.config.get("title") or "").strip()
        if explicit_title:
            return explicit_title

        labels: list[str] = []
        for target in self.targets:
            label = str(target.get("name") or target.get("title") or target.get("id") or "").strip()
            if label and label not in labels:
                labels.append(label)

        if len(labels) == 1:
            return labels[0]
        if labels:
            return " + ".join(labels) + " Monitor"
        return "Monitor"

    def _build_ui(self) -> None:
        self.root.title(self._window_title())
        self.root.geometry("1440x900")
        self._build_menu()

        top = ttk.Frame(self.root)
        top.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        if not self.targets:
            ttk.Label(top, text="No targets configured.").pack(fill=tk.X, padx=8, pady=8)
        elif len(self.targets) == 1:
            # Save vertical space by skipping the top-level target tab when only one target is present.
            self._build_target_panel(top, self.targets[0])
        else:
            target_notebook = ttk.Notebook(top)
            target_notebook.pack(fill=tk.BOTH, expand=True)
            for target in self.targets:
                tid = str(target.get("id") or "")
                title = str(target.get("title") or tid)
                frame = ttk.Frame(target_notebook)
                target_notebook.add(frame, text=title)
                self._build_target_panel(frame, target)

        footer = ttk.Frame(self.root)
        footer.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Label(footer, text="Console:").pack(side=tk.LEFT)
        ttk.Label(footer, textvariable=self.console_var).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(footer, text="Refresh Now", command=self._refresh_async).pack(side=tk.RIGHT)

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self.root)
        file_menu = tk.Menu(menu_bar, tearoff=0)
        file_menu.add_command(label="Relaunch", command=self._relaunch_app)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu_bar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menu_bar)

    def _build_target_panel(self, parent: ttk.Frame, target: dict[str, Any]) -> None:
        tid = str(target.get("id") or "")

        banner_var = tk.StringVar(value="")
        banner = ttk.Label(parent, textvariable=banner_var, foreground="#b00020")
        banner.pack(fill=tk.X, padx=8, pady=(6, 0))

        tabs = ttk.Notebook(parent)
        tabs.pack(fill=tk.BOTH, expand=True, padx=4, pady=6)

        runtime = {
            "target": target,
            "control": _target_control(target),
            "bannerVar": banner_var,
            "bindings": [],
            "profileSelectors": [],
            "actionSelectors": [],
            "actionMaps": [],
            "rowsTables": [],
            "fileViewers": [],
            "configEditors": [],
            "configFileSelectors": [],
            "logWidgetsByStream": {},
            "actionOutputWidget": None,
            "actionOutputPath": None,
            "lastGoodStatus": {},
            "lastStatusError": None,
            "nextRefreshAt": 0.0,
            "tabsWidget": tabs,
            "actionOutputTab": None,
            "actionOutputNotebook": None,
            "actionCatalogItems": [],
            "actionCatalogLoading": False,
            "actionCatalogLoaded": False,
            "actionCatalogError": "",
            "actionCatalogSignature": None,
        }
        self.target_runtime[tid] = runtime
        self._ensure_action_output_runtime(runtime)

        ui = target.get("ui") if isinstance(target.get("ui"), dict) else {}
        ui_tabs = ui.get("tabs") if isinstance(ui.get("tabs"), list) else []
        self._build_tabs(tabs, runtime, ui_tabs, top_level=True)
        self._refresh_action_catalog_async(tid, force=True)

    def _build_tabs(
        self,
        tabs_widget: ttk.Notebook,
        runtime: dict[str, Any],
        tabs: list[dict[str, Any]],
        *,
        top_level: bool = False,
    ) -> None:
        tabs_to_render = _order_top_level_tabs(tabs) if top_level else tabs
        for tab in tabs_to_render:
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
            self._build_tabs(child_tabs, runtime, children, top_level=False)
            return

        if widgets:
            self._build_widgets(tab_frame, runtime, widgets)
            return

        if children:
            child_tabs = ttk.Notebook(tab_frame)
            child_tabs.pack(fill=tk.BOTH, expand=True, padx=4, pady=6)
            self._build_tabs(child_tabs, runtime, children, top_level=False)
            return

        ttk.Label(tab_frame, text="No widgets configured.").pack(fill=tk.X, padx=8, pady=8)

    def _build_widgets(self, parent: ttk.Frame, runtime: dict[str, Any], widgets: list[dict[str, Any]]) -> None:
        widget_items = [item for item in widgets if isinstance(item, dict)]
        if not widget_items:
            return

        if len(widget_items) == 1:
            self._build_one_widget(parent, runtime, widget_items[0])
            return

        splitter_widget_types = {"log", "action_map", "action_output", "file_view", "rows_table"}
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

    def _build_one_widget(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        widget_type = str(widget.get("type") or "").strip().lower()
        if widget_type == "kv":
            self._build_widget_kv(parent, runtime, widget)
            return
        if widget_type == "table":
            self._build_widget_table(parent, runtime, widget)
            return
        if widget_type == "rows_table":
            self._build_widget_rows_table(parent, runtime, widget)
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
        if widget_type == "action_output":
            self._build_widget_action_output(parent, runtime, widget)
            return
        if widget_type == "text_block":
            self._build_widget_text_block(parent, runtime, widget)
            return
        if widget_type == "file_view":
            self._build_widget_file_view(parent, runtime, widget)
            return
        if widget_type == "config_editor":
            self._build_widget_config_editor(parent, runtime, widget)
            return
        if widget_type == "config_file_select":
            self._build_widget_config_file_select(parent, runtime, widget)
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

    def _build_widget_rows_table(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Rows"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6, anchor="n")

        rows_path = str(widget.get("rowsJsonpath") or "").strip()
        columns = widget.get("columns")
        if not rows_path or not isinstance(columns, list):
            return

        normalized_columns: list[dict[str, str]] = []
        for index, item in enumerate(columns):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            key = str(item.get("key") or "").strip()
            jsonpath = str(item.get("jsonpath") or "").strip()
            if not label or (not key and not jsonpath):
                continue
            normalized_columns.append(
                {
                    "id": f"col{index + 1}",
                    "label": label,
                    "key": key,
                    "jsonpath": jsonpath,
                }
            )
        if not normalized_columns:
            return

        table_wrap = ttk.Frame(frame)
        table_wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=(4, 2))
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)

        x_scroll = ttk.Scrollbar(table_wrap, orient=tk.HORIZONTAL)
        y_scroll = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL)
        tree = ttk.Treeview(
            table_wrap,
            columns=[column["id"] for column in normalized_columns],
            show="headings",
            xscrollcommand=x_scroll.set,
            yscrollcommand=y_scroll.set,
        )
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        x_scroll.configure(command=tree.xview)
        y_scroll.configure(command=tree.yview)

        for column in normalized_columns:
            label = column["label"]
            width = max(110, min(360, len(label) * 9 + 36))
            tree.heading(column["id"], text=label)
            tree.column(column["id"], anchor="w", width=width, stretch=True)

        empty_text = str(widget.get("emptyText") or "(no rows)")
        empty_var = tk.StringVar(value=empty_text)
        ttk.Label(frame, textvariable=empty_var).pack(fill=tk.X, padx=6, pady=(0, 4))

        max_rows = max(1, int(widget.get("maxRows", 200)))
        runtime["rowsTables"].append(
            {
                "rowsPath": rows_path,
                "columns": normalized_columns,
                "tree": tree,
                "emptyVar": empty_var,
                "emptyText": empty_text,
                "maxRows": max_rows,
                "lastSignature": None,
            }
        )

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

    def _ipc_control_for_runtime(self, runtime: dict[str, Any]) -> dict[str, Any] | None:
        control = runtime.get("control")
        if not isinstance(control, dict):
            return None
        if str(control.get("mode") or "").strip().lower() != "ipc":
            return None
        endpoint = str(control.get("endpoint") or "").strip()
        app_id = str(control.get("appId") or "").strip()
        if not endpoint or not app_id:
            return None
        return control

    def _action_items_for_runtime(self, runtime: dict[str, Any]) -> list[dict[str, Any]]:
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_actions_raw = target.get("actions") if isinstance(target.get("actions"), list) else []
        target_actions = [item for item in target_actions_raw if isinstance(item, dict)]

        control = self._ipc_control_for_runtime(runtime)
        if control is None:
            return target_actions

        catalog_items = runtime.get("actionCatalogItems")
        catalog_actions = [item for item in catalog_items if isinstance(item, dict)] if isinstance(catalog_items, list) else []
        if not catalog_actions:
            return target_actions

        merged: list[dict[str, Any]] = list(catalog_actions)
        seen_names = {
            str(item.get("name") or "").strip()
            for item in catalog_actions
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        for item in target_actions:
            name = str(item.get("name") or "").strip()
            if name and name in seen_names:
                continue
            merged.append(item)
        return merged

    def _has_local_action_command(self, target: dict[str, Any], action_name: str) -> bool:
        action = self._find_target_action(target, action_name)
        if not isinstance(action, dict):
            return False
        return bool(_normalize_cmd(action.get("cmd")))

    def _action_prefers_local_command(self, action: dict[str, Any]) -> bool:
        return bool(_normalize_cmd(action.get("cmd")))

    def _invoke_action(
        self,
        target_id: str,
        action_name: str,
        action_value: str | None = None,
        action_args: dict[str, str] | None = None,
    ) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return
        target = runtime.get("target")
        if not isinstance(target, dict):
            return
        actions = self._action_items_for_runtime(runtime)
        action = next((item for item in actions if isinstance(item, dict) and str(item.get("name")) == action_name), None)
        if action is None:
            self.console_var.set(f"Action not found: {action_name}")
            return

        confirm_text = str(action.get("confirm") or "").strip()
        if confirm_text and not messagebox.askyesno("Confirm Action", confirm_text):
            return

        show_output_panel = bool(action.get("showOutputPanel", False))
        if show_output_panel:
            tabs_widget = runtime.get("actionOutputNotebook")
            if not isinstance(tabs_widget, ttk.Notebook):
                tabs_widget = runtime.get("tabsWidget")
            action_output_tab = runtime.get("actionOutputTab")
            if isinstance(tabs_widget, ttk.Notebook) and isinstance(action_output_tab, ttk.Frame):
                try:
                    tabs_widget.select(action_output_tab)
                except Exception:
                    pass

        control = self._ipc_control_for_runtime(runtime)
        if self._action_prefers_local_command(action):
            thread = threading.Thread(
                target=self._run_action,
                args=(target_id, action, action_value, action_args),
                daemon=True,
            )
        elif control is not None:
            thread = threading.Thread(
                target=self._run_action_ipc,
                args=(target_id, action, action_value, action_args),
                daemon=True,
            )
        else:
            thread = threading.Thread(
                target=self._run_action,
                args=(target_id, action, action_value, action_args),
                daemon=True,
            )
        thread.start()

    def _refresh_action_catalog_async(self, target_id: str, *, force: bool = False) -> None:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return
        control = self._ipc_control_for_runtime(runtime)
        if control is None:
            return
        if bool(runtime.get("actionCatalogLoading", False)):
            return
        if bool(runtime.get("actionCatalogLoaded", False)) and not force:
            return
        runtime["actionCatalogLoading"] = True
        thread = threading.Thread(target=self._load_action_catalog, args=(target_id,), daemon=True)
        thread.start()

    def _load_action_catalog(self, target_id: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return
        control = self._ipc_control_for_runtime(runtime)
        if control is None:
            self.root.after(0, lambda: self._finalize_action_catalog_load(target_id, [], ""))
            return

        endpoint = str(control.get("endpoint") or "")
        app_id = str(control.get("appId") or "")
        timeout_seconds = float(control.get("timeoutSeconds") or DEFAULT_CONTROL_TIMEOUT_SECONDS)
        rc, response_obj, error_text = _request_ipc_v0(
            endpoint,
            {"method": "action.list", "params": {"appId": app_id}},
            timeout_seconds=timeout_seconds,
        )
        if rc != 0:
            self.root.after(
                0,
                lambda: self._finalize_action_catalog_load(
                    target_id,
                    [],
                    error_text or f"failed to fetch action catalog rc={rc}",
                ),
            )
            return

        response = response_obj.get("response")
        actions_raw = response.get("actions") if isinstance(response, dict) else None
        if not isinstance(actions_raw, list):
            self.root.after(
                0,
                lambda: self._finalize_action_catalog_load(target_id, [], "invalid action catalog payload"),
            )
            return

        actions: list[dict[str, Any]] = []
        for item in actions_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            action_item = {
                "name": name,
                "label": str(item.get("label") or name),
                "args": item.get("args") if isinstance(item.get("args"), list) else [],
            }
            cmd_value = item.get("cmd")
            if isinstance(cmd_value, list):
                action_item["cmd"] = [str(part) for part in cmd_value]
            actions.append(action_item)

        self.root.after(0, lambda: self._finalize_action_catalog_load(target_id, actions, ""))

    def _finalize_action_catalog_load(self, target_id: str, actions: list[dict[str, Any]], error_text: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return
        runtime["actionCatalogLoading"] = False
        runtime["actionCatalogLoaded"] = not bool(error_text)
        runtime["actionCatalogError"] = str(error_text or "")
        if not error_text:
            runtime["actionCatalogItems"] = actions
        signature_items: list[tuple[str, str]] = []
        for item in runtime.get("actionCatalogItems", []):
            if not isinstance(item, dict):
                continue
            signature_items.append((str(item.get("name") or ""), str(item.get("label") or "")))
        runtime["actionCatalogSignature"] = tuple(signature_items)
        self._refresh_action_widgets(target_id)

    def _refresh_action_widgets(self, target_id: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return
        payload = runtime.get("lastGoodStatus")
        payload_obj = payload if isinstance(payload, dict) else {}
        for selector in list(runtime.get("actionSelectors") or []):
            if not isinstance(selector, dict):
                continue
            refresh_fn = selector.get("refreshFn")
            if callable(refresh_fn):
                try:
                    refresh_fn(payload_obj)
                except Exception:
                    pass
        for action_map in list(runtime.get("actionMaps") or []):
            if not isinstance(action_map, dict):
                continue
            refresh_fn = action_map.get("refreshFn")
            if callable(refresh_fn):
                try:
                    refresh_fn()
                except Exception:
                    pass

    def _build_widget_action_map(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Commands"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        include_commands = bool(widget.get("includeCommands", True))
        show_action_name = bool(widget.get("showActionName", True))
        include_prefix = str(widget.get("includePrefix") or "").strip()
        include_regex = str(widget.get("includeRegex") or "").strip()
        matcher = re.compile(include_regex) if include_regex else None

        text = tk.Text(frame, wrap=tk.NONE, height=12)
        text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        action_map_runtime: dict[str, Any] = {
            "textWidget": text,
            "includeCommands": include_commands,
            "showActionName": show_action_name,
            "includePrefix": include_prefix,
            "matcher": matcher,
        }

        def refresh_action_map() -> None:
            actions = self._action_items_for_runtime(runtime)
            lines: list[str] = []
            for item in actions:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if include_prefix and not name.startswith(include_prefix):
                    continue
                if matcher and matcher.search(name) is None:
                    continue
                label = str(item.get("label") or name).strip()
                cmd = _normalize_cmd(item.get("cmd"))
                head = label
                if show_action_name and name:
                    head = f"{label} ({name})"
                lines.append(head)
                if include_commands:
                    if cmd:
                        lines.append("  " + " ".join(cmd))
                    elif self._ipc_control_for_runtime(runtime) is not None:
                        lines.append("  [IPC action]")
                lines.append("")
            render = "\n".join(lines).strip() or "(no actions configured)"
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert(tk.END, render + "\n")
            text.configure(state=tk.DISABLED)

        action_map_runtime["refreshFn"] = refresh_action_map
        runtime["actionMaps"].append(action_map_runtime)
        refresh_action_map()

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
        matcher = re.compile(include_regex) if include_regex else None

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, padx=8, pady=6)
        selected_var = tk.StringVar(value=empty_label)
        combo = ttk.Combobox(row, textvariable=selected_var, state="readonly", width=50)
        combo["values"] = [empty_label]
        combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        combo.set(empty_label)

        arg_row = ttk.Frame(frame)
        arg_row.pack(fill=tk.X, padx=8, pady=(0, 6))
        arg_label_var = tk.StringVar(value="")
        arg_status_var = tk.StringVar(value="")
        arg_value_var = tk.StringVar(value="")
        arg_label_widget = ttk.Label(arg_row, textvariable=arg_label_var, width=24)
        arg_entry_widget = ttk.Entry(arg_row, textvariable=arg_value_var, width=40)
        arg_combo_widget = ttk.Combobox(arg_row, textvariable=arg_value_var, state="readonly", width=40)
        arg_label_widget.pack_forget()
        arg_entry_widget.pack_forget()
        arg_combo_widget.pack_forget()
        ttk.Label(frame, textvariable=arg_status_var).pack(fill=tk.X, padx=8, pady=(0, 2))

        selector: dict[str, Any] = {
            "selectedVar": selected_var,
            "labelToName": {},
            "eligible": [],
            "argRow": arg_row,
            "argLabelVar": arg_label_var,
            "argStatusVar": arg_status_var,
            "argValueVar": arg_value_var,
            "argLabelWidget": arg_label_widget,
            "argEntryWidget": arg_entry_widget,
            "argComboWidget": arg_combo_widget,
            "currentArgSpec": None,
            "currentArgOptions": [],
            "lastPayload": {},
            "emptyLabel": empty_label,
            "combo": combo,
            "includePrefix": include_prefix,
            "matcher": matcher,
        }

        def run_selected() -> None:
            selected = str(selected_var.get() or "").strip()
            label_to_name_obj = selector.get("labelToName")
            label_to_name = label_to_name_obj if isinstance(label_to_name_obj, dict) else {}
            action_name = str(label_to_name.get(selected) or "")
            if not action_name:
                self.console_var.set("No action selected.")
                return
            eligible_obj = selector.get("eligible")
            eligible = eligible_obj if isinstance(eligible_obj, list) else []
            action = next((item for item in eligible if str(item.get("name") or "") == action_name), None)
            if not isinstance(action, dict):
                self.console_var.set("Selected action is unavailable.")
                return
            action_args: dict[str, str] = {}
            arg_spec = _action_primary_arg(action)
            if arg_spec is not None:
                raw_value = str(arg_value_var.get() or "")
                options_local = selector.get("currentArgOptions")
                options_for_validation = options_local if isinstance(options_local, list) else []
                normalized_value, error_message = _validate_action_arg_value(
                    raw_value,
                    arg_spec,
                    options_for_validation,
                )
                if error_message:
                    arg_status_var.set(error_message)
                    self.console_var.set(error_message)
                    return
                arg_name = str(arg_spec.get("name") or "value").strip() or "value"
                action_args[arg_name] = normalized_value or ""
            self._invoke_action(target_id, action_name, action_args=action_args or None)

        ttk.Button(row, text=run_label, command=run_selected).pack(side=tk.LEFT, padx=(8, 0))
        if show_command:
            command_var = tk.StringVar(value="")
            ttk.Label(frame, textvariable=command_var).pack(fill=tk.X, padx=8, pady=(0, 6))
        else:
            command_var = tk.StringVar(value="")

        def update_selector(payload: dict[str, Any] | None = None) -> None:
            payload_obj = payload if isinstance(payload, dict) else {}
            selector["lastPayload"] = payload_obj
            all_actions = self._action_items_for_runtime(runtime)
            eligible: list[dict[str, Any]] = []
            label_to_name: dict[str, str] = {}
            options: list[str] = []
            for item in all_actions:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if include_prefix and not name.startswith(include_prefix):
                    continue
                if matcher and matcher.search(name) is None:
                    continue
                label = str(item.get("label") or name).strip()
                display = f"{label} ({name})" if name else label
                label_to_name[display] = name
                options.append(display)
                eligible.append(item)
            selector["labelToName"] = label_to_name
            selector["eligible"] = eligible

            combo_values = options if options else [empty_label]
            combo["values"] = combo_values
            if options:
                selected_now = str(selected_var.get() or "").strip()
                if selected_now not in options:
                    selected_var.set(options[0])
            else:
                selected_var.set(empty_label)

            selected = str(selected_var.get() or "").strip()
            action_name = label_to_name.get(selected, "")
            action = next((item for item in eligible if str(item.get("name") or "") == action_name), None)
            if not isinstance(action, dict):
                if show_command:
                    command_var.set("-")
                arg_status_var.set("")
                selector["currentArgSpec"] = None
                selector["currentArgOptions"] = []
                arg_label_widget.pack_forget()
                arg_entry_widget.pack_forget()
                arg_combo_widget.pack_forget()
                return

            cmd = _normalize_cmd(action.get("cmd"))
            if show_command:
                if cmd:
                    command_var.set(" ".join(cmd))
                elif self._ipc_control_for_runtime(runtime) is not None:
                    command_var.set("[IPC action]")
                else:
                    command_var.set("-")

            arg_spec = _action_primary_arg(action)
            selector["currentArgSpec"] = arg_spec
            if arg_spec is None:
                arg_status_var.set("")
                selector["currentArgOptions"] = []
                arg_label_widget.pack_forget()
                arg_entry_widget.pack_forget()
                arg_combo_widget.pack_forget()
                return

            arg_name = str(arg_spec.get("name") or "value").strip() or "value"
            arg_label = str(arg_spec.get("label") or arg_name)
            arg_label_var.set(arg_label + ":")
            arg_options = _action_arg_options(arg_spec, payload_obj)
            selector["currentArgOptions"] = arg_options
            current_value = str(arg_value_var.get() or "").strip()
            arg_label_widget.pack(side=tk.LEFT, padx=(0, 6))
            if arg_options:
                arg_entry_widget.pack_forget()
                arg_combo_widget["values"] = arg_options
                if current_value not in arg_options:
                    current_value = arg_options[0]
                arg_value_var.set(current_value)
                arg_combo_widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            else:
                arg_combo_widget.pack_forget()
                if not current_value:
                    placeholder = str(arg_spec.get("placeholder") or "").strip()
                    if placeholder:
                        arg_value_var.set(placeholder)
                arg_entry_widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            arg_status_var.set("")

        selected_var.trace_add("write", lambda *_: update_selector(selector.get("lastPayload")))
        update_selector({})
        selector["refreshFn"] = update_selector
        runtime["actionSelectors"].append(selector)

    def _ensure_action_output_runtime(self, runtime: dict[str, Any]) -> None:
        if isinstance(runtime.get("actionOutputPath"), Path) and isinstance(runtime.get("actionOutputBuffer"), ActionOutputBuffer):
            return
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "target")
        action_output_root = self.config_path.parent / "action-output"
        action_output_root.mkdir(parents=True, exist_ok=True)
        action_output_path = (action_output_root / f"{target_id}.log").resolve()
        runtime["actionOutputPath"] = action_output_path
        action_output_cfg = target.get("actionOutput") if isinstance(target.get("actionOutput"), dict) else {}
        runtime["actionOutputBuffer"] = ActionOutputBuffer(
            max_lines=int(action_output_cfg.get("maxLines", DEFAULT_ACTION_OUTPUT_MAX_LINES)),
            max_bytes=int(action_output_cfg.get("maxBytes", DEFAULT_ACTION_OUTPUT_MAX_BYTES)),
        )

    def _resolve_tab_for_widget_parent(self, parent: ttk.Frame) -> tuple[ttk.Notebook | None, ttk.Frame | None]:
        current: Any = parent
        while isinstance(current, tk.Widget):
            parent_name = str(current.winfo_parent() or "").strip()
            if not parent_name:
                break
            try:
                parent_widget = current.nametowidget(parent_name)
            except Exception:
                break
            if isinstance(parent_widget, ttk.Notebook) and isinstance(current, ttk.Frame):
                return parent_widget, current
            current = parent_widget
        return None, None

    def _build_widget_action_output(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        self._ensure_action_output_runtime(runtime)
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Action Output"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6, anchor="n")

        output_path = runtime.get("actionOutputPath")
        output_path_text = str(output_path) if isinstance(output_path, Path) else ""

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X, padx=6, pady=(6, 2))
        output_path_var = tk.StringVar(value=output_path_text)
        ttk.Label(toolbar, text="Source:").pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=output_path_var).pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)
        ttk.Button(toolbar, text="Open", command=lambda var=output_path_var: self._open_file_path(var.get())).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(toolbar, text="Copy", command=lambda var=output_path_var: self._copy_to_clipboard(var.get())).pack(
            side=tk.RIGHT
        )
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "")
        ttk.Button(toolbar, text="Clear", command=lambda tid=target_id: self._clear_action_output(tid)).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        action_text = tk.Text(frame, wrap=tk.NONE, height=10)
        action_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 6))
        runtime["actionOutputWidget"] = action_text

        notebook, tab_frame = self._resolve_tab_for_widget_parent(parent)
        if isinstance(notebook, ttk.Notebook) and isinstance(tab_frame, ttk.Frame):
            runtime["actionOutputNotebook"] = notebook
            runtime["actionOutputTab"] = tab_frame

    def _build_widget_text_block(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Notes"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        text_value = str(widget.get("text") or "").strip()
        height = max(4, int(widget.get("height", 8)))
        text = tk.Text(frame, wrap=tk.WORD, height=height)
        text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        text.insert(tk.END, (text_value or "(empty)") + "\n")
        text.configure(state=tk.DISABLED)

    def _build_widget_file_view(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "File"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        show_content = bool(widget.get("showContent", True))

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
        text: tk.Text | None = None
        if show_content:
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

    def _build_widget_config_editor(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Config Editor"))
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X, padx=4, pady=(4, 2))

        path_var = tk.StringVar(value="-")
        status_var = tk.StringVar(value="Waiting for status refresh...")

        ttk.Label(toolbar, text="Path:").pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=path_var).pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)
        ttk.Button(toolbar, text="Open", command=lambda var=path_var: self._open_file_path(var.get())).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(toolbar, text="Copy", command=lambda var=path_var: self._copy_to_clipboard(var.get())).pack(side=tk.RIGHT)

        status_row = ttk.Frame(frame)
        status_row.pack(fill=tk.X, padx=4, pady=(0, 2))
        ttk.Label(status_row, textvariable=status_var).pack(side=tk.LEFT)

        table_wrap = ttk.Frame(frame)
        table_wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 4))
        rows_canvas = tk.Canvas(table_wrap, highlightthickness=0)
        rows_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vertical_scroll = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL, command=rows_canvas.yview)
        vertical_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        horizontal_scroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=rows_canvas.xview)
        horizontal_scroll.pack(fill=tk.X, padx=4, pady=(0, 4))
        rows_canvas.configure(yscrollcommand=vertical_scroll.set, xscrollcommand=horizontal_scroll.set)

        rows_frame = ttk.Frame(rows_canvas)
        rows_canvas.create_window((0, 0), window=rows_frame, anchor="nw")

        def sync_scroll_region(_: Any = None) -> None:
            try:
                rows_canvas.configure(scrollregion=rows_canvas.bbox("all"))
            except Exception:
                pass

        rows_frame.bind("<Configure>", sync_scroll_region)
        rows_canvas.bind("<Configure>", sync_scroll_region)

        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "")
        include_keys = widget.get("includeKeys")
        include_keys_set = {str(item).strip() for item in include_keys} if isinstance(include_keys, list) else set()
        include_keys_set = {item for item in include_keys_set if item}
        exclude_keys = widget.get("excludeKeys")
        exclude_keys_set = {str(item).strip() for item in exclude_keys} if isinstance(exclude_keys, list) else set()
        exclude_keys_set = {item for item in exclude_keys_set if item}

        editor: dict[str, Any] = {
            "targetId": target_id,
            "showAction": str(widget.get("showAction") or "").strip(),
            "setAction": str(widget.get("setAction") or "").strip(),
            "pathJsonpath": str(widget.get("pathJsonpath") or "").strip(),
            "pathLiteral": str(widget.get("pathLiteral") or "").strip(),
            "pathKey": str(widget.get("pathKey") or "").strip(),
            "includePrefix": str(widget.get("includePrefix") or "").strip(),
            "includeKeys": include_keys_set,
            "excludeKeys": exclude_keys_set,
            "settableOnly": bool(widget.get("settableOnly", True)),
            "pathVar": path_var,
            "statusVar": status_var,
            "rowsFrame": rows_frame,
            "rowsCanvas": rows_canvas,
            "loading": False,
            "needsRefresh": True,
            "hasLoadedOnce": False,
            "lastPathValue": "",
            "lastEntriesSignature": None,
            "rowState": {},
        }

        reload_label = str(widget.get("reloadLabel") or "Reload")
        ttk.Button(
            status_row,
            text=reload_label,
            command=lambda tid=target_id, item=editor: self._refresh_config_editor_async(tid, item, force=True),
        ).pack(side=tk.RIGHT, padx=(6, 0))

        runtime["configEditors"].append(editor)

    def _build_widget_config_file_select(self, parent: ttk.Frame, runtime: dict[str, Any], widget: dict[str, Any]) -> None:
        frame = ttk.LabelFrame(parent, text=str(widget.get("title") or "Active File"))
        frame.pack(fill=tk.X, padx=8, pady=6, anchor="n")

        empty_label = str(widget.get("emptyLabel") or "Select file")
        apply_label = str(widget.get("applyLabel") or "Set")
        reload_label = str(widget.get("reloadLabel") or "Reload")

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, padx=8, pady=(6, 2))
        current_var = tk.StringVar(value="-")
        selected_var = tk.StringVar(value="")
        status_var = tk.StringVar(value="")
        path_var = tk.StringVar(value="-")

        ttk.Label(row, text="Current:").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(row, textvariable=current_var).pack(side=tk.LEFT, padx=(0, 12))
        combo = ttk.Combobox(row, textvariable=selected_var, state="readonly", width=28)
        combo["values"] = [empty_label]
        combo.set(empty_label)
        combo.pack(side=tk.LEFT, padx=(0, 8), fill=tk.X, expand=True)
        apply_button = ttk.Button(row, text=apply_label)
        apply_button.pack(side=tk.LEFT)

        path_row = ttk.Frame(frame)
        path_row.pack(fill=tk.X, padx=8, pady=(0, 2))
        ttk.Label(path_row, text="Path:").pack(side=tk.LEFT)
        ttk.Label(path_row, textvariable=path_var).pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)
        ttk.Button(path_row, text="Open", command=lambda var=path_var: self._open_file_path(var.get())).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(path_row, text="Copy", command=lambda var=path_var: self._copy_to_clipboard(var.get())).pack(side=tk.RIGHT)

        status_row = ttk.Frame(frame)
        status_row.pack(fill=tk.X, padx=8, pady=(0, 4))
        ttk.Label(status_row, textvariable=status_var).pack(side=tk.LEFT)

        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "")
        selector: dict[str, Any] = {
            "targetId": target_id,
            "showAction": str(widget.get("showAction") or "").strip(),
            "setAction": str(widget.get("setAction") or "").strip(),
            "key": str(widget.get("key") or "").strip(),
            "pathKey": str(widget.get("pathKey") or "").strip(),
            "emptyLabel": empty_label,
            "currentVar": current_var,
            "selectedVar": selected_var,
            "pathVar": path_var,
            "statusVar": status_var,
            "combo": combo,
            "applyButton": apply_button,
            "optionsMap": {},
            "loading": False,
            "needsRefresh": True,
            "hasLoadedOnce": False,
            "lastSignature": None,
            "lastPathIdentity": "",
        }
        apply_button.configure(
            command=lambda tid=target_id, item=selector: self._set_config_file_selector_value(tid, item),
        )
        ttk.Button(
            status_row,
            text=reload_label,
            command=lambda tid=target_id, item=selector: self._refresh_config_file_selector_async(tid, item, force=True),
        ).pack(side=tk.RIGHT, padx=(6, 0))
        runtime["configFileSelectors"].append(selector)

    def _refresh_config_file_selectors(self, runtime: dict[str, Any]) -> None:
        selectors = runtime.get("configFileSelectors")
        if not isinstance(selectors, list):
            return
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "")
        for selector in selectors:
            if not isinstance(selector, dict):
                continue
            self._refresh_config_file_selector_async(target_id, selector, show_loading=False)

    def _refresh_config_file_selector_async(
        self,
        target_id: str,
        selector: dict[str, Any],
        *,
        force: bool = False,
        show_loading: bool | None = None,
    ) -> None:
        if force:
            selector["needsRefresh"] = True
        if not bool(selector.get("needsRefresh", False)):
            return
        if bool(selector.get("loading", False)):
            return
        selector["loading"] = True
        selector["needsRefresh"] = False
        if show_loading is None:
            show_loading = not bool(selector.get("hasLoadedOnce", False))
        status_var = selector.get("statusVar")
        if show_loading:
            self._set_stringvar_if_changed(status_var, "Loading...")
        thread = threading.Thread(target=self._load_config_file_selector_data, args=(target_id, selector), daemon=True)
        thread.start()

    def _load_config_file_selector_data(self, target_id: str, selector: dict[str, Any]) -> None:
        show_action = str(selector.get("showAction") or "").strip()
        payload, load_error = self._load_config_payload(target_id, show_action)
        if payload is None:
            self.root.after(
                0,
                lambda: self._finalize_config_file_selector_load(selector, "", "", [], {}, load_error),
            )
            return

        key = str(selector.get("key") or "").strip()
        path_key = str(selector.get("pathKey") or "").strip()
        current_text = ""
        options_text: list[str] = []
        options_map: dict[str, Any] = {}
        path_value = ""

        entries = payload.get("entries")
        if isinstance(entries, list):
            match = next(
                (
                    item
                    for item in entries
                    if isinstance(item, dict) and str(item.get("key") or "").strip() == key
                ),
                None,
            )
            if isinstance(match, dict):
                current_raw = match.get("value")
                current_text = self._config_editor_value_text(current_raw)
                allowed_raw = match.get("allowed")
                if isinstance(allowed_raw, list):
                    for item in allowed_raw:
                        option_text = self._config_editor_value_text(item)
                        if option_text in options_map:
                            continue
                        options_text.append(option_text)
                        options_map[option_text] = item
                if not options_text and current_text:
                    options_text = [current_text]
                    options_map[current_text] = current_raw

        path_entries = payload.get("paths")
        if isinstance(path_entries, list):
            path_match = next(
                (
                    item
                    for item in path_entries
                    if isinstance(item, dict) and str(item.get("key") or "").strip() == path_key
                ),
                None,
            )
            if isinstance(path_match, dict):
                path_value = str(path_match.get("value") or "").strip()

        self.root.after(
            0,
            lambda: self._finalize_config_file_selector_load(
                selector,
                current_text,
                path_value,
                options_text,
                options_map,
                "",
            ),
        )

    def _finalize_config_file_selector_load(
        self,
        selector: dict[str, Any],
        current_text: str,
        path_value: str,
        options_text: list[str],
        options_map: dict[str, Any],
        error_message: str,
    ) -> None:
        selector["loading"] = False
        status_var = selector.get("statusVar")
        if error_message:
            self._set_stringvar_if_changed(status_var, error_message)
            return

        selector["hasLoadedOnce"] = True
        self._set_stringvar_if_changed(status_var, "Ready.")

        path_var = selector.get("pathVar")
        display_path = path_value or "-"
        self._set_stringvar_if_changed(path_var, display_path)
        selector["lastPathIdentity"] = self._path_identity(path_value)

        signature = (current_text, tuple(options_text), selector.get("lastPathIdentity"))
        if signature == selector.get("lastSignature"):
            return
        selector["lastSignature"] = signature
        selector["optionsMap"] = dict(options_map)

        current_var = selector.get("currentVar")
        self._set_stringvar_if_changed(current_var, current_text or "-")

        combo = selector.get("combo")
        selected_var = selector.get("selectedVar")
        empty_label = str(selector.get("emptyLabel") or "Select file")
        apply_button = selector.get("applyButton")
        has_options = len(options_text) > 0
        values = options_text if has_options else [empty_label]
        if isinstance(combo, ttk.Combobox):
            combo["values"] = values
        if isinstance(selected_var, tk.StringVar):
            selected = str(selected_var.get() or "").strip()
            if has_options:
                if selected in options_text:
                    pass
                elif current_text in options_text:
                    selected_var.set(current_text)
                else:
                    selected_var.set(options_text[0])
            else:
                selected_var.set(empty_label)
        if isinstance(apply_button, ttk.Button):
            apply_button.configure(state=(tk.NORMAL if has_options else tk.DISABLED))

    def _set_config_file_selector_value(self, target_id: str, selector: dict[str, Any]) -> None:
        selected_var = selector.get("selectedVar")
        status_var = selector.get("statusVar")
        empty_label = str(selector.get("emptyLabel") or "Select file")
        selected_text = str(selected_var.get() if isinstance(selected_var, tk.StringVar) else "").strip()
        if not selected_text or selected_text == empty_label:
            self._set_stringvar_if_changed(status_var, "Select a value.")
            return

        options_map_raw = selector.get("optionsMap")
        options_map = options_map_raw if isinstance(options_map_raw, dict) else {}
        selected_value = options_map.get(selected_text, selected_text)
        key = str(selector.get("key") or "").strip()
        if not key:
            self._set_stringvar_if_changed(status_var, "Selector key is missing.")
            return

        set_value = self._config_editor_set_arg(selected_value)
        set_action = str(selector.get("setAction") or "").strip()
        self._set_stringvar_if_changed(status_var, "saving...")

        def run_set() -> None:
            rc, error_text = self._set_config_value(target_id, set_action, key, set_value)
            if rc != 0:
                self.root.after(0, lambda: self._set_stringvar_if_changed(status_var, error_text))
                return
            self._mark_target_config_widgets_for_refresh(target_id)
            self.root.after(0, lambda: self._refresh_target_config_widgets(target_id, show_loading=False))
            self._refresh_async()

        threading.Thread(target=run_set, daemon=True).start()

    def _find_target_action(self, target: dict[str, Any], action_name: str) -> dict[str, Any] | None:
        actions = target.get("actions")
        if not isinstance(actions, list):
            return None
        return next(
            (
                item
                for item in actions
                if isinstance(item, dict) and str(item.get("name") or "").strip() == str(action_name or "").strip()
            ),
            None,
        )

    def _run_named_action_command(
        self,
        target_id: str,
        action_name: str,
        *,
        replacements: dict[str, str] | None = None,
    ) -> tuple[int, str, str, str]:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return 2, "", "", f"unknown target: {target_id}"
        target = runtime.get("target")
        if not isinstance(target, dict):
            return 2, "", "", f"target runtime missing for: {target_id}"

        action = self._find_target_action(target, action_name)
        if action is None:
            return 2, "", "", f"action not found: {action_name}"

        cmd = _normalize_cmd(action.get("cmd"))
        if not cmd:
            return 2, "", "", f"action command is empty: {action_name}"

        replacements_map = {str(k): str(v) for k, v in (replacements or {}).items()}
        if replacements_map:
            for key, value in replacements_map.items():
                token = "{" + key + "}"
                cmd = [part.replace(token, value) for part in cmd]

        cwd_text = str(action.get("cwd") or "").strip()
        if replacements_map and cwd_text:
            for key, value in replacements_map.items():
                token = "{" + key + "}"
                cwd_text = cwd_text.replace(token, value)
        cwd = Path(cwd_text) if cwd_text else None
        timeout_seconds = float(action.get("timeoutSeconds") or self.default_command_timeout_seconds)

        try:
            rc, stdout, stderr = run_cmd(cmd, cwd, timeout_seconds)
            return rc, stdout, stderr, ""
        except subprocess.TimeoutExpired:
            return 2, "", "", f"action timeout after {timeout_seconds:.1f}s: {action_name}"
        except Exception as ex:
            return 2, "", "", str(ex)

    def _run_control_config_get(self, target_id: str) -> tuple[int, dict[str, Any], str]:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return 2, {}, f"unknown target: {target_id}"
        control = self._ipc_control_for_runtime(runtime)
        if control is None:
            return 2, {}, "ipc control is not configured"
        endpoint = str(control.get("endpoint") or "")
        app_id = str(control.get("appId") or "")
        timeout_seconds = float(control.get("timeoutSeconds") or DEFAULT_CONTROL_TIMEOUT_SECONDS)
        rc, response, error_text = _request_ipc_v0(
            endpoint,
            {"method": "config.get", "params": {"appId": app_id}},
            timeout_seconds=timeout_seconds,
        )
        if rc != 0:
            return rc, {}, error_text or "config.get failed"
        payload = response.get("response")
        if not isinstance(payload, dict):
            return 2, {}, "config.get returned invalid payload"
        return 0, _normalize_config_show_payload(payload), ""

    def _run_control_config_set(self, target_id: str, key: str, value: str) -> tuple[int, str]:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return 2, f"unknown target: {target_id}"
        control = self._ipc_control_for_runtime(runtime)
        if control is None:
            return 2, "ipc control is not configured"
        endpoint = str(control.get("endpoint") or "")
        app_id = str(control.get("appId") or "")
        timeout_seconds = float(control.get("timeoutSeconds") or DEFAULT_CONTROL_TIMEOUT_SECONDS)
        rc, response, error_text = _request_ipc_v0(
            endpoint,
            {"method": "config.set", "params": {"appId": app_id, "key": key, "value": value}},
            timeout_seconds=timeout_seconds,
        )
        if rc != 0:
            return rc, error_text or "config.set failed"
        if not isinstance(response.get("response"), dict):
            return 2, "config.set returned invalid payload"
        return 0, ""

    def _load_config_payload(self, target_id: str, show_action: str) -> tuple[dict[str, Any] | None, str]:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return None, f"unknown target: {target_id}"
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        if self._ipc_control_for_runtime(runtime) is not None:
            rc, payload, error_text = self._run_control_config_get(target_id)
            if rc == 0:
                return payload, ""
            if not self._has_local_action_command(target, show_action):
                return None, error_text or f"config.get failed rc={rc}"

        rc, stdout, stderr, command_error = self._run_named_action_command(target_id, show_action)
        if command_error:
            return None, command_error
        if rc != 0:
            message = (stderr or stdout or f"show action failed rc={rc}").strip()
            return None, message
        payload, parse_error = try_extract_json_object(stdout)
        if payload is None:
            return None, parse_error
        return _normalize_config_show_payload(payload), ""

    def _set_config_value(self, target_id: str, set_action: str, key: str, value: str) -> tuple[int, str]:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return 2, f"unknown target: {target_id}"
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        if self._ipc_control_for_runtime(runtime) is not None:
            rc, error_text = self._run_control_config_set(target_id, key, value)
            if rc == 0:
                return 0, ""
            if not self._has_local_action_command(target, set_action):
                return rc, error_text

        rc, stdout, stderr, command_error = self._run_named_action_command(
            target_id,
            set_action,
            replacements={"key": key, "value": value},
        )
        if command_error:
            return 2, command_error
        if rc != 0:
            message = (stderr or stdout or f"set failed rc={rc}").strip()
            return rc, message.splitlines()[0] if message else f"set failed rc={rc}"
        return 0, ""

    def _refresh_config_editors(self, runtime: dict[str, Any], payload: dict[str, Any]) -> None:
        editors = runtime.get("configEditors")
        if not isinstance(editors, list):
            return
        target = runtime.get("target") if isinstance(runtime.get("target"), dict) else {}
        target_id = str(target.get("id") or "")
        for editor in editors:
            if not isinstance(editor, dict):
                continue
            path_json = str(editor.get("pathJsonpath") or "").strip()
            path_literal = str(editor.get("pathLiteral") or "").strip()
            has_direct_path_source = bool(path_json or path_literal)
            if has_direct_path_source:
                path_value: str | None = path_literal if path_literal else None
                if path_json:
                    resolved = json_path_get(payload, path_json)
                    if resolved is not None:
                        resolved_text = str(resolved).strip()
                        if resolved_text:
                            path_value = resolved_text
                if path_value is not None:
                    path_var = editor.get("pathVar")
                    self._set_stringvar_if_changed(path_var, path_value or "-")
                    path_identity = self._path_identity(path_value)
                    if path_identity != str(editor.get("lastPathValue") or ""):
                        editor["lastPathValue"] = path_identity
                        editor["needsRefresh"] = True
            self._refresh_config_editor_async(target_id, editor, show_loading=False)

    def _refresh_config_editor_async(
        self,
        target_id: str,
        editor: dict[str, Any],
        *,
        force: bool = False,
        show_loading: bool | None = None,
    ) -> None:
        if force:
            editor["needsRefresh"] = True
        if not bool(editor.get("needsRefresh", False)):
            return
        if bool(editor.get("loading", False)):
            return
        editor["loading"] = True
        editor["needsRefresh"] = False
        if show_loading is None:
            show_loading = not bool(editor.get("hasLoadedOnce", False))
        status_var = editor.get("statusVar")
        if show_loading:
            self._set_stringvar_if_changed(status_var, "Loading...")
        thread = threading.Thread(target=self._load_config_editor_data, args=(target_id, editor), daemon=True)
        thread.start()

    def _load_config_editor_data(self, target_id: str, editor: dict[str, Any]) -> None:
        show_action = str(editor.get("showAction") or "").strip()
        payload, load_error = self._load_config_payload(target_id, show_action)
        if payload is None:
            self.root.after(0, lambda: self._finalize_config_editor_load(editor, [], "", load_error))
            return

        path_value = ""
        path_key = str(editor.get("pathKey") or "").strip()
        if path_key:
            path_entries = payload.get("paths")
            if isinstance(path_entries, list):
                match = next(
                    (
                        item
                        for item in path_entries
                        if isinstance(item, dict) and str(item.get("key") or "").strip() == path_key
                    ),
                    None,
                )
                if isinstance(match, dict):
                    path_value = str(match.get("value") or "").strip()

        entries_raw = payload.get("entries")
        entries = entries_raw if isinstance(entries_raw, list) else []
        filtered = self._filter_config_editor_entries(entries, editor)
        self.root.after(0, lambda: self._finalize_config_editor_load(editor, filtered, path_value, ""))

    def _filter_config_editor_entries(self, entries: list[Any], editor: dict[str, Any]) -> list[dict[str, Any]]:
        path_key = str(editor.get("pathKey") or "").strip()
        include_prefix = str(editor.get("includePrefix") or "").strip()
        include_keys_raw = editor.get("includeKeys")
        include_keys = set(include_keys_raw) if isinstance(include_keys_raw, set) else set()
        exclude_keys_raw = editor.get("excludeKeys")
        exclude_keys = set(exclude_keys_raw) if isinstance(exclude_keys_raw, set) else set()
        settable_only = bool(editor.get("settableOnly", True))

        filtered: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            file_key = str(item.get("fileKey") or "").strip()
            if path_key and not include_prefix and not include_keys and file_key and file_key != path_key:
                continue
            if include_prefix and not key.startswith(include_prefix):
                continue
            if include_keys and key not in include_keys:
                continue
            if exclude_keys and key in exclude_keys:
                continue
            if settable_only and bool(item.get("pathEntry", False)):
                continue
            filtered.append(item)
        return filtered

    def _finalize_config_editor_load(
        self,
        editor: dict[str, Any],
        entries: list[dict[str, Any]],
        path_value: str,
        error_message: str,
    ) -> None:
        editor["loading"] = False
        status_var = editor.get("statusVar")
        if error_message:
            self._set_stringvar_if_changed(status_var, error_message)
            if editor.get("lastEntriesSignature") is None:
                self._render_config_editor_rows(editor, [])
            return

        editor["hasLoadedOnce"] = True
        self._set_stringvar_if_changed(status_var, "Ready.")
        path_var = editor.get("pathVar")
        if isinstance(path_var, tk.StringVar) and path_value:
            self._set_stringvar_if_changed(path_var, path_value)
            editor["lastPathValue"] = self._path_identity(path_value)
        entries_signature = self._config_editor_entries_signature(entries)
        if entries_signature == editor.get("lastEntriesSignature"):
            return
        editor["lastEntriesSignature"] = entries_signature
        self._render_config_editor_rows(editor, entries)

    def _config_editor_entries_signature(self, entries: list[dict[str, Any]]) -> tuple[Any, ...]:
        signature: list[tuple[Any, ...]] = []
        for entry in entries:
            key = str(entry.get("key") or "")
            value = self._config_editor_value_text(entry.get("value"))
            value_type = str(entry.get("valueType") or "")
            constraint = str(entry.get("constraint") or "")
            path_entry = bool(entry.get("pathEntry", False))
            file_key = str(entry.get("fileKey") or "")
            allowed_raw = entry.get("allowed")
            allowed = (
                tuple(self._config_editor_value_text(item) for item in allowed_raw)
                if isinstance(allowed_raw, list)
                else ()
            )
            signature.append((key, value, value_type, constraint, path_entry, file_key, allowed))
        return tuple(signature)

    def _render_config_editor_rows(self, editor: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        rows_frame = editor.get("rowsFrame")
        if not isinstance(rows_frame, ttk.Frame):
            return
        previous_row_state = editor.get("rowState")
        if not isinstance(previous_row_state, dict):
            previous_row_state = {}
        for child in rows_frame.winfo_children():
            child.destroy()

        headers = ["Key", "Current", "New Value", "Validation", "Set"]
        for column, label in enumerate(headers):
            ttk.Label(rows_frame, text=label).grid(row=0, column=column, sticky="w", padx=6, pady=(2, 4))

        if not entries:
            editor["rowState"] = {}
            ttk.Label(rows_frame, text="No matching config entries.").grid(row=1, column=0, sticky="w", padx=6, pady=4)
            return

        target_id = str(editor.get("targetId") or "")
        next_row_state: dict[str, dict[str, Any]] = {}
        for row_index, entry in enumerate(entries, start=1):
            key = str(entry.get("key") or "")
            current_value = entry.get("value")
            current_text = self._config_editor_value_text(current_value)
            is_settable = bool(entry.get("settable", False))

            ttk.Label(rows_frame, text=key).grid(row=row_index, column=0, sticky="w", padx=6, pady=2)
            ttk.Label(rows_frame, text=current_text).grid(row=row_index, column=1, sticky="w", padx=6, pady=2)

            previous_state = previous_row_state.get(key)
            previous_dirty = bool(previous_state.get("dirty", False)) if isinstance(previous_state, dict) else False
            previous_var = previous_state.get("inputVar") if isinstance(previous_state, dict) else None
            previous_text = str(previous_var.get()) if isinstance(previous_var, tk.StringVar) else current_text
            input_text = previous_text if previous_dirty else current_text
            input_var = tk.StringVar(value=input_text)
            row_state = {
                "inputVar": input_var,
                "baseline": current_text,
                "dirty": input_text != current_text,
            }
            next_row_state[key] = row_state

            def on_input_change(*_: Any, entry_key: str = key, value_var: tk.StringVar = input_var) -> None:
                row_states = editor.get("rowState")
                if not isinstance(row_states, dict):
                    return
                state = row_states.get(entry_key)
                if not isinstance(state, dict):
                    return
                baseline = str(state.get("baseline") or "")
                state["dirty"] = str(value_var.get()) != baseline

            input_var.trace_add("write", on_input_change)
            allowed = entry.get("allowed")
            value_type = str(entry.get("valueType") or "")
            if isinstance(allowed, list) and len(allowed) > 0:
                allowed_text = [self._config_editor_value_text(item) for item in allowed]
                input_control = ttk.Combobox(rows_frame, textvariable=input_var, state="readonly", width=24)
                input_control["values"] = allowed_text
                if input_var.get() not in allowed_text and allowed_text:
                    input_var.set(allowed_text[0])
                input_control.grid(row=row_index, column=2, sticky="we", padx=6, pady=2)
            elif value_type == "bool" or isinstance(current_value, bool):
                input_control = ttk.Combobox(rows_frame, textvariable=input_var, state="readonly", width=10)
                input_control["values"] = ["true", "false"]
                if input_var.get().lower() not in {"true", "false"}:
                    input_var.set("false")
                input_control.grid(row=row_index, column=2, sticky="we", padx=6, pady=2)
            else:
                input_control = ttk.Entry(rows_frame, textvariable=input_var, width=32)
                input_control.grid(row=row_index, column=2, sticky="we", padx=6, pady=2)

            if not is_settable:
                input_control.configure(state=tk.DISABLED)

            validation_var = tk.StringVar(value="" if is_settable else "read-only")
            ttk.Label(rows_frame, textvariable=validation_var).grid(row=row_index, column=3, sticky="w", padx=6, pady=2)
            set_button = ttk.Button(
                rows_frame,
                text="Set",
                command=lambda item=entry, var=input_var, state_var=validation_var: self._set_config_editor_entry(
                    target_id,
                    editor,
                    item,
                    var,
                    state_var,
                ),
            )
            if not is_settable:
                set_button.configure(state=tk.DISABLED)
            set_button.grid(row=row_index, column=4, sticky="w", padx=6, pady=2)

        editor["rowState"] = next_row_state
        rows_frame.update_idletasks()
        rows_canvas = editor.get("rowsCanvas")
        if isinstance(rows_canvas, tk.Canvas):
            rows_canvas.configure(scrollregion=rows_canvas.bbox("all"))

    def _set_config_editor_entry(
        self,
        target_id: str,
        editor: dict[str, Any],
        entry: dict[str, Any],
        input_var: tk.StringVar,
        validation_var: tk.StringVar,
    ) -> None:
        key = str(entry.get("key") or "").strip()
        if not key:
            validation_var.set("invalid key")
            return
        raw_value = str(input_var.get() or "")
        parsed, parse_error = self._parse_config_editor_value(entry, raw_value)
        if parse_error:
            validation_var.set(f"invalid: {parse_error}")
            return

        validation_var.set("saving...")
        set_value = self._config_editor_set_arg(parsed)
        set_action = str(editor.get("setAction") or "").strip()

        def run_set() -> None:
            rc, error_text = self._set_config_value(target_id, set_action, key, set_value)
            if rc != 0:
                self.root.after(0, lambda: validation_var.set(error_text))
                return
            self.root.after(0, lambda: validation_var.set("saved"))
            self._mark_target_config_widgets_for_refresh(target_id)
            self.root.after(0, lambda: self._refresh_target_config_widgets(target_id, show_loading=False))
            self._refresh_async()

        threading.Thread(target=run_set, daemon=True).start()

    def _config_editor_value_text(self, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        if isinstance(value, str):
            return value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
        return str(value)

    def _config_editor_set_arg(self, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        return str(value)

    def _parse_config_editor_value(self, entry: dict[str, Any], raw_value: str) -> tuple[Any, str | None]:
        raw = str(raw_value or "").strip()
        allowed = entry.get("allowed")
        if isinstance(allowed, list) and len(allowed) > 0:
            if all(isinstance(item, bool) for item in allowed):
                parsed_bool = self._parse_bool_text(raw)
                if parsed_bool is None:
                    return raw, "must be true|false"
                if parsed_bool not in allowed:
                    return raw, f"must be one of: {allowed}"
                return parsed_bool, None
            for candidate in allowed:
                candidate_text = self._config_editor_value_text(candidate).strip().lower()
                if raw.lower() == candidate_text:
                    return candidate, None
            return raw, f"must be one of: {allowed}"

        current_value = entry.get("value")
        value_type = str(entry.get("valueType") or "").strip().lower()
        if not value_type:
            if isinstance(current_value, bool):
                value_type = "bool"
            elif isinstance(current_value, int) and not isinstance(current_value, bool):
                value_type = "int"
            elif isinstance(current_value, float):
                value_type = "float"
            elif current_value is None:
                value_type = "null"
            else:
                value_type = "string"

        parsed: Any = raw
        if value_type == "bool":
            parsed_bool = self._parse_bool_text(raw)
            if parsed_bool is None:
                return raw, "must be true|false"
            parsed = parsed_bool
        elif value_type == "int":
            try:
                parsed = int(raw)
            except Exception:
                return raw, "must be int"
        elif value_type == "float":
            try:
                parsed = float(raw)
            except Exception:
                return raw, "must be float"
        elif value_type == "null":
            lowered = raw.lower()
            if lowered == "null":
                parsed = None
            else:
                parsed_bool = self._parse_bool_text(raw)
                if parsed_bool is not None:
                    parsed = parsed_bool
                else:
                    try:
                        parsed = int(raw)
                    except Exception:
                        try:
                            parsed = float(raw)
                        except Exception:
                            parsed = raw
        else:
            parsed = raw
            if isinstance(current_value, str) and any(ch in current_value for ch in ("\r", "\n", "\t")):
                parsed = raw.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")

        constraint = str(entry.get("constraint") or "").strip()
        constraint_error = self._config_editor_constraint_error(parsed, constraint)
        if constraint_error:
            return parsed, constraint_error
        return parsed, None

    def _parse_bool_text(self, text: str) -> bool | None:
        lowered = str(text or "").strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
        return None

    def _set_stringvar_if_changed(self, var: Any, value: str) -> None:
        if not isinstance(var, tk.StringVar):
            return
        text = str(value)
        if var.get() == text:
            return
        var.set(text)

    def _path_identity(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        normalized = os.path.normpath(text)
        if os.name == "nt":
            normalized = os.path.normcase(normalized)
        return normalized

    def _mark_target_config_widgets_for_refresh(self, target_id: str) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return
        editors = runtime.get("configEditors")
        if isinstance(editors, list):
            for editor in editors:
                if isinstance(editor, dict):
                    editor["needsRefresh"] = True
        selectors = runtime.get("configFileSelectors")
        if isinstance(selectors, list):
            for selector in selectors:
                if isinstance(selector, dict):
                    selector["needsRefresh"] = True

    def _refresh_target_config_widgets(self, target_id: str, *, show_loading: bool = False) -> None:
        runtime = self.target_runtime.get(target_id)
        if runtime is None:
            return
        selectors = runtime.get("configFileSelectors")
        if isinstance(selectors, list):
            for selector in selectors:
                if isinstance(selector, dict):
                    self._refresh_config_file_selector_async(
                        target_id,
                        selector,
                        force=True,
                        show_loading=show_loading,
                    )
        editors = runtime.get("configEditors")
        if isinstance(editors, list):
            for editor in editors:
                if isinstance(editor, dict):
                    self._refresh_config_editor_async(
                        target_id,
                        editor,
                        force=True,
                        show_loading=show_loading,
                    )

    def _config_editor_constraint_error(self, value: Any, constraint: str) -> str | None:
        text = str(constraint or "").strip()
        if not text:
            return None
        if text.startswith("^"):
            if not isinstance(value, str):
                return f"value type must be string for {text}"
            if re.fullmatch(text, value) is None:
                return f"value does not match {text}"
            return None
        if text == "int 1..65535":
            if not isinstance(value, int):
                return "must be int 1..65535"
            if value < 1 or value > 65535:
                return "must be int 1..65535"
            return None
        return None

    def _open_file_path(self, path_text: str) -> None:
        candidate = str(path_text or "").strip()
        if not candidate or candidate == "-":
            self.console_var.set("No file path available.")
            return
        path = Path(candidate)
        open_path = path
        used_fallback = False
        if not path.exists():
            parent = path.parent
            if parent.exists() and parent != path:
                open_path = parent
                used_fallback = True
            else:
                self.console_var.set(f"path not found: {path}")
                return
        try:
            if os.name == "nt":
                os.startfile(str(open_path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(open_path)])
            if used_fallback:
                self.console_var.set(f"path not found, opened parent: {open_path}")
            else:
                self.console_var.set(f"opened: {open_path}")
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

    def _rows_table_column_value(self, row_payload: Any, column: dict[str, Any]) -> Any | None:
        key = str(column.get("key") or "").strip()
        if key:
            if isinstance(row_payload, dict):
                return row_payload.get(key)
            return None
        jsonpath = str(column.get("jsonpath") or "").strip()
        if jsonpath:
            return json_path_get(row_payload, jsonpath)
        return None

    def _refresh_rows_tables(self, runtime: dict[str, Any], payload: dict[str, Any]) -> None:
        tables = runtime.get("rowsTables")
        if not isinstance(tables, list):
            return
        for table in tables:
            if not isinstance(table, dict):
                continue
            rows_path = str(table.get("rowsPath") or "").strip()
            columns = table.get("columns")
            tree = table.get("tree")
            if not rows_path or not isinstance(columns, list) or not isinstance(tree, ttk.Treeview):
                continue

            rows_raw = json_path_get(payload, rows_path)
            rows = rows_raw if isinstance(rows_raw, list) else []
            max_rows = max(1, int(table.get("maxRows", 200)))
            visible_rows = rows[:max_rows]

            rendered_rows: list[tuple[str, ...]] = []
            for row_payload in visible_rows:
                values: list[str] = []
                for column in columns:
                    if not isinstance(column, dict):
                        values.append("-")
                        continue
                    values.append(render_value(self._rows_table_column_value(row_payload, column)))
                rendered_rows.append(tuple(values))

            signature = tuple(rendered_rows)
            if signature != table.get("lastSignature"):
                table["lastSignature"] = signature
                tree.delete(*tree.get_children())
                for values in rendered_rows:
                    tree.insert("", tk.END, values=values)

            empty_var = table.get("emptyVar")
            if isinstance(empty_var, tk.StringVar):
                if rendered_rows:
                    suffix = ""
                    if len(rows) > len(visible_rows):
                        suffix = f" (showing {len(visible_rows)} of {len(rows)})"
                    empty_var.set(f"{len(rows)} row(s){suffix}")
                else:
                    empty_var.set(str(table.get("emptyText") or "(no rows)"))

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

        control = self._ipc_control_for_runtime(runtime)
        if control is None:
            self._set_status_error(tid, "ipc control is not configured")
            self._render_target_status(tid)
            return

        status = target.get("status")
        status_timeout = 0.0
        if isinstance(status, dict):
            try:
                status_timeout = float(status.get("timeoutSeconds") or 0.0)
            except Exception:
                status_timeout = 0.0
        timeout_seconds = float(
            status_timeout or control.get("timeoutSeconds") or self.default_command_timeout_seconds
        )
        endpoint = str(control.get("endpoint") or "")
        app_id = str(control.get("appId") or "")

        payload: dict[str, Any] | None = None
        error_message = ""
        try:
            rc, response_obj, error_text = _request_ipc_v0(
                endpoint,
                {"method": "status.get", "params": {"appId": app_id}},
                timeout_seconds=timeout_seconds,
            )
            if rc == 0:
                response_payload = response_obj.get("response")
                if isinstance(response_payload, dict):
                    payload = response_payload
                else:
                    error_message = "status.get returned invalid payload"
            else:
                error_message = error_text or f"status.get failed rc={rc}"
        except Exception as ex:
            error_message = str(ex)

        if payload is not None:
            runtime["lastGoodStatus"] = payload
            runtime["lastStatusError"] = None
        else:
            self._set_status_error(tid, error_message or "status.get failed")

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
        action_selectors = list(runtime.get("actionSelectors") or [])
        error_obj = runtime.get("lastStatusError")

        def update() -> None:
            self._refresh_action_catalog_async(target_id, force=False)
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
            for selector in action_selectors:
                if not isinstance(selector, dict):
                    continue
                refresh_fn = selector.get("refreshFn")
                if callable(refresh_fn):
                    try:
                        refresh_fn(payload)
                    except Exception:
                        pass
            banner_var = runtime.get("bannerVar")
            if isinstance(banner_var, tk.StringVar):
                if isinstance(error_obj, dict):
                    ts = str(error_obj.get("ts") or "")
                    msg = str(error_obj.get("message") or "")
                    banner_var.set(f"[{ts}] {msg}")
                else:
                    banner_var.set("")
            self._refresh_rows_tables(runtime, payload)
            self._refresh_file_viewers(runtime, payload)
            self._refresh_config_file_selectors(runtime)
            self._refresh_config_editors(runtime, payload)

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

    def _run_action_ipc(
        self,
        target_id: str,
        action: dict[str, Any],
        action_value: str | None = None,
        action_args: dict[str, str] | None = None,
    ) -> None:
        runtime = self.target_runtime.get(target_id)
        if not isinstance(runtime, dict):
            return
        control = self._ipc_control_for_runtime(runtime)
        if control is None:
            self._append_action_output(target_id, "system", "ipc control is not configured")
            return

        endpoint = str(control.get("endpoint") or "")
        app_id = str(control.get("appId") or "")
        timeout_seconds = float(control.get("timeoutSeconds") or DEFAULT_CONTROL_TIMEOUT_SECONDS)
        job_poll_ms = int(control.get("jobPollMs") or DEFAULT_CONTROL_JOB_POLL_MS)
        job_timeout_seconds = float(control.get("jobTimeoutSeconds") or DEFAULT_CONTROL_JOB_TIMEOUT_SECONDS)

        action_name = str(action.get("name") or "")
        action_label = str(action.get("label") or action_name)
        resolved_args: dict[str, Any] = {}
        if isinstance(action_args, dict):
            for key, value in action_args.items():
                key_text = str(key).strip()
                if key_text:
                    resolved_args[key_text] = str(value)
        if action_value is not None and "value" not in resolved_args:
            resolved_args["value"] = str(action_value)

        self._append_action_output(target_id, "system", f"running {action_label} via ipc")
        self.root.after(0, lambda: self.console_var.set(f"running action: {action_label}"))

        rc, invoke_response, invoke_error = _request_ipc_v0(
            endpoint,
            {
                "method": "action.invoke",
                "params": {
                    "appId": app_id,
                    "actionName": action_name,
                    "args": resolved_args,
                },
            },
            timeout_seconds=timeout_seconds,
        )
        if rc != 0:
            self._append_action_output(target_id, "system", f"{action_label}: invoke failed: {invoke_error}")
            self.root.after(0, lambda: self.console_var.set(f"action failed: {action_label}"))
            return

        invoke_body = invoke_response.get("response")
        job_id = str(invoke_body.get("jobId") or "") if isinstance(invoke_body, dict) else ""
        if not job_id:
            self._append_action_output(target_id, "system", f"{action_label}: invoke returned no job id")
            self.root.after(0, lambda: self.console_var.set(f"action failed: {action_label}"))
            return

        start_time = time.time()
        terminal_states = {"succeeded", "failed", "timeout", "cancelled", "error"}
        while True:
            poll_rc, poll_response, poll_error = _request_ipc_v0(
                endpoint,
                {"method": "action.job.get", "params": {"appId": app_id, "jobId": job_id}},
                timeout_seconds=timeout_seconds,
            )
            if poll_rc != 0:
                self._append_action_output(target_id, "system", f"{action_label}: job poll failed: {poll_error}")
                self.root.after(0, lambda: self.console_var.set(f"action failed: {action_label}"))
                return

            poll_body = poll_response.get("response")
            state = str(poll_body.get("state") or "").strip().lower() if isinstance(poll_body, dict) else ""
            if state in terminal_states:
                stdout_text = str(poll_body.get("stdout") or "") if isinstance(poll_body, dict) else ""
                stderr_text = str(poll_body.get("stderr") or "") if isinstance(poll_body, dict) else ""
                if stdout_text.strip():
                    for line in stdout_text.splitlines():
                        self._append_action_output(target_id, "stdout", line)
                if stderr_text.strip():
                    for line in stderr_text.splitlines():
                        self._append_action_output(target_id, "stderr", line)

                if state == "succeeded":
                    self._append_action_output(target_id, "system", f"{action_label}: finished state={state}")
                    self.root.after(0, lambda: self.console_var.set(f"action done: {action_label}"))
                else:
                    self._append_action_output(target_id, "system", f"{action_label}: finished state={state}")
                    self.root.after(0, lambda: self.console_var.set(f"action failed: {action_label}"))
                return

            if (time.time() - start_time) >= job_timeout_seconds:
                self._append_action_output(
                    target_id,
                    "system",
                    f"{action_label}: job timeout after {job_timeout_seconds:.1f}s",
                )
                self.root.after(0, lambda: self.console_var.set(f"action timeout: {action_label}"))
                return
            time.sleep(max(0.05, float(job_poll_ms) / 1000.0))

    def _run_action(
        self,
        target_id: str,
        action: dict[str, Any],
        action_value: str | None = None,
        action_args: dict[str, str] | None = None,
    ) -> None:
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
            resolved_action_args: dict[str, str] = {}
            if isinstance(action_args, dict):
                for key, value in action_args.items():
                    key_text = str(key).strip()
                    if key_text:
                        resolved_action_args[key_text] = str(value)
            if action_value is not None and "value" not in resolved_action_args:
                resolved_action_args["value"] = str(action_value)
            if resolved_action_args:
                cmd = _apply_action_placeholders(cmd, resolved_action_args)
            if not cmd:
                self._append_action_output(target_id, "system", f"{action_label}: empty command")
                return
            unresolved_cmd = [part for part in cmd if re.search(r"{[A-Za-z0-9_]+}", part)]
            if unresolved_cmd:
                self._append_action_output(
                    target_id,
                    "system",
                    f"{action_label}: unresolved placeholders in command. Provide required arguments.",
                )
                return

            cwd_text = str(action.get("cwd") or "").strip()
            if cwd_text and resolved_action_args:
                cwd_text = _apply_action_placeholders([cwd_text], resolved_action_args)[0]
            if cwd_text and re.search(r"{[A-Za-z0-9_]+}", cwd_text):
                self._append_action_output(
                    target_id,
                    "system",
                    f"{action_label}: unresolved placeholders in cwd. Provide required arguments.",
                )
                return
            cwd = Path(cwd_text) if cwd_text else None
            timeout_seconds = float(action.get("timeoutSeconds") or 120.0)
            detached = bool(action.get("detached", False))

            self._append_action_output(target_id, "system", f"running {action_label}: {' '.join(cmd)}")
            self.root.after(0, lambda: self.console_var.set(f"running action: {action_label}"))

            if detached:
                subprocess.Popen(
                    cmd,
                    cwd=str(cwd) if cwd else None,
                    creationflags=_no_window_creationflags(),
                )
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
                creationflags=_no_window_creationflags(),
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

    def _relaunch_app(self) -> None:
        argv = [sys.executable, *sys.argv]
        self.stop_event.set()
        self.console_var.set("Relaunching monitor...")
        try:
            os.execv(sys.executable, argv)
        except Exception as ex:
            self.stop_event.clear()
            self.console_var.set(f"Relaunch failed: {ex}")
            messagebox.showerror("Relaunch Failed", str(ex))

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
