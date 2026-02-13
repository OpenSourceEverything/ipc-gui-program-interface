# IPC Monitor GUI

JSON-driven Tkinter monitor for fixture/bridge targets.

## Quick Start

```bash
python scripts/launch_monitor.py
```

This generates a root config from repo-local target files and launches the GUI.

Runtime sidecars are service-owned by fixture/bridge repos; GUI remains a blind hook-in.

## Run

```bash
python monitor.py --config monitor_config.json
```

## Validate Config

```bash
python monitor.py --config monitor_config.json --validate-config
```

## CLI Schema

`schemas/cli.schema.json` defines the normalized command-catalog contract used by the
infrastructure overhaul plan.

Monitor schemas:

- `schemas/monitor.root.schema.json`
- `schemas/monitor.target.v2.schema.json`

## Launcher Script

```bash
python scripts/launch_monitor.py --no-launch --print-config
python scripts/sync_target_schema.py
python scripts/launch_monitor.py --fixture-repo \\H3FT06-40318\c\40318-SOFT --bridge-repo C:\repos\test-fixture-data-bridge
```

Key flags:

- `--fixture-repo` / `--bridge-repo`
- `--fixture-target` / `--bridge-target` (explicit file overrides)
- `--config-out`
- `--validate-only`
- `--no-launch`

Paired runtime note:
- Bridge target actions include:
  - `Start Pair Run (detached)` (indefinite until manually stopped)
  - `Start Pair Overnight (detached)` (8h)

## Deploy Into App Repos (Manual Copy)

```bash
python scripts/deploy_to_repos.py --clean
```

This copies GUI runtime files into:
- `\\H3FT06-40318\c\40318-SOFT\tools\ipc-gui-program-interface`
- `C:\repos\test-fixture-data-bridge\tools\ipc-gui-program-interface`

and writes one launcher per repo:
- `\\H3FT06-40318\c\40318-SOFT\scripts\gui_monitor.py`
- `C:\repos\test-fixture-data-bridge\scripts\gui_monitor.py`

Run in either repo:
- `python scripts/gui_monitor.py`

## Config Versions

- `configVersion: 2` = new explicit target model (preferred for new files).
- Missing `configVersion` = assumed `v1` (back-compat only).
- No heuristic mixing: v2 is only parsed when `configVersion` is explicitly `2`.

## Root Config

`monitor_config.json` contains global settings and include file paths:

- `refreshSeconds`
- `commandTimeoutSeconds`
- `actionOutput.maxLines` / `actionOutput.maxBytes`
- `includeFiles[]`

## Include File Formats

### v1 (legacy)

Legacy files like:

- `target` / `targets[]` with `statusCommand`, `fields`, `commands`
- `logPanels[]`

These are normalized internally into the v2 runtime model.

### v2 (preferred)

Each include file should declare `configVersion: 2` and define one or more targets.
Target files should also include:

- `"$schema": "./monitor.target.v2.schema.json"`
- adjacent copy of `monitor.target.v2.schema.json` in the same folder

Target shape:

- `id`, `title`
- `refreshSeconds` (optional override)
- `status`:
  - `cwd`
  - `cmd[]`
  - `timeoutSeconds` (optional)
- `logs[]`:
  - `stream`, `title`, `glob`, `tailLines`
  - `maxLineBytes`, `pollMs`, `encoding`, `allowMissing`
- `actions[]`:
  - `name`, `label`, `cwd`, `cmd[]`
  - `timeoutSeconds`, `confirm`, `showOutputPanel`, `mutex`, `detached`
- `ui.tabs[].widgets[]`
  - widget types: `kv`, `table`, `log`, `button`
- `actionOutput.maxLines`, `actionOutput.maxBytes` (optional)

## Runtime Semantics

### Status cache

Per target, monitor keeps:

- `last_good_status` (JSON object)
- `last_status_error` (`message + timestamp`)

UI always renders from `last_good_status` and overlays the error banner.

### Log file selection and rotation

For each log `glob`, latest file is selected by:

1. newest `mtime`
2. if equal, lexicographically newest file name/path

Tail workers run in background threads and handle file switch/truncate.

### Action output capture

Action execution captures both streams:

- `[stdout] ...`
- `[stderr] ...`

Output is bounded by `maxLines` and `maxBytes` to prevent memory growth.

### JSONPath subset

Supported subset:

- `$.key`
- `$.a.b.c`
- `$.arr[0].k`

Missing paths return `None`, rendered as `-`.
