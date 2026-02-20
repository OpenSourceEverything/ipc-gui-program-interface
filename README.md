# IPC Monitor GUI

JSON-driven Tkinter monitor for generic targets (fixture/bridge compatible).

## Quick Start

```bash
python scripts/launch_monitor.py
```

This generates a root config from repo-local target files and launches the GUI.

Runtime sidecars are service-owned by fixture/bridge repos; GUI remains a blind hook-in.

Generic quick start:

```bash
python scripts/launch_monitor.py --repo C:\repos\my-app
python scripts/launch_monitor.py --target C:\repos\my-app\config\gui\monitor.my-app.target.json
python monitor.py --config examples/monitor_config.example.json
python examples/minimal_ipc_server.py
```

See `examples/README.md` for the smallest end-to-end sample.

Contract flow:
- `docs/GUI-Contract-Dev-Flow.md`

Internal module layout:
- `monitor.py`: Tk app shell, widget rendering, refresh/action orchestration
- `monitor_ipc.py`: IPC wire helpers + JSONPath/render utilities
- `monitor_config_payload.py`: config-show payload normalization helpers

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

Canonical contracts:

- `contract/schemas/monitor.root.schema.json`
- `contract/schemas/monitor.target.v2.schema.json`
- `contract/schemas/generic-process-interface.v1.schema.json`
- `contract/golden/*`

Compatibility mirror (used by deployed runtime/scripts):

- `schemas/monitor.root.schema.json`
- `schemas/monitor.target.v2.schema.json`

## Launcher Script

```bash
python scripts/launch_monitor.py --no-launch --print-config
python scripts/sync_target_schema.py --repo C:\repos\my-app
python scripts/launch_monitor.py --repo C:\repos\my-app --validate-only
python scripts/launch_monitor.py --fixture-repo \\H3FT06-40318\c\40318-SOFT --bridge-repo C:\repos\test-fixture-data-bridge
python scripts/check_target_contract.py --target C:\repos\test-fixture-data-bridge\config\gui\monitor.bridge.target.json
python scripts/check_target_contract.py --target \\H3FT06-40318\c\40318-SOFT\config\gui\monitor.fixture.target.json
python scripts/ci_target_policy.py --fixture-repo \\H3FT06-40318\c\40318-SOFT --bridge-repo C:\repos\test-fixture-data-bridge
```

Key flags:

- `--repo` (generic repo root; includes `config/gui/monitor.*.target.json`)
- `--target` (explicit target file path; may be repeated)
- `--fixture-repo` / `--bridge-repo`
- `--fixture-target` / `--bridge-target` (explicit file overrides)
- `--include-fixture` / `--include-bridge`
- `--config-out`
- `--validate-only`

Strict policy CI gate:

- `scripts/ci_target_policy.py` runs `check_target_contract.py --enforce-top-tabs` for selected targets.
- use this as a separate CI job so contract-shape validation and UI-policy validation are independently visible.
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
- fixture repo default: fixture tab only (add `--with-bridge` to include bridge tab)
- bridge repo default: bridge tab only (add `--with-fixture` to include fixture tab)

## Config Versions

- `configVersion: 2` = new explicit target model (preferred for new files).
- Missing `configVersion` = assumed `v1` (deprecated back-compat only).
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
- `ui.tabs[]`:
  - each tab has `id`, `title`, and at least one of `widgets[]` or `children[]`
  - tab nesting is supported recursively through `children[]`
- widget types: `kv`, `table`, `rows_table`, `log`, `button`, `profile_select`, `action_map`, `action_select`, `action_output`, `text_block`, `file_view`, `config_editor`, `config_file_select`
- `actionOutput.maxLines`, `actionOutput.maxBytes` (optional)

`rows_table` widget contract:

- required:
  - `rowsJsonpath` -> JSONPath resolving to a list in status payload
  - `columns[]` -> each item has `label` + (`key` or `jsonpath`)
- optional:
  - `emptyText`, `maxRows`

`config_editor` widget contract:

- required:
  - `showAction` -> action returning JSON object with `entries[]` (same shape as `python dev config show --json`)
  - `setAction` -> action command that accepts `{key}` and `{value}` placeholders
- optional:
  - `pathJsonpath` or `pathLiteral` for live status path display
  - `pathKey` to resolve path from `showAction` payload `paths[]` (`[{key,value}]` canonical)
  - `includePrefix`, `includeKeys[]`, `excludeKeys[]`, `settableOnly`, `reloadLabel`
  - `allowedValues[]` is accepted as a legacy alias of `allowed[]` during migration

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
