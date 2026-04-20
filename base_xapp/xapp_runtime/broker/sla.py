"""SLA steering logic."""


def _fmt(d: dict) -> str:
    return ", ".join(f"{k}={float(v):.2f}" for k, v in sorted(d.items()))



def compute_sla_steer_targets(snapshot: dict, policy_cfg: dict) -> dict:
    per_gnb = snapshot["per_gnb"]
    op_ids = list(per_gnb.keys())
    offered = {op_id: float(per_gnb[op_id].get("offered_mbps", 0.0)) for op_id in op_ids}
    delivered = {op_id: float(per_gnb[op_id].get("throughput_mbps", 0.0)) for op_id in op_ids}
    ue_debug = bool(policy_cfg.get("ue_debug", False))

    deficits = {op_id: max(0.0, offered[op_id] - delivered[op_id]) for op_id in op_ids}
    overloaded_id = max(deficits, key=deficits.get)
    overloaded_def = float(deficits[overloaded_id])

    tolerance = float(policy_cfg.get("steering_tolerance", 0.1))
    trigger = abs(tolerance * offered[overloaded_id])

    if ue_debug:
        print(
            f"[UE-DEBUG][SLA] offered=[{_fmt(offered)}] delivered=[{_fmt(delivered)}] "
            f"deficits=[{_fmt(deficits)}] overloaded={overloaded_id} overloaded_def={overloaded_def:.2f} "
            f"tol={tolerance:.3f} trigger={trigger:.2f}"
        )

    targets = dict(offered)
    if overloaded_def <= trigger:
        if ue_debug:
            print("[UE-DEBUG][SLA] hold: overloaded deficit below trigger")
        return targets

    headroom_mbps = {}
    for op_id, g in per_gnb.items():
        used_prbs = max(0.0, float(g.get("prbs", 0.0) or 0.0))
        throughput = max(0.0, float(g.get("throughput_mbps", 0.0) or 0.0))
        st = g.get("state")
        eff_override = g.get("gnb_eff_mbps_per_prb_override")
        if eff_override is None:
            eff_override = getattr(st, "_eff_override_mbps_per_prb", None)
        if eff_override is not None:
            eff = float(eff_override)
        else:
            eff = throughput / max(1e-6, used_prbs)
        eff = max(0.05, min(eff, 3.0))
        cap_prbs = float(g.get("cap_effective_prb", 0.0) or 0.0)
        headroom_prbs = max(0.0, cap_prbs - used_prbs) if cap_prbs > 0.0 else 0.0
        headroom_mbps[op_id] = headroom_prbs * eff

    absorber_candidates = [op for op in op_ids if op != overloaded_id]
    if not absorber_candidates:
        return targets
    absorber_id = max(absorber_candidates, key=lambda op: headroom_mbps.get(op, 0.0))
    absorber_head = float(headroom_mbps.get(absorber_id, 0.0))
    if absorber_head <= 0.0:
        return targets

    shift = min(overloaded_def, absorber_head)
    if shift <= 0.0:
        return targets

    targets[overloaded_id] = max(0.0, offered[overloaded_id] - shift)
    targets[absorber_id] = max(0.0, offered[absorber_id] + shift)
    if ue_debug:
        print(
            f"[UE-DEBUG][SLA] absorber={absorber_id} absorber_head_mbps={absorber_head:.2f} "
            f"shift={shift:.2f} targets=[{_fmt(targets)}]"
        )
    return targets
