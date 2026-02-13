#!/usr/bin/env python3
"""Copy canonical monitor target schema into fixture/bridge config folders."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


DEFAULT_FIXTURE_REPO = Path(r"\\H3FT06-40318\c\40318-SOFT")
DEFAULT_BRIDGE_REPO = Path(r"C:\repos\test-fixture-data-bridge")
SCHEMA_NAME = "monitor.target.v2.schema.json"


def _target_schema_path(repo_root: Path) -> Path:
    return (repo_root / "config" / "gui" / SCHEMA_NAME).resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-repo", default=str(DEFAULT_FIXTURE_REPO))
    parser.add_argument("--bridge-repo", default=str(DEFAULT_BRIDGE_REPO))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    src_schema = (repo_root / "schemas" / SCHEMA_NAME).resolve()
    if not src_schema.exists():
        raise SystemExit(f"missing canonical schema: {src_schema}")

    destinations = [
        _target_schema_path(Path(args.fixture_repo)),
        _target_schema_path(Path(args.bridge_repo)),
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
