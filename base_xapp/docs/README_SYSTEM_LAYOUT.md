# xApp System Layout (Single Main + Focused Runtime Modules)

This is the current organization focused on clarity and strict behavior matching.
`xapp_main.py` is still the only runtime entry script.

## Top-level

- `xapp_main.py` → single execution loop, supports dynamic (default) and static (`--static`).
- `xapp_runtime/` → helper modules grouped by responsibility.
- `conf/ue_placement.conf` → UE startup/traffic mapping file (`pod`→gNB/port/rate).
- `xapp_runtime/broker/` → broker internals split by concern (SLA, cost, decision order).
- `xapp_utils/` → socket/control signaling utils (kept untouched).
- `modules/` → cap generator and price model implementations (kept untouched).
- `Legacy/` → frozen historical reference (do not edit).

## Runtime module map

### `xapp_main.py`
Owns the strict end-to-end control loop flow and mode condition (`--static`).
Chooses dynamic broker behavior or static no-steer behavior from one place, and loads UE placements from `--ue-placment` (`conf/ue_placement.conf` default).

### `xapp_runtime/config_contract.py`
Loads `config_loop.json`, validates broker keys, and resolves gNB targets.
Contains config contract checks only.

### `xapp_runtime/cap_runtime.py`
Single place for CAP generation setup and per-tick CAP planning (`step()` path).
Builds cap generators/scenarios and computes cap slice ratios for enforcement.

### `xapp_runtime/snapshot.py`
Builds per-tick snapshot objects from latest gNB state.
Evaluates freshness validity used by tick decision gating.

### `xapp_runtime/actuation_engine.py`
Contains pricing application into gNB state (pricing config loaded from background traffic config) and dynamic actuation logic.
Dynamic traffic restarts/cooldown stay here.

### `xapp_runtime/persistence_bridge.py`
Builds Mongo documents and provides safe persistence wrappers.
Bridges runtime data to repository operations.

### `xapp_runtime/gnb_runtime_state.py`
Defines `GnbState` and all measurement/state fields.
Exports per-tick metric rows used by broker and persistence.

### `xapp_runtime/gnb_state_repository.py`
Mongo initialization/index management and insert helpers.
Canonical database representation for gNB state docs.

## Broker split (single folder)

### `xapp_runtime/broker/sla.py`
SLA steering target computation only.
No cost logic mixed in this file.

### `xapp_runtime/broker/cost.py`
Cost rebalance target computation only.
No SLA steering logic mixed in this file.

### `xapp_runtime/broker/decision.py`
Combines SLA and cost in strict decision order.
Preserves the decision ladder behavior for dynamic mode.

## Strict loop path (unchanged intent)

1. Indication parse and target normalization.
2. gNB state update + metrics row build (all UE entries in indication are aggregated per gNB before metrics compute).
3. Throughput clamp (static mode only) and pricing update.
4. Tick gate snapshot + freshness check.
5. Broker decision (dynamic) or static no-steer decision.
6. CAP plan and enforcement updates.
7. Actuation and persistence writes.

## Dynamic vs static behavior in one script

- Dynamic (default): uses broker decision ladder + dynamic actuation.
- Static (`--static`): keeps fixed rates and no-steer behavior while still running the same loop skeleton.

This keeps future changes discoverable in one main script, while related logic is grouped in dedicated runtime modules.


- CLI: `--ue-debug` prints UE-level KPI-style rows (throughput/demand/gap/PRBs) instead of the gNB summary table (runtime logic unchanged).
