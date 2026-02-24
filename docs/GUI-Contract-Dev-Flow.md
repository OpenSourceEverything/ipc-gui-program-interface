# GUI Contract Dev Flow

This monitor is contract-driven. Repo target files, action payloads, and widget schema must stay aligned.

## Invariants

1. Canonical monitor engine + schema live only in this repo:
   - `monitor.py`
   - `contract/schemas/monitor.target.v2.schema.json`
   - `contract/schemas/generic-process-interface.v1.schema.json`
   - `contract/golden/*`
   - compatibility mirror for deployed tooling: `schemas/*`
2. App repos only provide:
   - target mapping (`config/gui/monitor.<target>.target.json`)
   - action providers (`dev config show --json`, `dev config set <key> <value>`)
3. Config tabs are generic:
   - `config_file_select` for active file pointer
   - `config_editor` for fields

## Required Checks

Run after any GUI/schema/target/config-contract change:

```bash
python monitor.py --config monitor_config.json --validate-config
python scripts/check_target_contract.py --target <repo>/config/gui/monitor.<target>.target.json
python scripts/ci_target_policy.py --repo <app_repo>
python scripts/launch_monitor.py --repo <app_repo> --validate-only
```

Policy split:

- Keep `check_target_contract.py` as contract correctness.
- Run `ci_target_policy.py` as a separate CI job for strict UI policy (`--enforce-top-tabs`).

## Repo Change Matrix

Use this to decide where code edits are required:

1. New widget type, target schema rule, renderer behavior:
   - Modify canonical GUI repo only (`monitor.py` + `schemas/monitor.target.v2.schema.json`).
   - App repos only need changes if they choose to use the new fields/widgets in target JSON.
2. Tab layout, action mapping, log/view wiring for one app:
   - Modify that app repo target file only (`config/gui/monitor.<target>.target.json`).
3. Config editor key/path contract changes (`key`, `pathKey`, `allowed`, settable keys):
   - Modify app repo CLI provider (`dev config show --json`, `dev config set`) and target JSON.
4. Canonical contract validation rule changes:
   - Modify canonical checker (`scripts/check_target_contract.py`).
   - Re-run checks against both app targets.
5. Runtime defaults/source resolution behavior:
   - Keep defaults in schema/env/target JSON.
   - Do not add hidden cross-repo hardcoded paths.

## When App Repos Must Change

Change app repos when any of these happen:

1. Target JSON tabs/actions/logs changed.
2. `config_file_select.key` or `.pathKey` changes.
3. `config_editor.pathKey` or filtering keys/prefixes change.
4. `dev config show --json` payload keys/paths change.
5. `dev config set` settable keys change.

## When App Repos Do Not Need Code Changes

UI-only renderer improvements in canonical monitor usually do not require app code changes, unless they introduce new widget contract fields used by targets.
