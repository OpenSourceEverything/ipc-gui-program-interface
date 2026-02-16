# Generic Process Interface v1

## Purpose
Define one reusable app-to-GUI contract so 40318, bridge, PLC simulator, and BLE simulator expose the same interface shape.

## Scope
v1 is contract-first and transport-agnostic.

- The GUI consumes command outputs and target JSON mappings.
- Apps can provide this contract through CLI wrappers today.
- A future shared C/C++ module can keep the same contract and change only transport.

## Invariants
1. Core app logic does not branch on GUI/test/sim concerns.
2. Differences stay at boundaries: wiring, adapters, config, and external simulators.
3. Every app exposes three interface modules: status, actions, config.
4. Each app has its own identity (`appId`) and process instance identity (`bootId`).

## Contract Modules
1. Status module
- Canonical payload schema: `schemas/generic-process-interface.v1.schema.json#/$defs/statusPayload`
- Required base keys:
  - `interfaceName = generic-process-interface`
  - `interfaceVersion = 1`
  - `appId`, `appTitle`
  - `bootId`
  - `running`, `pid`, `hostRunning`, `hostPid`

2. Actions module
- Canonical descriptor schema: `schemas/generic-process-interface.v1.schema.json#/$defs/actionDescriptor`
- Each app defines only its own actions.
- GUI action selectors must scope to local action names.

3. Config module
- Canonical show payload schema: `schemas/generic-process-interface.v1.schema.json#/$defs/configShowPayload`
- Supports:
  - active file selection (`config_file_select`)
  - field editing (`config_editor`)

## App Modeling Rules
1. 40318, PLC simulator, BLE simulator, and bridge are separate app identities in GUI.
2. PLC and BLE can be hosted by 40318 runtime today, but remain separate logical interfaces.
3. Cross-app controls are disallowed in app-local action tabs.

## bootId vs sessionId
1. `bootId` identifies one process instance for status/action correlation.
2. `sessionId` is only a log/artifact partition key.
3. GUI correctness must rely on `bootId` and status fields, not log folder naming.

## Migration Path
1. Lock v1 contract in targets and status adapters.
2. Keep current CLI-based providers.
3. Add shared runtime module later (library or sidecar) that serves the same v1 payloads.
