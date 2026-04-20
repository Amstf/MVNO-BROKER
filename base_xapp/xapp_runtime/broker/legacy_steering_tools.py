"""Legacy steering helpers kept outside GnbState for optional analysis/testing.

These helpers are not used by the active xapp_main broker path.
"""

from typing import Dict, Optional


def _window_mean(values, n: int) -> float:
    if n <= 0 or not values:
        return 0.0
    if len(values) <= n:
        return float(sum(values) / len(values))
    return float(sum(list(values)[-n:]) / n)


def get_window_metrics(state, n: int) -> Dict[str, float]:
    return {
        "throughput_mbps": _window_mean(getattr(state, "_throughput_hist", []), n),
        "prbs": _window_mean(getattr(state, "_prbs_hist", []), n),
        "gnb_eff_mbps_per_prb": _window_mean(getattr(state, "_eff_mbps_per_prb_hist", []), n),
    }


def estimate_prb_allocation(
    *,
    target_mbps: float,
    throughput_mbps: float,
    bmin_prb: float,
    cap_effective_prb: float,
    mbps_per_prb: float,
) -> float:
    gap = max(0.0, float(target_mbps) - float(throughput_mbps))
    eff = max(0.0, float(mbps_per_prb))
    needed_prbs = 0.0 if eff <= 0.0 else gap / eff
    alloc = max(float(bmin_prb), float(bmin_prb) + needed_prbs)
    if cap_effective_prb > 0:
        alloc = min(float(cap_effective_prb), alloc)
    return alloc


def set_bmin(state, bmin_prb: float) -> None:
    """Legacy helper kept outside GnbState."""
    state.bmin_prb = max(0.0, float(bmin_prb))


def apply_pricing_outputs(
    state,
    *,
    scarcity: float,
    cost: float,
    cost_min: float,
    cost_max: float,
    guaranteed_prb: float,
    best_effort_prb: float,
) -> None:
    """Legacy helper kept outside GnbState."""
    state.scarcity = float(scarcity)
    state.cost = float(cost)
    state.cost_min = float(cost_min)
    state.cost_max = float(cost_max)
    state.guaranteed_prb = float(guaranteed_prb)
    state.best_effort_prb = float(best_effort_prb)


def compute_traffic_steering(
    states: Dict[str, object],
    *,
    total_demand_mbps: float,
    slice_sla_mbps: float,
    tolerance: float,
    step_mbps: float,
    window_n: Optional[int] = None,
    abs_trigger_mbps: float = 0.75,
    debug: bool = False,
) -> Dict[str, float]:
    op_ids = list(states.keys())
    if not op_ids:
        return {}

    n_ops = len(op_ids)
    equal_share = float(total_demand_mbps) / n_ops
    targets = {
        op_id: float(getattr(states[op_id], "offered_mbps", equal_share) or equal_share)
        for op_id in op_ids
    }

    def _metrics(st) -> Dict[str, float]:
        if window_n is None:
            return {
                "throughput_mbps": float(getattr(st, "throughput_mbps", 0.0)),
                "prbs": float(getattr(st, "prbs", 0.0)),
                "gnb_eff_mbps_per_prb": float(getattr(st, "gnb_eff_mbps_per_prb", 0.0)),
            }
        return get_window_metrics(st, window_n)

    total_kpi = sum(_metrics(st)["throughput_mbps"] for st in states.values())
    deficit_total = max(0.0, float(slice_sla_mbps) - float(total_kpi))

    for st in states.values():
        st.steering_deficit_mbps = 0.0
        st.steering_expected_gain_mbps = 0.0
        st.steering_role = "none"
        st.steering_sla_violated = deficit_total > 0.0
        st.steering_total_demand_mbps = float(total_demand_mbps)
        st.steering_gap_mbps = 0.0
        st.steering_active = False
        st.steering_traffic_delta_mbps = 0.0
        st.steering_target_mbps = float(getattr(st, "offered_mbps", equal_share) or equal_share)

    delivered: Dict[str, float] = {}
    offered: Dict[str, float] = {}
    deficit: Dict[str, float] = {}
    for op_id, st in states.items():
        t = _metrics(st)["throughput_mbps"]
        off = float(getattr(st, "offered_mbps", equal_share) or equal_share)
        delivered[op_id] = float(t)
        offered[op_id] = float(off)
        d = max(0.0, off - t)
        deficit[op_id] = float(d)
        st.steering_gap_mbps = float(t - off)
        st.steering_target_mbps = float(off)

    overloaded_id = max(deficit, key=deficit.get)
    overloaded_def = float(deficit[overloaded_id])

    trigger = max(float(abs_trigger_mbps), abs(float(tolerance) * offered[overloaded_id]))

    if debug:
        print("[STEER DEBUG] offered/throughput/deficit:", {op: (offered[op], delivered[op], deficit[op]) for op in op_ids})
        print("[STEER DEBUG] total_kpi/sla/def_total:", (total_kpi, float(slice_sla_mbps), deficit_total))
        print("[STEER DEBUG] overloaded/def/trigger:", (overloaded_id, overloaded_def, trigger))

    if deficit_total <= 0.0 or overloaded_def <= trigger:
        return targets

    headroom_mbps: Dict[str, float] = {}
    for op_id, st in states.items():
        m = _metrics(st)
        t = max(0.0, float(m["throughput_mbps"]))
        used_prbs = max(0.0, float(m["prbs"]))
        eff_override = getattr(st, "_eff_override_mbps_per_prb", None)
        eff = float(eff_override) if eff_override is not None else t / max(1e-6, used_prbs)
        eff = max(0.05, min(eff, 3.0))
        cap_prbs = float(getattr(st, "cap_effective_prb", 0.0) or 0.0)
        headroom_prbs = max(0.0, cap_prbs - used_prbs) if cap_prbs > 0.0 else 0.0
        headroom_mbps[op_id] = headroom_prbs * eff

    absorber_candidates = [op for op in op_ids if op != overloaded_id]
    if not absorber_candidates:
        return targets
    absorber_id = max(absorber_candidates, key=lambda op: headroom_mbps.get(op, 0.0))
    absorber_head = float(headroom_mbps.get(absorber_id, 0.0))
    if absorber_head <= 0.0:
        return targets

    shift = max(0.0, min(float(step_mbps), overloaded_def, absorber_head))
    if shift <= 0.0:
        return targets

    targets[overloaded_id] = max(0.0, offered[overloaded_id] - shift)
    targets[absorber_id] = max(0.0, offered[absorber_id] + shift)

    states[overloaded_id].steering_deficit_mbps = overloaded_def
    states[overloaded_id].steering_role = "overloaded"
    states[overloaded_id].steering_expected_gain_mbps = shift
    states[overloaded_id].steering_active = True
    states[absorber_id].steering_role = "absorber"
    states[absorber_id].steering_expected_gain_mbps = shift
    states[absorber_id].steering_active = True

    for op_id in op_ids:
        st = states[op_id]
        st.steering_target_mbps = targets[op_id]
        st.steering_traffic_delta_mbps = targets[op_id] - float(getattr(st, "offered_mbps", equal_share) or equal_share)

    return targets
