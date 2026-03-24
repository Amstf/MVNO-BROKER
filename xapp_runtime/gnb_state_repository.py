# gnb_state_mongo.py
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from pymongo import ASCENDING, MongoClient


# ---------------- Mongo init ----------------

def init_gnb_state_collection(
    client: Optional[MongoClient] = None,
    db_name: str = "Paper1",
    col_name: str = "gnb_state",
) -> Tuple[MongoClient, Any]:
    mongo_client = client
    if mongo_client is None:
        uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI (or MONGO_URI) must be set for Mongo writing")
        mongo_client = MongoClient(uri)

    col = mongo_client[db_name][col_name]

    # Minimal indexes for fast filtering + ordering
    col.create_index([("gnb_id", ASCENDING), ("report_index", ASCENDING)])
    col.create_index([("meid", ASCENDING), ("report_index", ASCENDING)])
    col.create_index([("report_index", ASCENDING), ("_id", ASCENDING)])
    col.create_index([("gnb_id", ASCENDING), ("tick_id", ASCENDING)])
    col.create_index([("tick_id", ASCENDING), ("_id", ASCENDING)])

    return mongo_client, col


# ---------------- helpers ----------------

def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
        if v != v:  # NaN
            return None
        if v in (float("inf"), float("-inf")):
            return None
        return v
    except Exception:
        return None


def build_gnb_state_doc(
    gnb_state: Any,
    metrics_row: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Minimal gnb_state doc (NO timestamp field).

    Stores:
      - gnb identity + ordering: gnb_id, meid, report_index, tick_id
      - cell_total_prbs, cap_effective_prb, cap_ratio
      - computed metrics: goodput/throughput/prbs/eff
      - pricing abstraction: bmin_prb, scarcity, costs, guaranteed/best-effort split
    """
    return {
        "gnb_id": str(getattr(gnb_state, "gnb_id", metrics_row.get("gnb_id", ""))),
        "meid": getattr(gnb_state, "meid", None),
        "report_index": int(getattr(gnb_state, "report_index", 0)),
        "tick_id": int(getattr(gnb_state, "tick_id", metrics_row.get("tick_id", 0)) or 0),
        "tick_ts": _safe_float(getattr(gnb_state, "tick_ts", metrics_row.get("tick_ts"))),

        # static-ish context (handy for later analysis)
        "cell_total_prbs": _safe_float(getattr(gnb_state, "cell_total_prbs", None)),

        # cap context
        "cap_effective_prb": _safe_float(getattr(gnb_state, "cap_effective_prb", None)),
        "cap_ratio": _safe_float(getattr(gnb_state, "cap_ratio", metrics_row.get("cap_ratio"))),

        # metrics
        "goodput_mbps": _safe_float(metrics_row.get("goodput")),
        "throughput_mbps": _safe_float(metrics_row.get("throughput")),
        "prbs": _safe_float(metrics_row.get("prbs")),
        "gnb_eff": _safe_float(metrics_row.get("gnb_eff")),
        "gnb_eff_mbps_per_prb": _safe_float(metrics_row.get("gnb_eff_mbps_per_prb")),
        "our_eff": _safe_float(metrics_row.get("our_eff")),
        "dl_bler": _safe_float(metrics_row.get("dl_bler", getattr(gnb_state, "dl_bler", None))),
        "dl_mcs": _safe_float(metrics_row.get("dl_mcs", getattr(gnb_state, "dl_mcs", None))),

        # steering outputs
        "steering_target_mbps": _safe_float(metrics_row.get("steering_target_mbps")),
        "steering_deficit_mbps": _safe_float(metrics_row.get("steering_deficit_mbps")),
        "steering_expected_gain_mbps": _safe_float(metrics_row.get("steering_expected_gain_mbps")),
        "steering_role": metrics_row.get("steering_role"),
        "steering_sla_violated": metrics_row.get("steering_sla_violated"),
        "steering_total_demand_mbps": _safe_float(metrics_row.get("steering_total_demand_mbps")),
        "steering_gap_mbps": _safe_float(metrics_row.get("steering_gap_mbps")),
        "steering_active": metrics_row.get("steering_active"),
        "steering_traffic_delta_mbps": _safe_float(metrics_row.get("steering_traffic_delta_mbps")),
        "offered_mbps": _safe_float(metrics_row.get("offered_mbps")),

        # pricing / cost abstraction (NEW)
        "bmin_prb": _safe_float(getattr(gnb_state, "bmin_prb", metrics_row.get("bmin_prb"))),
        "scarcity": _safe_float(getattr(gnb_state, "scarcity", metrics_row.get("scarcity"))),

        # Costs: current, and bounds (min only / max if saturated)
        "cost": _safe_float(getattr(gnb_state, "cost", metrics_row.get("cost"))),
        "cost_min": _safe_float(getattr(gnb_state, "cost_min", metrics_row.get("cost_min"))),
        "cost_max": _safe_float(getattr(gnb_state, "cost_max", metrics_row.get("cost_max"))),

        # Used PRB split (guaranteed vs best-effort)
        "guaranteed_prb": _safe_float(getattr(gnb_state, "guaranteed_prb", metrics_row.get("guaranteed_prb"))),
        "best_effort_prb": _safe_float(getattr(gnb_state, "best_effort_prb", metrics_row.get("best_effort_prb"))),
    }


def mongo_insert_one(col: Any, doc: Dict[str, Any]) -> None:
    if col is None or not doc:
        return
    try:
        col.insert_one(doc)
    except Exception as exc:
        print(f"[GNB_STATE][MONGO][ERROR] Failed to insert gnb_state doc: {exc}")
