"""Mongo and decision-document helpers (Phase-1 pure extraction)."""

import xapp_runtime.gnb_state_repository as gnb_state_mongo


def _fallback_init_gnb_state_collection(client=None, db_name="Paper1", col_name="gnb_state"):
    raise RuntimeError(
        "gnb_state_mongo.init_gnb_state_collection is missing in this runtime; "
        "please sync gnb_state_mongo.py on target host"
    )


def _fallback_mongo_insert_one(col, doc):
    if col is None or not doc:
        return
    try:
        col.insert_one(doc)
    except Exception as exc:
        print(f"[GNB_STATE][MONGO][ERROR] Failed to insert gnb_state doc: {exc}")


init_gnb_state_collection = getattr(
    gnb_state_mongo, "init_gnb_state_collection", _fallback_init_gnb_state_collection
)
mongo_insert_one = getattr(gnb_state_mongo, "mongo_insert_one", _fallback_mongo_insert_one)


def build_gnb_state_doc(gnb_state, metrics_row):
    fn = getattr(gnb_state_mongo, "build_gnb_state_doc", None)
    if callable(fn):
        return fn(gnb_state, metrics_row)
    return {
        "gnb_id": str(getattr(gnb_state, "gnb_id", metrics_row.get("gnb_id", ""))),
        "meid": getattr(gnb_state, "meid", None),
        "report_index": int(getattr(gnb_state, "report_index", 0)),
        "tick_id": int(metrics_row.get("tick_id", getattr(gnb_state, "tick_id", 0)) or 0),
        "tick_ts": metrics_row.get("tick_ts", getattr(gnb_state, "tick_ts", None)),
        "offered_mbps": metrics_row.get("offered_mbps"),
        "throughput_mbps": metrics_row.get("throughput_mbps", metrics_row.get("throughput")),
        "prbs": metrics_row.get("prbs"),
    }


def _build_decision_doc(
    *,
    tick_id: int,
    valid_snapshot: bool,
    snapshot: dict,
    action_plan: dict,
    actuation: dict,
    policy_state: dict = None,
    system_healthy: bool = True,
) -> dict:
    summary = {}
    for op_id, g in snapshot.get("per_gnb", {}).items():
        summary[op_id] = {
            "throughput_mbps": float(g.get("throughput_mbps", 0.0) or 0.0),
            "prbs": float(g.get("prbs", 0.0) or 0.0),
            "cap_effective_prb": float(g.get("cap_effective_prb", 0.0) or 0.0),
            "cost": float(g.get("cost", 0.0) or 0.0),
            "offered_mbps": float(g.get("offered_mbps", 0.0) or 0.0),
            "freshness_age_ms": g.get("freshness_age_ms"),
            "last_seen_ts": g.get("last_seen_ts"),
            "scarcity": float(g.get("scarcity", 0.0) or 0.0),
        }

    ue_state_raw = (policy_state or {}).get("ue_state", {})
    ue_snapshot = {
        ue_id: {
            "gnb_id":    str(info.get("gnb_id", "")),
            "rate_mbps": float(info.get("rate_mbps", 0.0) or 0.0),
        }
        for ue_id, info in ue_state_raw.items()
    }

    return {
        "tick_id": int(tick_id),
        "tick_ts": float(snapshot.get("tick_ts", 0.0) or 0.0),
        "valid_snapshot": bool(valid_snapshot),
        "broker_phase": (policy_state or {}).get("broker_phase", "unknown"),
        "system_healthy": bool(system_healthy),
        "ue_state": ue_snapshot,
        "ue_moves": action_plan.get("ue_moves", []),
        "snapshot": {
            "total_throughput": float(snapshot.get("total_throughput", 0.0) or 0.0),
            "slice_sla_mbps": float(snapshot.get("slice_sla_mbps", 0.0) or 0.0),
            "total_demand_mbps": float(snapshot.get("total_demand_mbps", 0.0) or 0.0),
            "per_gnb": summary,
        },
        "decision_reason": action_plan.get("reason"),
        "desired_rates": action_plan.get("desired_rates", {}),
        "slice_ctrl_updates": action_plan.get("slice_ctrl_updates", {}),
        "rates_before": actuation.get("rates_before", {}),
        "rates_after": actuation.get("rates_after", {}),
        "actuated": bool(actuation.get("actuated", False)),
        "restart_tasks": actuation.get("restart_tasks", []),
    }
