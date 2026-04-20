"""Broker decision ladder: three-phase state machine (observe → decide → frozen)."""

from typing import Tuple

from xapp_runtime.actuation_engine import PENDING_GHOST_MOVES
from xapp_runtime.broker.sla import compute_sla_steer_targets
from xapp_runtime.broker.cost import compute_cost_rebalance_targets
from xapp_runtime.broker.gcsa import compute_gcsa_moves


def _fmt_rates(rate_map: dict) -> str:
    return ", ".join(f"{k}={float(v):.2f}" for k, v in sorted(rate_map.items()))


def broker_step(
    *,
    snapshot: dict,
    policy_cfg: dict,
    policy_state: dict,
    valid_snapshot: bool,
    placements: list,
    system_healthy: bool = True,
) -> Tuple[dict, dict]:
    """Single decision authority per tick: three-phase state machine."""
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

    # ── STEP 0: initialize state collections ─────────────────────────────────
    state = policy_state if policy_state is not None else {}

    if "ue_state" not in state:
        state["ue_state"] = {
            str(pl.get("logical_id") or pl["pod"]): {
                "gnb_id": str(pl["gnb_id"]),
                "rate_mbps": float(pl.get("initial_rate_mbps", 0.0)),
            }
            for pl in (placements or [])
            if "pod" in pl and "gnb_id" in pl
        }

    if "ue_role" not in state:
        state["ue_role"] = {
            str(pl.get("logical_id") or pl["pod"]): str(
                pl.get("role", "ghost" if str(pl.get("gnb_id")) == "Ghost_UE" else "active")
            ).lower()
            for pl in (placements or [])
            if "pod" in pl and "gnb_id" in pl
        }
    if "ue_rate" not in state:
        state["ue_rate"] = {
            str(pl.get("logical_id") or pl["pod"]): float(pl.get("initial_rate_mbps", 0.0))
            for pl in (placements or [])
            if "pod" in pl and "gnb_id" in pl
        }
    if "ue_gnb_id" not in state:
        state["ue_gnb_id"] = {
            str(pl.get("logical_id") or pl["pod"]): str(pl.get("gnb_id"))
            for pl in (placements or [])
            if "pod" in pl and "gnb_id" in pl
        }

    if "broker_phase" not in state:
        state["broker_phase"] = "observe"
    if "observe_ticks" not in state:
        state["observe_ticks"] = 0
    if "direct_move_cooldown_ticks_left" not in state:
        state["direct_move_cooldown_ticks_left"] = 0

    # ── GATE 1: data quality ──────────────────────────────────────────────────
    if not valid_snapshot:
        action_plan["reason"] = "invalid_snapshot"
        return action_plan, state

    cooldown_left = int(state.get("direct_move_cooldown_ticks_left", 0) or 0)
    if cooldown_left > 0:
        state["direct_move_cooldown_ticks_left"] = cooldown_left - 1

    # ── GATE 2: system health ─────────────────────────────────────────────────
    if not system_healthy:
        action_plan["reason"] = "frozen_recovery"
        return action_plan, state

    # ── GATE 3: startup guard (unchanged — Step 3 removes this) ──────────────
    report_count = int(state.get("report_count", 0))
    startup_no_steer_reports = int(policy_cfg.get("startup_no_steer_reports", 0))
    if report_count < startup_no_steer_reports:
        action_plan["reason"] = "hold_startup_no_steer"
        action_plan["desired_rates"] = dict(current_offered)
        return action_plan, state

    # ── GATE 4: frozen phase ──────────────────────────────────────────────────
    broker_phase = state.get("broker_phase", "observe")

    if broker_phase == "frozen":
        ghost_still_pending = any(
            info.get("status") not in ("done", "failed")
            for info in PENDING_GHOST_MOVES.values()
        )
        if ghost_still_pending:
            action_plan["reason"] = "frozen_ghost_pending"
            return action_plan, state
        # ghost done (or no ghost move was started) → open observe window
        state["broker_phase"] = "observe"
        state["observe_ticks"] = 0
        action_plan["reason"] = "frozen_stabilizing"
        return action_plan, state

    # ── GATE 5: post-move dwell (observe-only) ───────────────────────────────
    dwell_left = int(state.get("post_move_dwell_ticks", 0) or 0)
    if dwell_left > 0:
        state["post_move_dwell_ticks"] = dwell_left - 1
        state["broker_phase"] = "observe"
        action_plan["reason"] = "observing_dwell"
        return action_plan, state

    # ── GATE 6: observe phase ─────────────────────────────────────────────────
    if broker_phase == "observe":
        state["observe_ticks"] = state.get("observe_ticks", 0) + 1
        observe_window = int(policy_cfg.get("observe_window_ticks", 3))
        if state["observe_ticks"] < observe_window:
            action_plan["reason"] = "observing"
            return action_plan, state
        # window complete → fall through to decide immediately
        state["broker_phase"] = "decide"

    # ── GATE 6: decide phase ──────────────────────────────────────────────────
    total_throughput = float(snapshot.get("total_throughput", 0.0))
    sla = float(snapshot.get("slice_sla_mbps", 0.0))
    deficit = max(0.0, sla - total_throughput)

    # update streak counter (metrics/logging only — no longer a gate)
    if deficit == 0.0:
        state["sla_ok_tick_streak"] = state.get("sla_ok_tick_streak", 0) + 1
    else:
        state["sla_ok_tick_streak"] = 0

    ue_state = state.get("ue_state", {})
    ue_role = state.get("ue_role", {})
    ue_gnb_id = state.get("ue_gnb_id", {})
    attached_ues_by_gnb = {str(op_id): 0 for op_id in snapshot.get("per_gnb", {}).keys()}
    for ue_id in ue_state.keys():
        role = str(ue_role.get(ue_id, "active")).lower()
        if role == "ghost":
            continue
        gnb_id = str(ue_gnb_id.get(ue_id, ue_state.get(ue_id, {}).get("gnb_id", "")))
        if gnb_id in attached_ues_by_gnb:
            attached_ues_by_gnb[gnb_id] += 1

    for op_id, g in snapshot.get("per_gnb", {}).items():
        g["attached_ues"] = int(attached_ues_by_gnb.get(str(op_id), 0))

    ue_debug = bool(policy_cfg.get("ue_debug", False))
    if ue_debug:
        print(
            f"[UE-DEBUG][BROKER] phase={state.get('broker_phase')} total={total_throughput:.2f} "
            f"sla={sla:.2f} deficit={deficit:.2f} offered=[{_fmt_rates(current_offered)}]"
        )

    sla_only = bool(policy_cfg.get("sla_only", False))
    if deficit > 0.0 or sla_only:
        desired_rates = compute_sla_steer_targets(snapshot, policy_cfg)
        gcsa_policy_cfg = dict(policy_cfg)
        gcsa_policy_cfg["prefer_swap_first"] = False
        if int(state.get("direct_move_cooldown_ticks_left", 0) or 0) > 0:
            gcsa_policy_cfg["max_direct_moves_per_decision"] = 0
        ue_moves = compute_gcsa_moves(
            ue_state=state["ue_state"],
            desired_rates=desired_rates,
            policy_cfg=gcsa_policy_cfg,
            per_gnb=snapshot.get("per_gnb", {}),
        )
        reason = "sla_steer" if deficit > 0.0 else "sla_only_hold"
    else:
        cost_policy_cfg = dict(policy_cfg)
        cost_policy_cfg["attached_ues_by_gnb"] = dict(attached_ues_by_gnb)
        desired_rates = compute_cost_rebalance_targets(snapshot, cost_policy_cfg)
        gcsa_policy_cfg = dict(policy_cfg)
        gcsa_policy_cfg["prefer_swap_first"] = True
        if int(state.get("direct_move_cooldown_ticks_left", 0) or 0) > 0:
            gcsa_policy_cfg["max_direct_moves_per_decision"] = 0
        ue_moves = compute_gcsa_moves(
            ue_state=state["ue_state"],
            desired_rates=desired_rates,
            policy_cfg=gcsa_policy_cfg,
            per_gnb=snapshot.get("per_gnb", {}),
        )
        reason = "cost_rebalance"

    if ue_debug:
        print(
            f"[UE-DEBUG][BROKER] branch={reason} desired=[{_fmt_rates(desired_rates)}]"
        )

    action_plan["reason"] = reason
    action_plan["desired_rates"] = desired_rates
    action_plan["ue_moves"] = ue_moves

    direct_cooldown_ticks = max(0, int(policy_cfg.get("direct_move_cooldown_ticks", 3) or 3))
    if any(str(m.get("type", "")) == "direct" for m in (ue_moves or [])):
        state["direct_move_cooldown_ticks_left"] = direct_cooldown_ticks

    if ue_moves:
        # action taken → freeze until execution completes
        state["broker_phase"] = "frozen"
        state["post_move_dwell_ticks"] = max(
            int(state.get("post_move_dwell_ticks", 0) or 0),
            int(policy_cfg.get("post_move_dwell_ticks", 2) or 2),
        )
    else:
        # no moves found → restart observe window fresh
        state["broker_phase"] = "observe"
        state["observe_ticks"] = 0
        action_plan["reason"] = reason + "_no_moves"

    return action_plan, state
