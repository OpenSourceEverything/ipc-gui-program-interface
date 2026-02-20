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


def resolve_repo_target_files(repo: Path) -> list[Path]:
    base = (repo / "config" / "gui").resolve()
    if not base.exists():
        return []
    return sorted(
        (candidate.resolve() for candidate in base.glob("monitor.*.target.json") if candidate.is_file()),
        key=lambda item: str(item).lower(),
    )


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


def dedupe_paths(items: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for item in items:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def collect_generic_targets(args: argparse.Namespace) -> list[Path]:
    targets: list[Path] = []

    target_values = args.target if isinstance(args.target, list) else []
    for target_text in target_values:
        text = str(target_text or "").strip()
        if not text:
            continue
        targets.append(Path(text).resolve())

    repo_values = args.repo if isinstance(args.repo, list) else []
    for repo_text in repo_values:
        text = str(repo_text or "").strip()
        if not text:
            continue
        repo = Path(text).resolve()
        discovered = resolve_repo_target_files(repo)
        if not discovered:
            raise RuntimeError(
                f"no target files found in repo: {repo} "
                "(expected config/gui/monitor.*.target.json)."
            )
        targets.extend(discovered)

    return dedupe_paths(targets)


def collect_legacy_targets(args: argparse.Namespace) -> list[Path]:
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
            raise RuntimeError(
                "fixture target requested but --fixture-repo/--fixture-target was not provided "
                "(or FIXTURE_REPO/FIXTURE_TARGET env vars are empty)."
            )

    if args.include_bridge:
        if args.bridge_target.strip():
            bridge_target = Path(args.bridge_target).resolve()
        elif args.bridge_repo.strip():
            bridge_target = resolve_target_file(Path(args.bridge_repo), "monitor.bridge.target.json")
        else:
            raise RuntimeError(
                "bridge target requested but --bridge-repo/--bridge-target was not provided "
                "(or BRIDGE_REPO/BRIDGE_TARGET env vars are empty)."
            )
        include_files.append(bridge_target)

    include_files = dedupe_paths(include_files)
    if not include_files:
        raise RuntimeError("No targets selected. Use --include-fixture and/or --include-bridge.")
    return include_files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Generic app repo root; includes config/gui/monitor.*.target.json. May be repeated.",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Explicit target file path. May be repeated.",
    )
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

    try:
        generic_targets = collect_generic_targets(args)
        if generic_targets:
            include_files = generic_targets
        else:
            include_files = collect_legacy_targets(args)
    except RuntimeError as ex:
        print(str(ex), file=sys.stderr)
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
