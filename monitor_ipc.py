"""IPC and JSONPath helpers shared by monitor runtime and tests."""

from __future__ import annotations

import json
import socket
from typing import Any
from urllib.parse import urlparse


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


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    text = str(endpoint or "").strip()
    if not text:
        raise ValueError("endpoint is empty")

    if "://" in text:
        parsed = urlparse(text)
        host = str(parsed.hostname or "").strip()
        raw_port = parsed.port
        if not host:
            raise ValueError("endpoint host is empty")
        if raw_port is None:
            raise ValueError("endpoint must include port")
        port = int(raw_port)
    else:
        if ":" not in text:
            raise ValueError("endpoint must be host:port")
        host, raw_port = text.rsplit(":", 1)
        host = host.strip() or "127.0.0.1"
        port = int(raw_port.strip())

    if port <= 0 or port > 65535:
        raise ValueError("endpoint port is out of range")
    return host, port


def _request_ipc_v0(
    endpoint: str,
    request: dict[str, Any],
    *,
    timeout_seconds: float,
) -> tuple[int, dict[str, Any], str]:
    try:
        host, port = _parse_endpoint(endpoint)
        payload = request if isinstance(request, dict) else {}
        with socket.create_connection((host, port), timeout=max(0.1, float(timeout_seconds))) as sock:
            sock.settimeout(max(0.1, float(timeout_seconds)))
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            response_line = sock.makefile("r", encoding="utf-8", newline="\n").readline().strip()
        if not response_line:
            return 2, {}, "ipc response is empty"
        response_obj = json.loads(response_line)
        if not isinstance(response_obj, dict):
            return 2, {}, "ipc response is not an object"
        if bool(response_obj.get("ok", False)):
            return 0, response_obj, ""
        error_obj = response_obj.get("error")
        if isinstance(error_obj, dict):
            return 2, response_obj, str(error_obj.get("message") or "ipc request failed")
        return 2, response_obj, "ipc request failed"
    except Exception as ex:
        return 2, {}, f"ipc request failed: {ex}"


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
