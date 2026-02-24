#!/usr/bin/env python3
"""Run strict monitor target policy checks for selected targets."""

from __future__ import annotations

import argparse
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
            raise RuntimeError(
                "fixture target requested but --fixture-repo/--fixture-target was not provided "
                "(or FIXTURE_REPO/FIXTURE_TARGET env vars are empty)."
            )

    if args.include_bridge:
        if args.bridge_target.strip():
            targets.append(Path(args.bridge_target).resolve())
        elif args.bridge_repo.strip():
            targets.append(resolve_target_file(Path(args.bridge_repo), "monitor.bridge.target.json"))
        else:
            raise RuntimeError(
                "bridge target requested but --bridge-repo/--bridge-target was not provided "
                "(or BRIDGE_REPO/BRIDGE_TARGET env vars are empty)."
            )

    targets = dedupe_paths(targets)
    if not targets:
        raise RuntimeError("No targets selected. Use --include-fixture and/or --include-bridge.")
    return targets


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

    try:
        generic_targets = collect_generic_targets(args)
        if generic_targets:
            targets = generic_targets
        else:
            targets = collect_legacy_targets(args)
    except RuntimeError as ex:
        print(str(ex), file=sys.stderr)
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
