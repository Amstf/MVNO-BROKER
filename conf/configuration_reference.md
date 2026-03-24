# Configuration Reference

Detailed reference for `conf/config_loop.json` and related files.
Pricing coefficients are loaded from `conf/background_traffic_gnb.json` (see section 7).

---

## 1) `gnb_targets`
**Objective:** define controlled gNBs and identity mapping.
**Used by:** target resolution in `xapp_runtime/config_contract.py`.

- `gnb_targets.<op_id>.meid`
  - Purpose: map indication MEID to logical operator id.
  - Example objective: add a third gNB (`"310"`) and its MEID so it is included in control.
- `gnb_targets.<op_id>.cell_total_prbs`
  - Purpose: initialize PRB capacity context in `GnbState`.
  - Example objective: correct cell PRB total if radio profile changes.

## 2) `slice`
**Objective:** identify slice for control dispatch.
**Used by:** `xapp_main.py` when sending slice control updates.

- `slice.sst`
- `slice.sd` or `slice.sd_hex`

Example objective: run experiment on a different slice while keeping policy unchanged.

## 3) `ue`
**Objective:** reserved runtime block (kept for compatibility).
**Used by:** currently not used for placements; placements are loaded from `conf/ue_placement.conf` via `--ue-placement`.

Example objective: keep empty object `{}` while driving placements from the dedicated file.

## 4) `ue_control`
**Objective:** define offered traffic and UE runtime control mapping.
**Used by:** initial rate setup + traffic control paths in `xapp_main.py`.

- `initial_rate_mbps`: initial offered load per operator.
- `duration_s`: iperf traffic run duration.
- `by_operator.<op_id>.pod` / `.port`: traffic endpoint per operator.

Example objective: increase initial offered load from 70 to 90 Mbps for stress tests.

### UE placement file (`conf/ue_placement.conf`)
- Source of truth for UE startup entries.
- Each entry supports: `pod`, `logical_id`, `gnb_id`, `gnb_ip`, `conf`, `port`, `initial_rate_mbps`.
- Main loop loads this file with `--ue-placement` and calls `configure_ue_settings({"placements": ...})`.

### Multi-UE metric aggregation (runtime behavior)
- Per indication, UE entries from `UE_LIST` are aggregated into a single synthetic per-gNB sample before `GnbState.compute_metrics(...)`.
- Throughput/goodput are computed per UE from deltas, then summed at gNB level.
- `avg_prbs_dl` is summed across UEs; `avg_tbs_per_prb_dl` uses the max UE value for that indication.

## 5) `broker`
**Objective:** tune decision timing and policy constraints.
**Used by:** broker config validation + dynamic decision engine.

### Timing / snapshot validity
- `tick_period_s`
- `snapshot_require_all_gnbs`
- `max_freshness_ms`

Example objective: raise `max_freshness_ms` when indication cadence is unstable.

### SLA steering parameters
- `slice_sla_mbps`
- `steering_tolerance`
- `steering_step_mbps`

Example objective: reduce `steering_step_mbps` for smoother steering changes.

### Cost rebalance parameters (dynamic mode)
- `cost_hysteresis_ratio`
- `mbps_per_cost_action`
- `cost_min_headroom_prbs`
- `cost_minimal_report_number`
- `cost_min_hold_reports`

Example objective: increase `cost_hysteresis_ratio` to reduce cost-based oscillations.

### Actuation safety constraints
- `min_rate_delta_mbps`
- `max_rate_step_mbps_per_tick`
- `decision_cooldown_ticks`

Example objective: cap aggressive per-tick rate change during live demos.

### Startup gate
- `startup_no_steer_reports`

Example objective: wait first N reports before any steering decision.

## 6) `pricing` (in background_traffic_gnb.json)
**Location:** `conf/background_traffic_gnb.json` under `pricing.ops`.
**Used by:** `PriceModel.from_config_dict(...)` in runtime, loaded from traffic config in `xapp_main.py`.

- `pricing.ops.<op_id>.cap_base`
- `pricing.ops.<op_id>.nu`
- `pricing.ops.<op_id>.pi_min`
- `pricing.ops.<op_id>.pi_be`
- `pricing.ops.<op_id>.eps`

Example objective: increase `nu` for an operator to make scarcity more sensitive.

## Optional top-level keys
- `clamp_throughput_enabled`
- `clamp_throughput_mbps`

Behavior note:
- Both modes read config.
- `clamp_throughput_enabled` and `clamp_throughput_mbps` are applied only in static mode (`--static`).
- Dynamic mode always uses unclamped throughput for decisions and persisted metrics.
