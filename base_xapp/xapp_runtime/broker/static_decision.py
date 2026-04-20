"""Static-mode decision helper."""

from typing import Tuple


def static_broker_step(*, snapshot: dict, policy_state: dict, valid_snapshot: bool) -> Tuple[dict, dict]:
    op_ids = list(snapshot.get("per_gnb", {}).keys())
    current_offered = {
        op_id: float(snapshot["per_gnb"][op_id].get("offered_mbps", 0.0) or 0.0)
        for op_id in op_ids
    }
    action_plan = {
        "reason": "hold",
        "desired_rates": dict(current_offered),
        "slice_ctrl_updates": {},
    }
    state = dict(policy_state or {})
    if not valid_snapshot:
        action_plan["reason"] = "invalid_snapshot"
        return action_plan, state
    action_plan["reason"] = "static_no_steer"
    if "fixed_rates" in state:
        action_plan["desired_rates"] = {
            op_id: float(state["fixed_rates"].get(op_id, current_offered.get(op_id, 0.0)))
            for op_id in op_ids
        }
    return action_plan, state
