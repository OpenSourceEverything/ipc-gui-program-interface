# Generic Examples

Use these files to validate the monitor contract with a tiny app integration.

## Quick Start

1. Run the sample IPC server:

```bash
python examples/minimal_ipc_server.py
```

2. Launch monitor with the example root config:

```bash
python monitor.py --config examples/monitor_config.example.json
```

3. Optional validation-only pass:

```bash
python monitor.py --config examples/monitor_config.example.json --validate-config
```

## Files

- `examples/monitor_config.example.json`
- `examples/targets/minimal.ipc.target.json`
- `examples/minimal_ipc_server.py`

## Notes

- The target uses IPC-only control mode (`control.mode=ipc`) and does not require local `actions[]`.
- Endpoint defaults to `127.0.0.1:8777`.
