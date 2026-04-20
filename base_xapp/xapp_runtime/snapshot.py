"""Snapshot and freshness helpers (Phase-1 pure extraction)."""


def _build_tick_snapshot(*, tick_id: int, tick_ts: float, target_logical_ids: tuple, gnb_states: dict, latest_rows: dict, slice_sla_mbps: float, total_demand_mbps: float) -> dict:
    per_gnb = {}
    total_throughput = 0.0

    for op_id in target_logical_ids:
        st = gnb_states[op_id]
        row = latest_rows.get(op_id, {})
        throughput_mbps = float(row.get("throughput_mbps", 0.0) or 0.0)
        total_throughput += throughput_mbps

        last_seen_ts = getattr(st, "last_seen_ts", None)
        freshness_age_ms = None
        if last_seen_ts is not None:
            freshness_age_ms = max(0.0, (float(tick_ts) - float(last_seen_ts)) * 1000.0)

        eff_override = row.get("gnb_eff_mbps_per_prb_override", None)
        if eff_override is None:
            eff_override = getattr(st, "_eff_override_mbps_per_prb", None)

        per_gnb[op_id] = {
            "throughput_mbps": throughput_mbps,
            "prbs": float(row.get("prbs", getattr(st, "prbs", 0.0)) or 0.0),
            "gnb_eff_mbps_per_prb": float(row.get("gnb_eff_mbps_per_prb", getattr(st, "gnb_eff_mbps_per_prb", 0.0)) or 0.0),
            "gnb_eff_mbps_per_prb_override": (None if eff_override is None else float(eff_override)),
            "cap_effective_prb": float(getattr(st, "cap_effective_prb", 0.0) or 0.0),
            "cost": float(getattr(st, "cost", 0.0) or 0.0),
            "scarcity": float(getattr(st, "scarcity", 0.0) or 0.0),
            "offered_mbps": float(getattr(st, "offered_mbps", 0.0) or 0.0),
            "last_seen_ts": last_seen_ts,
            "freshness_age_ms": freshness_age_ms,
            "state": st,
        }

    return {
        "tick_id": int(tick_id),
        "tick_ts": float(tick_ts),
        "per_gnb": per_gnb,
        "total_throughput": float(total_throughput),
        "slice_sla_mbps": float(slice_sla_mbps),
        "total_demand_mbps": float(total_demand_mbps),
    }


def evaluate_snapshot_freshness(*, snapshot: dict, target_logical_ids: tuple, snapshot_require_all_gnbs: bool, max_freshness_ms: float, seen_ok: bool):
    freshness_ok = True
    freshness_by_gnb = {}
    if snapshot_require_all_gnbs:
        for op_id in target_logical_ids:
            age = snapshot["per_gnb"][op_id].get("freshness_age_ms")
            freshness_by_gnb[op_id] = age
            if age is None or float(age) > max_freshness_ms:
                freshness_ok = False
    valid_snapshot = (seen_ok and freshness_ok) if snapshot_require_all_gnbs else True
    return freshness_ok, freshness_by_gnb, valid_snapshot
