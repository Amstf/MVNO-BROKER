# conf/

Runtime configuration files loaded by `xapp_main.py` at startup.

| File | Purpose |
|------|---------|
| `config_loop.json` | Main broker configuration: gNB targets, slice identity, broker tuning, and SLA parameters. |
| `background_traffic_gnb.json` | Scenario definition: capacity-generator parameters, phase progression, and pricing coefficients. |
| `ue_placement.conf` | UE placement map: each UE pod with its gNB assignment, port, role, and initial rate demand. |

For a detailed breakdown of every configuration key and its usage, see [configuration_reference.md](configuration_reference.md).
