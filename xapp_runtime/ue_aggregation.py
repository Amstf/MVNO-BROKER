"""Per-indication UE aggregation helpers."""

from types import SimpleNamespace

from xapp_runtime.gnb_runtime_state import GnbState


def build_aggregated_ue_sample(*, op_id: str, ue_info_list: list, now: float, gnb_state: GnbState, ue_counter_state: dict, agg_counter_state: dict, ue_demand_state: dict, default_ue_rate: float, rnti_to_ue: dict = None, ue_rate_map: dict = None):
    per_ue = ue_counter_state.setdefault(op_id, {})
    agg = agg_counter_state.setdefault(op_id, {"mac_total": 0.0, "pdcp_total": 0.0})

    total_tp_mbps = 0.0
    total_gp_mbps = 0.0
    total_prbs = 0.0
    max_tbs_per_prb = 0.0

    bler_weighted = 0.0
    mcs_weighted = 0.0
    weight_sum = 0.0

    demand_state = ue_demand_state.setdefault(op_id, {"rates": [], "next_idx": 0, "by_rnti": {}})
    ue_debug_rows = []

    current_rntis = set()

    for ue in ue_info_list:
        rnti = int(getattr(ue, "rnti", 0) or 0)
        current_rntis.add(rnti)

        mac_total = float(getattr(ue, "dl_total_bytes", 0.0) or 0.0)
        pdcp_total = float(getattr(ue, "dl_pdcp_sdu_bytes", 0.0) or 0.0)
        prbs = max(0.0, float(getattr(ue, "avg_prbs_dl", 0.0) or 0.0))
        tbs_prb = max(0.0, float(getattr(ue, "avg_tbs_per_prb_dl", 0.0) or 0.0))
        dl_bler = float(getattr(ue, "dl_bler", 0.0) or 0.0)
        dl_mcs = float(getattr(ue, "dl_mcs", 0.0) or 0.0)

        rates = demand_state.get("rates", [])
        by_rnti = demand_state.setdefault("by_rnti", {})
        if rnti not in by_rnti:
            rnti_hex = hex(rnti)
            logical_id = None
            if rnti_to_ue is not None:
                logical_id = rnti_to_ue.get((op_id, rnti_hex))
            if logical_id is not None and ue_rate_map is not None:
                by_rnti[rnti] = float(ue_rate_map.get(logical_id, default_ue_rate))
            else:
                by_rnti[rnti] = 0.0
        ue_demand = float(by_rnti.get(rnti, default_ue_rate))

        total_prbs += prbs
        if tbs_prb > max_tbs_per_prb:
            max_tbs_per_prb = tbs_prb

        w = prbs if prbs > 0.0 else 1.0
        bler_weighted += dl_bler * w
        mcs_weighted += dl_mcs * w
        weight_sum += w

        prev = per_ue.get(rnti)
        per_ue[rnti] = {"mac": mac_total, "pdcp": pdcp_total, "ts": now}
        if prev is None:
            continue

        dt = now - float(prev.get("ts", now))
        if dt <= 0.0:
            continue

        dmac = mac_total - float(prev.get("mac", 0.0))
        dpdcp = pdcp_total - float(prev.get("pdcp", 0.0))
        if dmac < 0.0:
            dmac = 0.0
        if dpdcp < 0.0:
            dpdcp = 0.0

        ue_tp_mbps = (dmac * 8.0) / (dt * 1e6)
        ue_gp_mbps = (dpdcp * 8.0) / (dt * 1e6)
        total_tp_mbps += ue_tp_mbps
        total_gp_mbps += ue_gp_mbps

        ue_debug_rows.append({
            "gnb_id": str(op_id),
            "rnti": rnti,
            "throughput_mbps": float(ue_tp_mbps),
            "goodput_mbps": float(ue_gp_mbps),
            "demand_mbps": float(ue_demand),
            "gap_mbps": float(ue_tp_mbps - ue_demand),
            "prbs": float(prbs),
            "tbs_per_prb": float(tbs_prb),
            "dl_bler": float(dl_bler),
            "dl_mcs": float(dl_mcs),
        })

    stale = [r for r in per_ue.keys() if r not in current_rntis]
    for rnti in stale:
        per_ue.pop(rnti, None)

    prev_ts = getattr(gnb_state, "_curr_ts", None)
    gnb_dt = (now - float(prev_ts)) if prev_ts is not None else 0.0
    if gnb_dt > 0.0:
        agg["mac_total"] += (total_tp_mbps * 1e6 * gnb_dt) / 8.0
        agg["pdcp_total"] += (total_gp_mbps * 1e6 * gnb_dt) / 8.0

    avg_bler = (bler_weighted / weight_sum) if weight_sum > 0.0 else 0.0
    avg_mcs = (mcs_weighted / weight_sum) if weight_sum > 0.0 else 0.0

    aggregated_sample = SimpleNamespace(
        dl_pdcp_sdu_bytes=float(agg["pdcp_total"]),
        dl_total_bytes=float(agg["mac_total"]),
        avg_prbs_dl=float(total_prbs),
        avg_tbs_per_prb_dl=float(max_tbs_per_prb),
        dl_bler=float(avg_bler),
        dl_mcs=float(avg_mcs),
    )

    return aggregated_sample, ue_debug_rows
