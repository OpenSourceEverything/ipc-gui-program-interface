#!/usr/bin/env python3
"""Copy GUI runtime bundle into fixture/bridge repos and generate launchers."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


EMBED_RELATIVE = Path("tools") / "ipc-gui-program-interface"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-repo", default=os.getenv("FIXTURE_REPO", "").strip())
    parser.add_argument("--bridge-repo", default=os.getenv("BRIDGE_REPO", "").strip())
    parser.add_argument("--clean", action="store_true", help="Remove existing embed folder before copy.")
    parser.add_argument(
        "--targets",
        nargs="+",
        choices=["fixture", "bridge"],
        default=["fixture", "bridge"],
        help="Deploy target repos.",
    )
    return parser.parse_args()


def ignore_filter(_: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in {".git", ".pytest_cache", "__pycache__", ".mypy_cache"}:
            ignored.add(name)
            continue
        if name.endswith((".pyc", ".pyo", ".log")):
            ignored.add(name)
            continue
        if name in {"monitor_config.generated.json"}:
            ignored.add(name)
            continue
    return ignored


def copy_item(source_root: Path, relative: str, destination_root: Path) -> None:
    source = source_root / relative
    dest = destination_root / relative
    if not source.exists():
        raise RuntimeError(f"Missing source item: {source}")
    if source.is_dir():
        shutil.copytree(source, dest, dirs_exist_ok=True, ignore=ignore_filter)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)


def launcher_text_for_fixture() -> str:
    return """#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

def _resolve_canonical_repo(repo_root: Path, explicit: str) -> Path:
    if explicit.strip():
        return Path(explicit).resolve()
    env_value = os.getenv("IPC_GUI_CANONICAL_REPO", "").strip()
    if env_value:
        return Path(env_value).resolve()
    config_path = repo_root / "config" / "gui" / "monitor.canonical.json"
    if config_path.exists():
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            configured = str(payload.get("canonicalRepo") or "").strip()
            if configured:
                return Path(configured).resolve()
    raise RuntimeError(
        "Canonical GUI repo not set. Use --canonical-repo, IPC_GUI_CANONICAL_REPO, "
        "or config/gui/monitor.canonical.json."
    )

def _parse_known_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--canonical-repo", default="")
    return parser.parse_known_args(argv)

def main() -> int:
    known, passthrough = _parse_known_args(list(sys.argv[1:]))
    repo_root = Path(__file__).resolve().parents[1]
    gui_root = _resolve_canonical_repo(repo_root, str(known.canonical_repo or ""))
    launcher = gui_root / "scripts" / "launch_monitor.py"
    if not launcher.exists():
        print(f"missing launcher: {launcher}", file=sys.stderr)
        return 2

    argv = passthrough
    include_bridge = False
    if "--with-bridge" in argv:
        include_bridge = True
        argv.remove("--with-bridge")

    cmd = [
        sys.executable,
        str(launcher),
        "--fixture-repo",
        str(repo_root),
        "--no-include-bridge",
    ]
    if include_bridge:
        bridge_repo = os.getenv("BRIDGE_REPO", "").strip()
        cmd.append("--include-bridge")
        if bridge_repo and "--bridge-repo" not in argv and "--bridge-target" not in argv:
            cmd.extend(["--bridge-repo", bridge_repo])
    cmd.extend(argv)
    return subprocess.call(cmd, cwd=gui_root)

if __name__ == "__main__":
    raise SystemExit(main())
"""


def launcher_text_for_bridge() -> str:
    return """#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

def _resolve_canonical_repo(repo_root: Path, explicit: str) -> Path:
    if explicit.strip():
        return Path(explicit).resolve()
    env_value = os.getenv("IPC_GUI_CANONICAL_REPO", "").strip()
    if env_value:
        return Path(env_value).resolve()
    config_path = repo_root / "config" / "gui" / "monitor.canonical.json"
    if config_path.exists():
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            configured = str(payload.get("canonicalRepo") or "").strip()
            if configured:
                return Path(configured).resolve()
    raise RuntimeError(
        "Canonical GUI repo not set. Use --canonical-repo, IPC_GUI_CANONICAL_REPO, "
        "or config/gui/monitor.canonical.json."
    )

def _parse_known_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--canonical-repo", default="")
    return parser.parse_known_args(argv)

def main() -> int:
    known, passthrough = _parse_known_args(list(sys.argv[1:]))
    repo_root = Path(__file__).resolve().parents[1]
    gui_root = _resolve_canonical_repo(repo_root, str(known.canonical_repo or ""))
    launcher = gui_root / "scripts" / "launch_monitor.py"
    if not launcher.exists():
        print(f"missing launcher: {launcher}", file=sys.stderr)
        return 2

    argv = passthrough
    include_fixture = False
    if "--with-fixture" in argv:
        include_fixture = True
        argv.remove("--with-fixture")

    cmd = [
        sys.executable,
        str(launcher),
        "--bridge-repo",
        str(repo_root),
        "--no-include-fixture",
    ]
    if include_fixture:
        fixture_repo = os.getenv("FIXTURE_REPO", "").strip()
        cmd.append("--include-fixture")
        if fixture_repo and "--fixture-repo" not in argv and "--fixture-target" not in argv:
            cmd.extend(["--fixture-repo", fixture_repo])
    cmd.extend(argv)
    return subprocess.call(cmd, cwd=gui_root)

if __name__ == "__main__":
    raise SystemExit(main())
"""


def write_launcher(repo_root: Path, role: str) -> Path:
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    launcher = scripts_dir / "gui_monitor.py"
    content = launcher_text_for_fixture() if role == "fixture" else launcher_text_for_bridge()
    launcher.write_text(content, encoding="utf-8")
    return launcher


def deploy_to_repo(source_root: Path, repo_root: Path, role: str, clean: bool) -> None:
    embed_root = repo_root / EMBED_RELATIVE
    if clean and embed_root.exists():
        shutil.rmtree(embed_root)
    embed_root.mkdir(parents=True, exist_ok=True)

    runtime_items = [
        ".gitignore",
        "monitor.py",
        "README.md",
        "cli.schema.json",
        "summary.md",
        "monitor.target.v2.example.json",
        "schemas",
        "scripts/launch_monitor.py",
        "scripts/sync_target_schema.py",
    ]
    for item in runtime_items:
        copy_item(source_root, item, embed_root)

    launcher = write_launcher(repo_root, role)
    print(f"[{role}] copied_gui={embed_root}")
    print(f"[{role}] launcher={launcher}")


def main() -> int:
    args = parse_args()
    source_root = Path(__file__).resolve().parents[1]
    fixture_repo_text = str(args.fixture_repo or "").strip()
    bridge_repo_text = str(args.bridge_repo or "").strip()
    if not fixture_repo_text:
        raise RuntimeError("fixture repo not provided (--fixture-repo or FIXTURE_REPO).")
    if not bridge_repo_text:
        raise RuntimeError("bridge repo not provided (--bridge-repo or BRIDGE_REPO).")
    fixture_repo = Path(fixture_repo_text).resolve()
    bridge_repo = Path(bridge_repo_text).resolve()

    target_map = {
        "fixture": fixture_repo,
        "bridge": bridge_repo,
    }

    for role in args.targets:
        repo_root = target_map[role]
        if not repo_root.exists():
            raise RuntimeError(f"{role} repo not found: {repo_root}")
        deploy_to_repo(source_root, repo_root, role, args.clean)

    print("DONE: gui runtime deployed")
    print("launch fixture: python scripts/gui_monitor.py")
    print("launch bridge:  python scripts/gui_monitor.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
