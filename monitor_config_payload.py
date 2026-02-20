"""Config show payload normalization helpers."""

from __future__ import annotations

from typing import Any


def _normalize_config_paths_payload(paths_raw: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if isinstance(paths_raw, list):
        for item in paths_raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            value = item.get("value")
            if value is None and "path" in item:
                value = item.get("path")
            normalized.append({"key": key, "value": value})
        return normalized

    if isinstance(paths_raw, dict):
        for key_raw, value in paths_raw.items():
            key = str(key_raw or "").strip()
            if not key:
                continue
            normalized.append({"key": key, "value": value})
    return normalized


def _normalize_config_entries_payload(entries_raw: Any, paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(entries_raw, list):
        return []

    path_values: dict[str, Any] = {}
    for item in paths:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        path_values[key] = item.get("value")

    normalized: list[dict[str, Any]] = []
    for item in entries_raw:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        allowed = entry.get("allowed")
        if not isinstance(allowed, list):
            legacy_allowed = entry.get("allowedValues")
            if isinstance(legacy_allowed, list):
                entry["allowed"] = list(legacy_allowed)
        if key in path_values:
            if not str(entry.get("path") or "").strip():
                entry["path"] = path_values[key]
            if "pathEntry" not in entry:
                entry["pathEntry"] = True
        normalized.append(entry)
    return normalized


def _normalize_config_show_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    paths = _normalize_config_paths_payload(normalized.get("paths"))
    normalized["paths"] = paths
    normalized["entries"] = _normalize_config_entries_payload(normalized.get("entries"), paths)
    return normalized
