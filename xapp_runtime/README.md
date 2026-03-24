# xapp_runtime/

Core runtime logic: state management, broker decisions, actuation, and persistence.

## Module Map

| File | Purpose |
|------|---------|
| `gnb_runtime_state.py` | `GnbState` dataclass holding real-time per-cell state: measured KPIs (throughput, PRBs, efficiency), steering outputs, CAP limits, and pricing data. |
| `config_contract.py` | Loads and validates the broker configuration, resolves gNB target mappings (logical ID / MEID), and normalizes incoming gNB IDs against the target set. |
| `targeting_constants.py` | Constants for gNB target mappings, SLA parameters, and broker configuration key names. |
| `snapshot.py` | Builds per-tick system snapshots from gNB states and metrics rows; evaluates snapshot freshness to determine decision validity. |
| `ue_aggregation.py` | Aggregates per-UE indication reports into a single sample per gNB, maintaining counter state and demand tracking across ticks. |
| `cap_runtime.py` | Builds capacity generators from config and orchestrates per-tick CAP planning (PRB cap updates for each operator). |
| `actuation_engine.py` | Executes action plans: adjusts UE traffic rates, sends slice-control messages, manages ghost-UE moves, and monitors system health. |
| `persistence_bridge.py` | MongoDB persistence: serializes gNB state documents and broker decision logs. |
| `gnb_state_repository.py` | Initializes the MongoDB client and collections with indexes for gNB state storage. |

## Broker Decision Engine (`broker/`)

| File | Purpose |
|------|---------|
| `decision.py` | Core dynamic broker: three-phase state machine (observe / decide / frozen) that sequences SLA repair, cost rebalancing, and GCSA UE steering. |
| `static_decision.py` | Static broker: applies fixed-rate decisions without adaptive steering, used with `--static` mode. |
| `sla.py` | SLA-driven steering: detects overloaded gNBs and computes rebalancing targets to move load toward underutilized cells. |
| `cost.py` | Cost-driven rebalancing: uses per-operator cost-per-Mbps ratios from the pricing model to shift traffic toward cheaper cells when SLA is met. |
| `gcsa.py` | Greedy Capacity-Safe Assignment (GCSA): computes individual UE move plans respecting per-cell capacity constraints. |

## Control Loop Path

Each tick follows this strict order:

1. Indication parse and target normalization.
2. gNB state update + metrics row build (per-UE entries aggregated per gNB).
3. Tick gate: snapshot construction + freshness check.
4. Broker decision (dynamic) or static no-steer decision.
5. CAP plan and enforcement updates.
6. Actuation and persistence writes.
