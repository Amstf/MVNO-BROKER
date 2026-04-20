# 5G O-RAN xApp — Code-Read Answers (No Fix Proposals)

This document answers Q1–Q44 strictly by reading the current code.
For each answer, I cite file + function + concrete logic/conditions.

---

## SECTION 1 — TICK AND INDICATION LIFECYCLE

### Q1. What defines one tick? tick period? boundary crossing?
- Tick period is `broker.tick_period_s` from `config_loop.json`, read in `xapp_main.py::main` as `tick_period_s = float(broker_cfg.get("tick_period_s", 0.5))`. Tick starts from `tick_start_ts` and deadline is `tick_deadline = tick_start_ts + tick_period_s`. Boundary is crossed when `now >= tick_deadline`. 
- Source: `xapp_main.py::main` (tick config and boundary condition).

### Q2. How many indications per tick? what if only one gNB before deadline?
- Not fixed to exactly one indication total; the loop processes every incoming indication and can process multiple per gNB per tick.
- `seen_gnbs` records which logical gNBs were seen during current tick; validity gate uses `missing = target_set - seen_gnbs` and `seen_ok = len(missing)==0`.
- If only one gNB sends before deadline and `snapshot_require_all_gnbs=true`, snapshot validity can be false (through `seen_ok` and freshness checks).
- Source: `xapp_main.py::main` (indication loop + tick block) and `snapshot.py::evaluate_snapshot_freshness`.

### Q3. What is `seen_gnbs`? reset timing? `snapshot_require_all_gnbs` impact?
- `seen_gnbs` is a per-tick set of logical gNB IDs that delivered at least one indication in current tick (`seen_gnbs.add(logical_id)`).
- It resets after tick handling (`seen_gnbs = set()`) and also in startup tick-skip branch.
- `snapshot_require_all_gnbs` controls whether validity uses `seen_ok` + freshness; when true, `valid_snapshot = seen_ok and freshness_ok`; when false, `valid_snapshot=True` regardless.
- Source: `xapp_main.py::main`, `snapshot.py::evaluate_snapshot_freshness`.

### Q4. What is `max_freshness_ms`? stale behavior?
- `max_freshness_ms` comes from `broker.max_freshness_ms` (default `2*tick_period_s*1000`) in `xapp_main.py::main`.
- Per-gNB freshness is `freshness_age_ms = (tick_ts - last_seen_ts)*1000` built in `snapshot.py::_build_tick_snapshot`.
- If `snapshot_require_all_gnbs=true`, each gNB age must be non-None and <= `max_freshness_ms`; otherwise `freshness_ok=False`, and then `valid_snapshot=False`.
- Source: `xapp_main.py::main`, `snapshot.py::_build_tick_snapshot`, `snapshot.py::evaluate_snapshot_freshness`.

### Q5. Exact per-indication operation order until next `_send_indication`
In `xapp_main.py::main`, for each indication:
1. `receive_from_socket` gets bytes.
2. Parse protobuf response.
3. Extract gNB ID from `param_map` (`GNB_ID`).
4. Normalize to logical gNB via `normalize_gnb_id`; if non-target: print skip and `_send_indication`, continue.
5. Extract UE list from `param_map` (`UE_LIST`).
6. Build `raw_rntis` set and store in `runtime_state["current_rntis_by_gnb"][logical_id]`.
7. Advance startup FSM `_advance_startup_state(runtime_state, duration_s)`.
8. If UE list empty: `_send_indication`, sleep, continue.
9. Update `gnb_state` meta fields (`meid`, report index, tick fields).
10. Call `build_aggregated_ue_sample(...)` and `gnb_state.update_sample(...)`.
11. Apply optional efficiency override `_apply_eff_override_to_states`.
12. Compute metrics row (`compute_metrics`), normalize throughput/goodput fields, set offered/tick fields.
13. Apply pricing `_apply_pricing_to_state`.
14. Store row in `latest_rows`, increment `total_reports`, update `seen_gnbs`.
15. If tick boundary reached (`now >= tick_deadline`): do tick snapshot/decision/actuation/report/persistence/deadline advance.
16. Call `_send_indication(udp_sock)`.
- Source: `xapp_main.py::main`.

### Q6. When does broker decision fire?
- Broker decision fires only inside tick block and only after startup is complete (`if now >= tick_deadline` and `startup_complete` true branch).
- Inside that tick branch, one broker step per tick (`broker_step(...)` in dynamic mode, `static_broker_step(...)` in static mode).
- It is not per indication.
- Source: `xapp_main.py::main`.

---

## SECTION 2 — UE AGGREGATION AND THROUGHPUT COMPUTATION

### Q7. Per-UE throughput formula
In `xapp_runtime/ue_aggregation.py::build_aggregated_ue_sample`:
- Raw counters: `dl_total_bytes` (MAC) and `dl_pdcp_sdu_bytes` (PDCP).
- Per-UE stored previous sample in `per_ue[rnti]` with `mac/pdcp/ts`.
- `dt = now - prev_ts`; if `dt<=0`, skip.
- `dmac = max(0, mac_total - prev_mac)`, `dpdcp = max(0, pdcp_total - prev_pdcp)`.
- Throughput: `ue_tp_mbps = (dmac * 8.0) / (dt * 1e6)`.
- Goodput: `ue_gp_mbps = (dpdcp * 8.0) / (dt * 1e6)`.

### Q8. Per-gNB throughput from per-UE
- `total_tp_mbps` is sum of `ue_tp_mbps` across UEs with valid delta in that indication.
- `aggregated_sample` encodes cumulative bytes using that total over gNB window dt.
- Later `GnbState.compute_metrics` computes gNB throughput from gNB aggregate counter deltas (`_curr_mac - _prev_mac` over dt), i.e., effectively aggregated sum behavior.
- Source: `ue_aggregation.py::build_aggregated_ue_sample`, `gnb_runtime_state.py::compute_metrics`.

### Q9. `ue_demand_state` and `by_rnti` semantics
- `ue_demand_state[op_id]` has `{rates, next_idx, by_rnti}` in `xapp_main.py::main`.
- `by_rnti` maps **int RNTI -> demand Mbps** and is read in aggregation.
- New RNTI assignment in aggregation: if mapping exists in `rnti_to_ue`, assign from `ue_rate_map[logical_id]`; else assign `0.0`.
- It is updated after first assignment in several places:
  - startup mapping writes correct demand in `_advance_startup_state`,
  - swap path updates by_rnti in `_apply_swap_moves`,
  - ghost direct thread removes old source RNTI and writes target new RNTI demand.
- Source: `xapp_main.py::_advance_startup_state`, `ue_aggregation.py::build_aggregated_ue_sample`, `actuation_engine.py::_apply_swap_moves`, `_apply_direct_move_with_ghost`.

### Q10. `default_ue_rate` source/use
- `default_ue_rate` argument passed as `float(initial_rate_mbps)` from `xapp_main.py`.
- `initial_rate_mbps` comes from `ue_control.initial_rate_mbps` in config (overridable by CLI `--ue-rate`).
- In current aggregation logic, it is used only as fallback for mapped logical UE missing in `ue_rate_map` and in `by_rnti.get(..., default_ue_rate)` fallback expression.
- Source: `xapp_main.py::main`, `ue_aggregation.py::build_aggregated_ue_sample`.

### Q11. Return of `build_aggregated_ue_sample` and downstream use
Returns `(aggregated_sample, ue_debug_rows)`:
- `aggregated_sample` (SimpleNamespace) fields:
  - `dl_pdcp_sdu_bytes`, `dl_total_bytes`, `avg_prbs_dl`, `avg_tbs_per_prb_dl`, `dl_bler`, `dl_mcs`.
- Used by `gnb_state.update_sample(aggregated_sample, now)`.
- `ue_debug_rows` stored in `latest_ue_by_gnb` for debug reporting.
- Source: `ue_aggregation.py::build_aggregated_ue_sample`, `xapp_main.py::main`.

### Q12. How `gnb_state.offered_mbps` is set/updated
- Initial set before loop from `last_ue_rates` in `xapp_main.py`.
- During actuation refresh, `_refresh_offered_from_ue_state` recomputes per gNB as sum of UE rates from `runtime_state["ue_state"]`, then updates both `last_ue_rates` and `gnb_states[op_id].offered_mbps`.
- Static mode also sets from fixed rates in `apply_static_action_plan`.
- It represents offered demand-like target load per gNB (not measured throughput).
- Source: `xapp_main.py::main`, `actuation_engine.py::_refresh_offered_from_ue_state`, `apply_static_action_plan`.

---

## SECTION 3 — SNAPSHOT CONSTRUCTION

### Q13. Snapshot keys and per_gnb sub-keys
`_build_tick_snapshot` returns:
- top-level: `tick_id`, `tick_ts`, `per_gnb`, `total_throughput`, `slice_sla_mbps`, `total_demand_mbps`.
- `per_gnb[op_id]` contains:
  `throughput_mbps`, `prbs`, `gnb_eff_mbps_per_prb`, `cap_effective_prb`, `cost`, `scarcity`, `offered_mbps`, `last_seen_ts`, `freshness_age_ms`, `state`.
- Source: `snapshot.py::_build_tick_snapshot`.

### Q14. `total_throughput` computation
- Yes: sum across gNBs of `row.throughput_mbps` in snapshot builder loop.
- Source: `snapshot.py::_build_tick_snapshot`.

### Q15. `slice_sla_mbps` determination
- Set at tick creation call in `xapp_main.py` as `float(broker_cfg.get("slice_sla_mbps", sum(last_ue_rates.values())))`.
- So primary source is static config `broker.slice_sla_mbps`; fallback is dynamic sum of `last_ue_rates`.
- Source: `xapp_main.py::main` tick block.

### Q16. `total_demand_mbps` computation
- Passed as `float(sum(last_ue_rates.values()))` when building snapshot.
- `last_ue_rates` is updated from `ue_state` by `_refresh_offered_from_ue_state`; entries keyed by target gNB IDs only.
- It does not directly count per-RNTI rows; ghost/zombie are only represented insofar as they affect `ue_state` and per-gNB sums.
- Source: `xapp_main.py::main`, `actuation_engine.py::_refresh_offered_from_ue_state`.

### Q17. `cost` and `scarcity` formulas and inputs
- Pricing applied in `_apply_pricing_to_state`: inputs are `cap=gnb_state.cap_effective_prb`, `used=metrics_row.prbs`, `bmin=bmin_by_op[op_id]`.
- `scarcity`: `s = (cap_base / (cap + eps)) ** nu`.
- `cost`: `c = s * (pi_min * guaranteed + pi_be * best_effort)`, with
  - `guaranteed = min(bmin, used)`,
  - `best_effort = max(0, used - bmin)`,
  - `used` clamped to `[0, cap]`.
- Source: `actuation_engine.py::_apply_pricing_to_state`, `modules/price_model.py::scarcity`, `PriceModel.cost`.

### Q18. `cap_effective_prb` origin and relation to slice control ratios
- Origin: per tick `plan_cap_for_tick` calls generator `step()`, takes `cap_prb`, then `gnb_states[op_id].apply_cap(cap_prb)` which sets `cap_effective_prb` and `cap_ratio` (`100*cap/cell_total_prbs`).
- Slice control uses `cap_ratio` as `max_ratio`, and computes `min_ratio` from configured `min_prb_by_op`: `100*min_prb/cell_total_prbs`, clamped not to exceed `max_ratio`.
- Source: `cap_runtime.py::plan_cap_for_tick`, `gnb_runtime_state.py::apply_cap`.

---

## SECTION 4 — BROKER DECISION LOGIC

### Q19. Conditions for non-empty `ue_moves`
For dynamic broker (`decision.py::broker_step`), non-empty `ue_moves` requires all relevant guards to pass and GCSA to produce moves:
1. `valid_snapshot` must be true (else `invalid_snapshot`).
2. `report_count >= startup_no_steer_reports` (else `hold_startup_no_steer`).
3. If SLA deficit (`slice_sla_mbps - total_throughput > 0`), Phase-1 runs immediately.
4. If no deficit, then cost path requires:
   - `report_count >= cost_min_hold_reports` (else `hold_cost_warmup`),
   - `sla_ok_tick_streak >= cost_minimal_report_number` (else `hold_sla_warmup`).
5. In whichever phase runs, `compute_gcsa_moves` must output at least one move (otherwise empty list).
6. Inside GCSA, additional suppressors: empty `ue_state`, all gaps < 0.1, no eligible direct/swap candidates.
- Source: `decision.py::broker_step`, `gcsa.py::compute_gcsa_moves`.

### Q20. `sla_ok_tick_streak`
- Maintained in `xapp_main.py` tick block: incremented by 1 when `valid_snapshot` and `total_throughput >= slice_sla_mbps`; reset to 0 otherwise.
- Used in broker cost guard: cost rebalance requires `sla_ok_tick_streak >= cost_minimal_report_number`.
- Source: `xapp_main.py::main`, `decision.py::broker_step`.

### Q21. `cost_minimal_report_number`
- Read from `policy_cfg` in `decision.py`; compared against `sla_ok_tick_streak`.
- It guards cost rebalance until enough consecutive SLA-ok ticks occurred.
- It is effectively a **tick-based streak threshold**, not raw indication count.
- Source: `decision.py::broker_step`, `xapp_main.py` streak update.

### Q22. `startup_no_steer_reports`
- Read from `policy_cfg`; guard is `report_count < startup_no_steer_reports` -> hold.
- `report_count` is set from `total_reports` (indication count) once per tick before broker call.
- Startup attachment phase affects whether tick block runs (startup_complete gate), and therefore when broker starts evaluating this guard.
- Source: `decision.py::broker_step`, `xapp_main.py::main`.

### Q23. `hold_cost_warmup` vs `hold_sla_warmup`
- `hold_cost_warmup`: no SLA deficit path, and `report_count < cost_min_hold_reports`.
- `hold_sla_warmup`: after passing `cost_min_hold_reports`, but `sla_ok_tick_streak < cost_minimal_report_number`.
- Source: `decision.py::broker_step`.

### Q24. `decision_cooldown_ticks`
- Counter stored in `runtime_state` and read in `apply_action_plan`.
- If `cooldown_left > 0`, it decrements by 1 and returns without executing UE moves.
- After any actuation with `executed` moves, counter is reset to configured value `decision_cooldown_ticks_cfg`.
- Number of skipped ticks equals configured value unless interrupted by process state changes.
- Source: `actuation_engine.py::apply_action_plan`, and cfg seed in `xapp_main.py` runtime_state init.

### Q25. Phase 1 (SLA restoration)
- Trigger: `deficit = max(0, sla - total_throughput)` > 0 in `decision.py`.
- Desired rates computed by `compute_sla_steer_targets`:
  1. `offered` and `delivered` per gNB.
  2. `deficits[op]=max(0, offered-delivered)`.
  3. choose `overloaded_id = argmax(deficits)`.
  4. trigger threshold = `abs(steering_tolerance * offered[overloaded_id])`; if overloaded deficit <= trigger, no change.
  5. compute headroom per candidate gNB from `cap_effective_prb - used_prbs` times efficiency (`eff_override` if present else `throughput/used_prbs`, clipped [0.05,3.0]).
  6. pick absorber as non-overloaded gNB with max headroom.
  7. shift = `min(steering_step_mbps, overloaded_def, absorber_head)`.
  8. desired rates: overloaded minus shift, absorber plus shift.
- Output of phase-1 in decision: `reason="sla_steer"`, `desired_rates`, and `ue_moves` from GCSA.
- Source: `decision.py::broker_step`, `sla.py::compute_sla_steer_targets`.

### Q26. Phase 2 (cost reduction)
- Entered only when deficit==0 and warmup guards pass.
- Desired rates via `compute_cost_rebalance_targets`:
  1. for each gNB with `used_prbs>0`, `eff>0`, `cost>0`, compute `cost_eff = cost/eff`.
  2. require at least 2 domains with valid `cost_eff`.
  3. expensive domain = max `cost_eff`.
  4. feasible cheap domains require `headroom = cap_effective_prb - prbs >= cost_min_headroom_prbs`.
  5. choose cheap with minimal `cost_eff` (tie-break larger headroom).
  6. `advantage = (expensive-cheap)/cheap`; must be >= `cost_hysteresis_ratio`.
  7. shift step = `mbps_per_cost_action`; apply expensive-step, cheap+step (floored at 1.0 Mbps).
- Then decision uses GCSA to translate desired rates into UE moves (`reason="cost_rebalance"`).
- Source: `decision.py::broker_step`, `cost.py::compute_cost_rebalance_targets`.

### Q27. Gap formula G[d]
- In GCSA Step-1:
  - `L[d] = sum(ue.rate_mbps for UEs with ue.gnb_id==d)`
  - `gap[d] = desired_rates[d] - L[d]`
- Source: `gcsa.py::compute_gcsa_moves`.

### Q28. Pass 1 direct moves details
- Source domains: `gap[d] < -0.1` sorted by descending `abs(gap)`.
- Candidate UEs in source sorted descending by UE rate.
- UE selected if for some target (targets sorted by descending positive gap):
  - `r_u <= gap[d_plus]` and
  - `r_u <= abs(gap[d_minus])`.
- After move: add `direct` move, update gaps (`gap[target]-=r_u`, `gap[source]+=r_u`), mark UE moved.
- Source: `gcsa.py::compute_gcsa_moves`.

### Q29. Pass 2 swap evaluation and criterion; physical vs rate change
- For each source/target with residual gaps, examine UE pairs `(u in source, v in target)` excluding already moved.
- Compute `net = r_u - r_v`; candidate requires `net>0`, `net<=gap[target]`, `net<=abs(gap[source])`.
- Best-fit criterion is minimal residual `abs(gap[target]-net)`.
- Two `swap` moves emitted with exchanged rates (`u.new_rate=r_v`, `v.new_rate=r_u`).
- In decision output, swap denotes reassignment metadata, but actuation swap path only restarts traffic rates and does not reattach UE process.
- Source: `gcsa.py::compute_gcsa_moves`, `actuation_engine.py::_apply_swap_moves`.

### Q30. Pass 3 chains implemented?
- No Pass-3 implementation exists in current `gcsa.py`.
- If Pass-1 and Pass-2 produce no moves, function returns empty and logs that no UE could be swapped/moved.
- Source: `gcsa.py::compute_gcsa_moves`.

### Q31. How move type is assigned
- Type assigned directly in GCSA move construction:
  - Pass-1 emits `"type": "direct"`.
  - Pass-2 emits `"type": "swap"` for both paired moves.
- Source: `gcsa.py::compute_gcsa_moves`.

---

## SECTION 5 — ACTUATION AND RATE UPDATES

### Q32. Swap execution: updated state and order
In `_apply_swap_moves` after successful traffic starts:
1. For each move in pair, set `runtime_state["ue_rate"][logical_id] = new_rate`.
2. If UE exists in `runtime_state["ue_state"]`, set `ue_state[logical_id]["rate_mbps"] = new_rate`.
3. Resolve `(gnb_id, rnti_hex)` via `runtime_state["ue_to_rnti"][logical_id]`.
4. Update `runtime_state["ue_demand_state"][gnb_id]["by_rnti"][rnti_int] = new_rate`.
5. Append moves to executed list.
- Source: `actuation_engine.py::_apply_swap_moves`.

### Q33. Direct ghost move sync vs async
- Synchronous/main thread in `_apply_direct_move_with_ghost`:
  - validate profiles/target IP,
  - call `start_ue_in_pod(ghost, target_ip, conf)`,
  - create pending entry in `PENDING_GHOST_MOVES` (`thread`, `status`, target info, `started_at`, `rntis_before`),
  - start daemon thread, return `True` immediately.
- Asynchronous thread `_complete_direct_move`:
  - wait iface up,
  - stop source UE, clear ready cache,
  - start traffic on ghost,
  - rotate ghost/source roles and update `ue_role/ue_gnb_id/ue_rate`,
  - update `ue_state`, demand/rnti maps cleanup+new assignment,
  - set pending status done/failed.
- Source: `actuation_engine.py::_apply_direct_move_with_ghost`.

### Q34. `last_ue_rates` update and meaning
- Updated in `_refresh_offered_from_ue_state` as rounded per-gNB sum of UE rates from `ue_state`; also updated in static mode fixed-rate path.
- Used in next tick snapshot call for `total_demand_mbps = sum(last_ue_rates.values())` and SLA fallback.
- Represents per-gNB offered UE rate totals (not PRB allocations).
- Source: `actuation_engine.py::_refresh_offered_from_ue_state`, `xapp_main.py::main` tick snapshot call.

### Q35. `offered_mbps` update after actuation
- Same function `_refresh_offered_from_ue_state` sets `gnb_states[op_id].offered_mbps = total` after UE moves.
- Called at end of `_apply_ue_moves` each dynamic actuation pass.
- Source: `actuation_engine.py::_refresh_offered_from_ue_state`, `_apply_ue_moves`.

### Q36. `rates_before` / `rates_after` semantics
- They are snapshots of `runtime_state["last_ue_rates"]` before/after actuation, per gNB integer Mbps.
- They represent offered UE rate totals used by runtime state, not PRB caps.
- Source: `actuation_engine.py::apply_action_plan`, `apply_static_action_plan`.

---

## SECTION 6 — CAP AND SLICE CONTROL

### Q37. Cap generator cadence
- Cap runtime is built once at startup (`build_cap_runtime`).
- New cap plan produced in each valid tick via `plan_cap_for_tick(...)` inside tick block.
- So generation is tick-driven (every valid tick), not event-driven.
- Source: `xapp_main.py::main`, `cap_runtime.py::plan_cap_for_tick`.

### Q38. `plan_cap_for_tick` output, min/max ratio, units
- Output: `{"slice_ctrl_updates": {op_id: {"min_ratio": int, "max_ratio": int}}}`.
- `max_ratio` is rounded `cap_ratio` (%) from `cap_effective_prb/cell_total_prbs`.
- `min_ratio` is rounded percentage from `min_prb_by_op / cell_total_prbs`, clamped to `<= max_ratio`.
- Units: integer percent ratio values.
- Source: `cap_runtime.py::plan_cap_for_tick`, `gnb_runtime_state.py::apply_cap`.

### Q39. How many slice control messages per tick and when
- `apply_action_plan`/`apply_static_action_plan` iterate over `slice_ctrl_updates` and call `send_slice_ctrl` per op_id entry.
- Therefore up to one message per gNB in update set each tick (not a single combined message).
- Called after broker output is prepared and before UE move execution/cooldown handling in actuation function.
- Source: `actuation_engine.py::apply_action_plan`, `apply_static_action_plan`, `xapp_main.py` tick block.

### Q40. Cap/slice control feedback into broker headroom/capacity
- Yes. `cap_effective_prb` is stored in gNB state and propagated to snapshot per_gnb.
- SLA phase headroom uses `cap_effective_prb - used_prbs`.
- Cost phase feasibility uses same headroom check against `cost_min_headroom_prbs`.
- Pricing/cost calculation also uses cap as input in `PriceModel.cost` via `_apply_pricing_to_state`.
- Source: `snapshot.py::_build_tick_snapshot`, `sla.py::compute_sla_steer_targets`, `cost.py::compute_cost_rebalance_targets`, `actuation_engine.py::_apply_pricing_to_state`.

---

## SECTION 7 — CONFIGURATION PARAMETERS

### Q41. All `broker_cfg` parameters, defaults, readers/functions
Read in `xapp_main.py::main` (plus consumed downstream):
- `tick_period_s` (default 0.5): tick interval. Used in main loop deadline logic.
- `snapshot_require_all_gnbs` (default True): snapshot validity mode. Used by `evaluate_snapshot_freshness` call.
- `max_freshness_ms` (default `2*tick_period_s*1000`): freshness threshold. Used by `evaluate_snapshot_freshness` call.
- `slice_sla_mbps` (fallback `sum(last_ue_rates)`): passed into snapshot.
- `steering_tolerance` (default 0.1): SLA trigger tolerance in `compute_sla_steer_targets`.
- `steering_step_mbps` (default 5.0): SLA shift step in `compute_sla_steer_targets`.
- `mbps_per_cost_action` (default 3.0): cost rebalance shift step in `compute_cost_rebalance_targets`.
- `cost_hysteresis_ratio` (default 0.10): minimum advantage threshold in cost phase.
- `decision_cooldown_ticks` (default 0): cooldown reset value in actuation.
- `cost_minimal_report_number` (default 3 in policy cfg build): required `sla_ok_tick_streak` for cost phase.
- `cost_min_hold_reports` (default 0): report warmup hold before cost phase.
- `cost_min_headroom_prbs` (default 2.0): minimum cheap-domain headroom for cost phase.
- `startup_no_steer_reports` (default 0): initial no-steer hold on report count.
- `min_rate_delta_mbps` (default 0.0): stored in runtime_state (currently not consumed in shown decision path).
- `max_rate_step_mbps_per_tick` (default 5.0): stored in runtime_state (currently not consumed in shown decision path).
- Source: `xapp_main.py::main`, `decision.py::broker_step`, `sla.py`, `cost.py`, `actuation_engine.py`.

### Q42. `ue_control` parameters
From `config_loop.json` read in `xapp_main.py::main`:
- `initial_rate_mbps`: base UE initial rate; used for initial totals, demand defaults, and startup iperf start rates.
- `duration_s`: iperf duration used by startup and actuation traffic restarts.
- `by_operator`: map gNB op_id -> `{pod,port}` used to build `runtime_state["ue_pods"]` and validation that every target operator exists.
- Source: `xapp_main.py::main`.

### Q43. `cap_generator` parameters and behavior impact
Configured in `background_traffic_gnb.json`, consumed by `cap_runtime.py` + `modules/cap_generator.py`:
- Top-level: `seed` or `seeds`, `time_scale`, `scenario`, `ops`, `lam`, `max_step`, `kappa`, `baseline`, `floor_frac`, `bursts`, `upbursts`.
- `ops[op].max_cap/min_cap/init_cap`: absolute cap bounds + initial cap.
- `baseline`: mean-reversion target.
- `floor_frac`: lower cap floor as fraction of max (combined with min_cap).
- `lam`: Poisson intensity for random step magnitude.
- `max_step`: max random step size.
- `kappa`: mean-reversion strength.
- `bursts`: downward burst episodes (rate/duration/depth/recover).
- `upbursts`: upward burst episodes.
- `scenario`: optional phase controller mutating baseline/burst behavior by step index.
- Source: `cap_runtime.py::build_cap_runtime`, `modules/cap_generator.py`.

### Q44. `pricing` section params and formula mapping
- Pricing config is taken from `background_traffic_gnb.json["pricing"]` (fallback to `config_loop.json["pricing"]`) in `xapp_main.py` and loaded via `PriceModel.from_config_dict`.
- Per op parameters: `cap_base`, `nu`, `eps`, `pi_min`, `pi_be`.
- They feed formulas in `PriceModel`:
  - scarcity: `(cap_base / (cap + eps)) ** nu`
  - cost: `scarcity * (pi_min * guaranteed + pi_be * best_effort)`
  - with `guaranteed=min(bmin,used)`, `best_effort=max(0,used-bmin)`, and used clamped to `[0,cap]`.
- Source: `xapp_main.py::main`, `modules/price_model.py`.

---

## Notes on file/logic locations used
Primary files/functions read:
- `xapp_main.py::main`, `_advance_startup_state`, `_send_indication`
- `xapp_runtime/ue_aggregation.py::build_aggregated_ue_sample`
- `xapp_runtime/snapshot.py::_build_tick_snapshot`, `evaluate_snapshot_freshness`
- `xapp_runtime/broker/decision.py::broker_step`
- `xapp_runtime/broker/sla.py::compute_sla_steer_targets`
- `xapp_runtime/broker/cost.py::compute_cost_rebalance_targets`
- `xapp_runtime/broker/gcsa.py::compute_gcsa_moves`
- `xapp_runtime/actuation_engine.py::apply_action_plan`, `_apply_ue_moves`, `_apply_swap_moves`, `_apply_direct_move_with_ghost`, `_refresh_offered_from_ue_state`, `_apply_pricing_to_state`
- `xapp_runtime/cap_runtime.py::build_cap_runtime`, `plan_cap_for_tick`
- `xapp_runtime/gnb_runtime_state.py::update_sample`, `compute_metrics`, `apply_cap`
- `modules/price_model.py::PriceModel`
- config files: `conf/config_loop.json`, `conf/background_traffic_gnb.json`
