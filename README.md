# IPC Monitor GUI

JSON-driven Tkinter monitor for fixture/bridge targets.

## How It Talks To Apps

The GUI is a blind hook-in. It does not import app code.

Runtime flow:

1. Launcher builds a root config that includes one or more app-owned target files.
2. Each target file declares:
   - `status.cwd` + `status.cmd[]` (how to fetch current runtime state JSON)
   - `logs[]` (where to tail log streams by file glob)
   - `actions[]` (which commands can be run from the UI)
   - `ui.tabs[].widgets[]` (how to render values using `jsonpath`)
3. `monitor.py` executes `status.cmd[]`, parses JSON, and renders widgets from configured paths.
4. `monitor.py` tails configured logs and streams updates into log widgets.
5. `monitor.py` executes configured actions and captures `stdout`/`stderr` into Action Output.

App-side ownership:

- App repo owns business mapping in `config/gui/monitor.<app>.target.json`.
- GUI repo owns generic renderer + schemas.
- App repo can change fields/actions/log mapping without GUI code changes.

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
- `--include-fixture` / `--include-bridge`
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
- fixture repo default: fixture tab only (add `--with-bridge` to include bridge tab)
- bridge repo default: bridge tab only (add `--with-fixture` to include fixture tab)

## Config Versions

- `configVersion: 2` = new explicit target model (preferred for new files).
- Missing `configVersion` = assumed `v1` (back-compat only).
- No heuristic mixing: v2 is only parsed when `configVersion` is explicitly `2`.

## Versioning: config/gui vs bundle_version.json

Short answer: app behavior/version should stay app-owned in `config/gui`.

Use today (already in place):

- `config/gui/monitor.<app>.target.json` + `config/gui/monitor.target.v2.schema.json`
- `configVersion` in target file
- schema id/version in schema files

Optional guard (`bundle_version.json`):

- Not required for runtime behavior.
- Only useful as a copy/sync stamp when manually copying GUI bundle files into app repos.
- Value: quick drift detection ("is this app repo using the expected GUI bundle build?").

If you do not want a separate bundle file, that is valid. Keep versioning in `config/gui` + schema only.

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
  - widget types: `kv`, `table`, `log`, `button`, `profile_select`, `action_map`
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

## Status Payload Contract

Current model:

- The status payload contract is implicit in app target files:
  - widgets reference `jsonpath` keys
  - status command output must provide those keys

What is strict today:

- target file structure is schema-validated (`monitor.target.v2.schema.json`)
- command/action/log definitions are schema-validated

What is not strict yet:

- per-app status JSON field schema is not enforced by a dedicated JSON schema file in this repo.

Recommended next step (optional):

- Add app-owned status schemas in each app repo, for example:
  - `config/gui/status.fixture.schema.json`
  - `config/gui/status.bridge.schema.json`
- Validate status command output against those schemas before rendering.
