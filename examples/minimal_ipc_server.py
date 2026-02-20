#!/usr/bin/env python3
"""Minimal IPC v0 sample server for monitor integration testing."""

from __future__ import annotations

import argparse
import json
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def _json_ok(response: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "response": response}


def _json_error(message: str, *, code: str = "request_failed") -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


@dataclass
class DemoState:
    app_id: str = "sample-app"
    app_title: str = "Sample App"
    profile: str = "sim"
    mode: str = "sim"
    running: bool = True
    boot_id: str = field(default_factory=lambda: f"boot-{uuid.uuid4().hex[:8]}")
    last_action: str = "-"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _jobs: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "interfaceName": "generic-process-interface",
                "interfaceVersion": 1,
                "appId": self.app_id,
                "appTitle": self.app_title,
                "bootId": self.boot_id,
                "running": self.running,
                "pid": None,
                "hostRunning": True,
                "hostPid": None,
                "profile": self.profile,
                "mode": self.mode,
                "lastAction": self.last_action,
                "timestampUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

    def config_payload(self) -> dict[str, Any]:
        with self._lock:
            profile_path = f"C:/demo/config/profiles/{self.profile}.json"
            return {
                "paths": [{"key": "profilePath", "value": profile_path}],
                "entries": [
                    {
                        "key": "profile",
                        "value": self.profile,
                        "settable": True,
                        "allowed": ["sim", "lab", "prod"],
                        "path": profile_path,
                    },
                    {
                        "key": "mode",
                        "value": self.mode,
                        "settable": True,
                        "allowed": ["sim", "live"],
                    },
                ],
            }

    def action_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "ping",
                "label": "Ping",
                "args": [
                    {
                        "name": "value",
                        "label": "Message",
                        "required": False,
                        "type": "string",
                        "placeholder": "hello",
                    }
                ],
            },
            {
                "name": "toggle_running",
                "label": "Toggle Running",
                "args": [],
            },
        ]

    def set_config_value(self, key: str, value: str) -> None:
        with self._lock:
            if key == "profile":
                if value not in {"sim", "lab", "prod"}:
                    raise ValueError("profile must be one of sim|lab|prod")
                self.profile = value
            elif key == "mode":
                if value not in {"sim", "live"}:
                    raise ValueError("mode must be one of sim|live")
                self.mode = value
            else:
                raise ValueError(f"unknown key: {key}")
            self.last_action = f"config.set {key}={value}"

    def invoke_action(self, action_name: str, args: dict[str, Any]) -> str:
        with self._lock:
            if action_name == "ping":
                message = str(args.get("value") or "hello")
                stdout_text = f"pong: {message}"
            elif action_name == "toggle_running":
                self.running = not self.running
                stdout_text = f"running={self.running}"
            else:
                raise ValueError(f"unknown action: {action_name}")

            self.last_action = action_name
            job_id = f"job-{uuid.uuid4().hex[:12]}"
            self._jobs[job_id] = {
                "jobId": job_id,
                "state": "succeeded",
                "stdout": stdout_text,
                "stderr": "",
                "finishedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            return job_id

    def job_status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self._jobs.get(job_id)
            if payload is None:
                raise ValueError(f"unknown jobId: {job_id}")
            return dict(payload)


STATE = DemoState()


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    method = str(request.get("method") or "").strip()
    params = request.get("params")
    params_obj = params if isinstance(params, dict) else {}

    if method == "status.get":
        return _json_ok(STATE.status_payload())

    if method == "action.list":
        return _json_ok({"actions": STATE.action_catalog()})

    if method == "action.invoke":
        action_name = str(params_obj.get("actionName") or "").strip()
        if not action_name:
            return _json_error("actionName is required", code="invalid_params")
        action_args = params_obj.get("args")
        action_args_obj = action_args if isinstance(action_args, dict) else {}
        try:
            job_id = STATE.invoke_action(action_name, action_args_obj)
        except ValueError as ex:
            return _json_error(str(ex), code="invalid_action")
        return _json_ok({"jobId": job_id})

    if method == "action.job.get":
        job_id = str(params_obj.get("jobId") or "").strip()
        if not job_id:
            return _json_error("jobId is required", code="invalid_params")
        try:
            payload = STATE.job_status(job_id)
        except ValueError as ex:
            return _json_error(str(ex), code="invalid_job")
        return _json_ok(payload)

    if method == "config.get":
        return _json_ok(STATE.config_payload())

    if method == "config.set":
        key = str(params_obj.get("key") or "").strip()
        value = str(params_obj.get("value") or "").strip()
        if not key:
            return _json_error("key is required", code="invalid_params")
        try:
            STATE.set_config_value(key, value)
        except ValueError as ex:
            return _json_error(str(ex), code="invalid_config")
        return _json_ok({"updated": True})

    return _json_error(f"unsupported method: {method}", code="unsupported_method")


class JsonLineHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        try:
            request = json.loads(line.decode("utf-8", errors="ignore"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
        except Exception as ex:
            response = _json_error(f"invalid JSON request: {ex}", code="invalid_json")
            self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            self.wfile.flush()
            return

        response = handle_request(request)
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


class ThreadedTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    host = str(args.host or "127.0.0.1").strip() or "127.0.0.1"
    port = int(args.port)
    if port <= 0 or port > 65535:
        raise SystemExit("port must be in range 1..65535")

    with ThreadedTcpServer((host, port), JsonLineHandler) as server:
        print(f"minimal IPC server listening on {host}:{port} (appId={STATE.app_id})")
        print("Press Ctrl+C to stop.")
        try:
            server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            print("\nStopping server...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
