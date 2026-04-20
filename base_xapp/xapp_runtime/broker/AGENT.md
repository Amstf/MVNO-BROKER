# Broker runtime logic (current)

This folder contains the dynamic broker steering pipeline in strict phases:

1. `sla.py`
   - Computes per-gNB `desired_rates` in Mbps under SLA-deficit conditions.
   - Uses snapshot throughput/offered/PRB context and CAP-limited headroom.
   - **Output:** only `desired_rates`.

2. `cost.py`
   - Computes per-gNB `desired_rates` in Mbps under no-deficit cost rebalance.
   - Uses cost-per-efficiency comparison with hysteresis/warmup constraints.
   - **Output:** only `desired_rates`.

3. `gcsa.py`
   - Converts per-gNB `desired_rates` + `ue_state` into UE-level actions:
     - Pass-1 direct move candidates
     - Pass-2 swap augmentation
   - `gap[op_id] = desired_rates[op_id] - L[op_id]`, where `L` is sum of UE rates currently assigned to that gNB.
   - Mutates `ue_state` in-place to represent the planned assignment/rate after selected moves.
   - **Output:** `ue_moves` list with `type in {direct, swap}`.

4. `decision.py`
   - Single broker entrypoint (`broker_step`).
   - Preserves decision ladder order:
     - invalid snapshot -> hold startup -> SLA steer -> cost warmup -> SLA warmup -> cost rebalance
   - Initializes persistent policy maps from placements on first run:
     - `ue_state`, `ue_role`, `ue_rate`, `ue_gnb_id`
   - Calls GCSA only when reason is `sla_steer` or `cost_rebalance`.
   - **Output:** action plan with gNB-level desired rates and UE-level `ue_moves`.

## Separation of responsibilities

- SLA/Cost modules never perform UE assignment.
- GCSA never computes SLA/Cost policy.
- Decision module orchestrates branch selection and combines outputs.
- Execution of `ue_moves` is handled by runtime actuation layer (`xapp_runtime/actuation_engine.py`).
