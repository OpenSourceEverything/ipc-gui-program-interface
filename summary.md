Best cross-language approach
- transport: localhost HTTP bound to 127.0.0.1 only
- encoding: JSON
Reason: every language can host a tiny HTTP server and emit JSON.

Also good on Windows
- transport: named pipe
- encoding: JSON
Reason: strong local security, no ports. C# is easy; C/C++ is doable.

Per language feasibility
- C#
  - HTTP: built-in server options exist; easy.
  - named pipe: built-in support; very easy.
- C++
  - HTTP: use a small embedded HTTP server library; moderate effort.
  - named pipe: Windows API; moderate effort.
- C
  - HTTP: possible but more work (parsing, sockets); higher effort.
  - named pipe: Windows API; possible, still more manual work.

If you want C to be realistic, pick ONE of these:
- "daemon writes JSON status file" + "GUI reads it"
  - simplest for C
  - but you lose request/response and live queries
- HTTP + a tiny, strict, minimal C server you keep extremely small
  - doable but more engineering

Recommendation for your goals
- Start: C# daemon exposes GetStatus over named pipe or localhost HTTP.
- Next: define a tiny spec so C/C++ can implement it later.
- Keep protocol small and stable: Hello + GetStatus + GetLogTail.

Key constraint for bulletproof
- UI never required.
- daemon never blocks waiting on UI.
- all requests have size limits and timeouts.

Core idea
- GUI = generic client + renderer (widgets, charts, logs, alerts)
- target app (daemon) = exposes "status provider" API over IPC
- contract = versioned schema + capability discovery

Do NOT do this
- "target app links a library that talks to my GUI"
Reason: tight coupling, version hell, crashes propagate, harder security.

Do this instead
A) Standard wire protocol (daemon implements it, GUI consumes it)
- transport (choose one):
  1) named pipe (Windows best)
  2) localhost HTTP (cross-language easiest)
- encoding:
  - JSON (simple) or protobuf (more strict)

B) Required endpoints (minimum contract)
1) Hello
- request: {}
- response:
  - protocol_version
  - app_id (stable string)
  - app_name
  - app_version
  - instance_id (changes each run)
  - capabilities list (what else is supported)

2) GetStatus
- response fields (fixed small set):
  - state (string enum like "ok", "degraded", "error")
  - uptime_ms
  - last_error_code
  - last_error_text (bounded)
  - timestamp_utc

C) Optional endpoints (capabilities)
- GetMetrics
  - returns numeric timeseries snapshots (cpu, mem, queue depth)
- GetLogTail
  - returns last N lines, bounded size, with sequence numbers
- GetHealthChecks
  - list of named checks with pass/fail + message
- GetConfigSummary
  - safe, non-secret settings (never return secrets)

How to make it truly reusable
1) Capability-driven UI
- GUI starts with Hello.
- It only shows widgets for capabilities the daemon reports.

2) Schema-driven layout (no per-app code)
- Provide a "UI manifest" endpoint:
  - describes panels, fields, units, refresh rate, and grouping.
- Example concept (not literal code):
  - panels:
    - "Overview": show status fields
    - "Queues": show metric queue_depth, rate, max
    - "Errors": show recent errors and log tail

3) Namespaces and stable identifiers
- app_id: "com.yourco.product"
- metric keys: "queue.depth", "http.requests_per_s"
- check keys: "disk.space", "db.reachable"

4) Versioning rules (this is what keeps it amazing long-term)
- protocol_version major/minor
- major bump = breaking
- minor bump = new optional fields only
- never rename keys; deprecate instead

"Library" story (what target apps reuse)
- Provide small client/server helpers per language, but optional.
  - C# helper for named pipe or HTTP server boilerplate.
  - Python helper for testing.
- The contract is the real product. Libraries are convenience only.

Practical recommendation
- If you want generic across many languages fast: localhost HTTP + JSON.
- If Windows-only and want maximal lock-down: named pipe + JSON.

Minimal deliverable that is already reusable
- daemon implements Hello + GetStatus over your chosen transport
- GUI:
  - tray icon shows state color
  - click opens window with fields from GetStatus
  - optional log tail panel if capability exists

This becomes a generic UI you can point at many daemons by:
- selecting an instance (pipe name or localhost port)
- reading Hello
- rendering whatever it reports