# Contract Artifacts

This folder is the canonical source for GUI/IPC contract artifacts.

## Layout

- `schemas/`: versioned schema files used by producers and consumers.
- `golden/`: executable example artifacts validated in tests.
- `VERSIONS.lock`: pinned logical versions for sync/change review.

Policy metadata:

- `VERSIONS.lock.targetTabPolicy` pins the strict UI tab policy profile used by `scripts/ci_target_policy.py`.

## Compatibility Mirror

`schemas/` at repo root remains as a compatibility mirror for deployed tooling and historical paths.
Canonical updates should be made under `contract/` and mirrored as needed.
