# xApp Main — O-RAN Multi-Operator Broker

This repository implements an O-RAN near-RT RIC xApp that performs real-time, multi-operator resource brokering over shared gNB infrastructure. The entry point is `xapp_main.py`, which runs a tick-based control loop: receives RAN indications, aggregates per-UE metrics, builds system snapshots, invokes a broker decision engine, and actuates rate/slice changes back to the RAN.

## Repository Structure

```
├── xapp_main.py                  # Entry point: tick-based control loop
├── conf/                         # Runtime configuration files (JSON)
├── gnb_utils/                    # gNB identity resolution and UE/pod management
├── modules/                      # Domain models (capacity generation, pricing)
├── xapp_utils/                   # Low-level signaling, socket I/O, metrics printing
├── xapp_runtime/                 # Core runtime: broker, actuation, persistence, snapshots
│   └── broker/                   # Decision engines (dynamic & static) and steering algorithms
├── oai-oran-protolib/            # Protobuf definitions for RAN message encoding/decoding
└── xapp_bs_connector/            # C/C++ RIC xApp connector (E2AP/E2SM ASN.1 codec, RMR transport, SDL)
```

## Prerequisites

- **Python 3.8**
- Install dependencies:
  ```bash
  pip install -r requirements.txt
  ```
- **MongoDB**: the broker persists per-tick metrics and decisions to MongoDB. Set the connection URI as an environment variable:
  ```bash
  export MONGODB_URI="mongodb://<host>:<port>"
  ```
  The fallback variable `MONGO_URI` is also accepted. If neither is set, the broker will fail at startup.

## Configuration Files

Three files in `conf/` control the broker behavior:

- **`config_loop.json`** — gNB targets, slice identity (SST/SD), broker tuning knobs, and SLA parameters.
- **`background_traffic_gnb.json`** — scenario definition: capacity-generator parameters, phase progression, and pricing coefficients per domain.
- **`ue_placement.conf`** — UE-to-gNB mapping with pod name, port, role (active/ghost), and initial rate demand.

See [`conf/`](conf/) for a summary and [`conf/configuration_reference.md`](conf/configuration_reference.md) for the full parameter reference.

## Operating Modes

The broker supports three operating modes that correspond to the three policies evaluated in the paper:

| Mode | CLI | Behavior |
|------|-----|----------|
| **Dynamic (SLO + cost)** | `python3 xapp_main.py` | Full broker: SLA steering restores compliance when violated, cost rebalancing shifts load to cheaper domains during safe intervals. |
| **SLA-only** | `python3 xapp_main.py --sla` | SLA steering only. Cost rebalancing is disabled; the broker never moves UEs for cost reasons. |
| **Static** | `python3 xapp_main.py --static` | Fixed UE placement. No steering or rebalancing. Runs the same tick loop but never actuates moves. |

### Usage

```bash
python3 xapp_main.py \
  --config conf/config_loop.json \
  --traffic conf/background_traffic_gnb.json \
  --ue-placement conf/ue_placement.conf
```

### CLI Flags

| Flag | Description |
|------|-------------|
| `--config PATH` | Path to main broker configuration (default: `conf/config_loop.json`). |
| `--traffic PATH` | Path to background traffic / capacity-generator config. |
| `--ue-placement PATH` | Path to UE placement file. |
| `--static` | Run in static mode (fixed placement, no steering). |
| `--sla` | SLA-only mode: disable cost rebalancing, keep SLA steering. |
| `--collection NAME` | MongoDB base collection name (default: `gnb_state`). See [MongoDB Persistence](#mongodb-persistence). |
| `--ue-debug` | Print per-UE KPI rows (throughput, demand, gap, PRBs) each tick instead of the default per-gNB summary table. Without this flag, the broker prints a compact gNB-level summary per tick. |

## MongoDB Persistence

The broker writes experiment data to MongoDB database `Paper1`. The `--collection` flag sets the base collection name (default: `gnb_state`). Two collections are created automatically:

**`{collection}`** — one document per gNB per tick, containing:
- Measured KPIs: throughput, goodput, PRBs, efficiency, BLER, MCS.
- Capacity context: effective PRB cap, cap ratio.
- Steering state: SLA target, deficit, expected gain, role, active flag.
- Pricing: scarcity, cost, guaranteed/best-effort PRB breakdown.

**`{collection}_broker_decision`** — one document per tick, containing:
- Broker phase (SLA steer, cost rebalance, observe, hold).
- System snapshot: total throughput, per-gNB summary, SLA target.
- UE state and any UE moves actuated.
- Desired rates, actuation commands, and decision reason.

Example: running with `--collection run_01` creates collections `run_01` and `run_01_broker_decision` in the `Paper1` database.

## Broker Timing

The broker loop executes at a fixed period (configurable via `broker.tick_period_s` in `config_loop.json`, default: 5 seconds). At each tick, the broker ingests domain indications, validates snapshot freshness, and issues either an SLA steering or cost rebalancing decision. Following any actuation, the system enforces a configurable observation dwell (`decision_cooldown_ticks`) before the next decision is eligible.

## Notes

- **gNB identifiers**: the short logical IDs (e.g., `21`, `223`) and the long MEIDs (e.g., `gnb_734_733_61623130`) originate from the gNB and the near-RT RIC respectively. They are mapped in `config_loop.json` under `gnb_targets`. This mapping is currently configured manually and will be automated in a future version.
