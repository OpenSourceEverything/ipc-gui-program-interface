#!/usr/bin/env python3
"""Generate root monitor config from repo paths, then launch monitor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def resolve_target_file(repo: Path, expected_name: str) -> Path:
    return (repo / "config" / "gui" / expected_name).resolve()


def resolve_fixture_target_files(repo: Path) -> list[Path]:
    base = repo / "config" / "gui"
    names = [
        "monitor.fixture.target.json",
        "monitor.plc-simulator.target.json",
        "monitor.ble-simulator.target.json",
    ]
    result: list[Path] = []
    for name in names:
        candidate = (base / name).resolve()
        if candidate.exists():
            result.append(candidate)
    return result


def build_root_config(include_files: list[Path], refresh: float, timeout: float) -> dict:
    return {
        "refreshSeconds": refresh,
        "commandTimeoutSeconds": timeout,
        "includeFiles": [str(path) for path in include_files],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-repo", default=os.getenv("FIXTURE_REPO", "").strip())
    parser.add_argument("--bridge-repo", default=os.getenv("BRIDGE_REPO", "").strip())
    parser.add_argument("--fixture-target", default=os.getenv("FIXTURE_TARGET", "").strip())
    parser.add_argument("--bridge-target", default=os.getenv("BRIDGE_TARGET", "").strip())
    parser.add_argument(
        "--include-fixture",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include fixture target in generated monitor config.",
    )
    parser.add_argument(
        "--include-bridge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include bridge target in generated monitor config.",
    )
    parser.add_argument("--config-out", default="monitor_config.generated.json")
    parser.add_argument("--refresh-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    include_files: list[Path] = []
    if args.include_fixture:
        if args.fixture_target.strip():
            fixture_target = Path(args.fixture_target).resolve()
            include_files.append(fixture_target)
        elif args.fixture_repo.strip():
            fixture_targets = resolve_fixture_target_files(Path(args.fixture_repo))
            if not fixture_targets:
                fixture_target = resolve_target_file(Path(args.fixture_repo), "monitor.fixture.target.json")
                include_files.append(fixture_target)
            else:
                include_files.extend(fixture_targets)
        else:
            print(
                "fixture target requested but --fixture-repo/--fixture-target was not provided "
                "(or FIXTURE_REPO/FIXTURE_TARGET env vars are empty).",
                file=sys.stderr,
            )
            return 2
    if args.include_bridge:
        if args.bridge_target.strip():
            bridge_target = Path(args.bridge_target).resolve()
        elif args.bridge_repo.strip():
            bridge_target = resolve_target_file(Path(args.bridge_repo), "monitor.bridge.target.json")
        else:
            print(
                "bridge target requested but --bridge-repo/--bridge-target was not provided "
                "(or BRIDGE_REPO/BRIDGE_TARGET env vars are empty).",
                file=sys.stderr,
            )
            return 2
        include_files.append(bridge_target)
    if not include_files:
        print("No targets selected. Use --include-fixture and/or --include-bridge.", file=sys.stderr)
        return 2

    missing = [str(path) for path in include_files if not path.exists()]
    if missing:
        for item in missing:
            print(f"missing target config: {item}", file=sys.stderr)
        return 2

    config_out = Path(args.config_out)
    if not config_out.is_absolute():
        config_out = (repo_root / config_out).resolve()
    config_out.parent.mkdir(parents=True, exist_ok=True)

    payload = build_root_config(
        include_files=include_files,
        refresh=args.refresh_seconds,
        timeout=args.timeout_seconds,
    )
    config_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"wrote config: {config_out}")
    if args.print_config:
        print(json.dumps(payload, indent=2))

    if args.no_launch:
        return 0

    monitor_py = repo_root / "monitor.py"
    cmd = [sys.executable, str(monitor_py), "--config", str(config_out)]
    if args.validate_only:
        cmd.append("--validate-config")
    return subprocess.call(cmd, cwd=repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
