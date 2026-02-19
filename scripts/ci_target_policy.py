#!/usr/bin/env python3
"""Run strict monitor target policy checks for fixture/bridge targets."""

from __future__ import annotations

import argparse
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
        help="Include fixture target(s) in strict policy check.",
    )
    parser.add_argument(
        "--include-bridge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include bridge target in strict policy check.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    checker = (repo_root / "scripts" / "check_target_contract.py").resolve()
    if not checker.exists():
        print(f"missing checker: {checker}", file=sys.stderr)
        return 2

    targets: list[Path] = []
    if args.include_fixture:
        if args.fixture_target.strip():
            targets.append(Path(args.fixture_target).resolve())
        elif args.fixture_repo.strip():
            fixture_targets = resolve_fixture_target_files(Path(args.fixture_repo))
            if fixture_targets:
                targets.extend(fixture_targets)
            else:
                targets.append(resolve_target_file(Path(args.fixture_repo), "monitor.fixture.target.json"))
        else:
            print(
                "fixture target requested but --fixture-repo/--fixture-target was not provided "
                "(or FIXTURE_REPO/FIXTURE_TARGET env vars are empty).",
                file=sys.stderr,
            )
            return 2

    if args.include_bridge:
        if args.bridge_target.strip():
            targets.append(Path(args.bridge_target).resolve())
        elif args.bridge_repo.strip():
            targets.append(resolve_target_file(Path(args.bridge_repo), "monitor.bridge.target.json"))
        else:
            print(
                "bridge target requested but --bridge-repo/--bridge-target was not provided "
                "(or BRIDGE_REPO/BRIDGE_TARGET env vars are empty).",
                file=sys.stderr,
            )
            return 2

    if not targets:
        print("No targets selected. Use --include-fixture and/or --include-bridge.", file=sys.stderr)
        return 2

    missing = [str(path) for path in targets if not path.exists()]
    if missing:
        for item in missing:
            print(f"missing target config: {item}", file=sys.stderr)
        return 2

    failures = 0
    for target in targets:
        cmd = [sys.executable, str(checker), "--target", str(target), "--enforce-top-tabs"]
        completed = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
        if completed.stdout.strip():
            print(completed.stdout.strip())
        if completed.stderr.strip():
            print(completed.stderr.strip(), file=sys.stderr)
        if completed.returncode != 0:
            failures += 1

    if failures:
        print(f"FAIL: strict policy check failed for {failures}/{len(targets)} target(s).", file=sys.stderr)
        return 2

    print(f"OK: strict policy check passed for {len(targets)} target(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
