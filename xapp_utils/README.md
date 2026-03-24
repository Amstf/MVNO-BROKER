# xapp_utils/

Low-level utilities for RAN signaling, socket communication, configuration loading, and metrics reporting.

| File | Purpose |
|------|---------|
| `xapp_control.py` | TCP socket layer: opens the control socket, sends binary payloads, and receives framed data from the RIC/gNB. |
| `control_signaling.py` | Builds protobuf-encoded RAN control messages for triggering indications, sending slice-control updates, and issuing UE steering directives. |
| `metrics_utils.py` | Formatted console reporting: prints per-gNB QoS/resource tables and per-UE debug output for each tick. |
| `config_loader.py` | Loads a JSON configuration file from a given path or the `BASE_XAPP_CONFIG` environment variable. |
