#!/usr/bin/env python3
"""Copy canonical monitor target schema into fixture/bridge config folders."""

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
    fixture_repo = str(args.fixture_repo or "").strip()
    bridge_repo = str(args.bridge_repo or "").strip()
    if not fixture_repo:
        raise SystemExit("fixture repo not provided (--fixture-repo or FIXTURE_REPO).")
    if not bridge_repo:
        raise SystemExit("bridge repo not provided (--bridge-repo or BRIDGE_REPO).")

    destinations = [
        _target_schema_path(Path(fixture_repo)),
        _target_schema_path(Path(bridge_repo)),
    ]

    for dst in destinations:
        print(f"{src_schema} -> {dst}")
        if args.dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_schema, dst)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
