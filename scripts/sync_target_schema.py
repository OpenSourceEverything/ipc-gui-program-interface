#!/usr/bin/env python3
"""Copy canonical monitor target schema into selected app config folders."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


SCHEMA_NAME = "monitor.target.v2.schema.json"


def _target_schema_path(repo_root: Path) -> Path:
    return (repo_root / "config" / "gui" / SCHEMA_NAME).resolve()


def _canonical_schema_path(repo_root: Path) -> Path:
    preferred = (repo_root / "contract" / "schemas" / SCHEMA_NAME).resolve()
    if preferred.exists():
        return preferred
    return (repo_root / "schemas" / SCHEMA_NAME).resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Generic app repo root to receive schema copy. May be repeated.",
    )
    parser.add_argument("--fixture-repo", default=os.getenv("FIXTURE_REPO", "").strip())
    parser.add_argument("--bridge-repo", default=os.getenv("BRIDGE_REPO", "").strip())
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    src_schema = _canonical_schema_path(repo_root)
    if not src_schema.exists():
        raise SystemExit(f"missing canonical schema: {src_schema}")

    repo_roots: list[Path] = []
    generic_repo_values = args.repo if isinstance(args.repo, list) else []
    for item in generic_repo_values:
        text = str(item or "").strip()
        if text:
            repo_roots.append(Path(text).resolve())

    fixture_repo = str(args.fixture_repo or "").strip()
    bridge_repo = str(args.bridge_repo or "").strip()
    if fixture_repo:
        repo_roots.append(Path(fixture_repo).resolve())
    if bridge_repo:
        repo_roots.append(Path(bridge_repo).resolve())

    if not repo_roots:
        raise SystemExit(
            "No repositories provided. Use --repo (recommended) or "
            "--fixture-repo/--bridge-repo (legacy compatibility)."
        )

    if not generic_repo_values:
        if not fixture_repo:
            raise SystemExit("fixture repo not provided (--fixture-repo or FIXTURE_REPO).")
        if not bridge_repo:
            raise SystemExit("bridge repo not provided (--bridge-repo or BRIDGE_REPO).")

    deduped_roots: list[Path] = []
    seen_roots: set[str] = set()
    for repo in repo_roots:
        key = str(repo)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        deduped_roots.append(repo)

    destinations = [_target_schema_path(repo) for repo in deduped_roots]

    for dst in destinations:
        print(f"{src_schema} -> {dst}")
        if args.dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_schema, dst)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
