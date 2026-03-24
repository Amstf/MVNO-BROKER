"""Cap planning and traffic actuation helpers (Phase-1 pure extraction)."""

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from xapp_runtime.gnb_runtime_state import GnbState
from modules.price_model import PriceModel
from xapp_utils.control_signaling import send_slice_ctrl
from gnb_utils.ue_management import (
    check_iface_in_pod,
    start_ue_traffic,
    stop_ue_in_pod,
    start_ue_in_pod,
    wait_for_iface_in_pod,
    _clear_ue_ready,
)


PENDING_GHOST_MOVES = {}

_ghost_event_col = None


def set_ghost_event_col(col) -> None:
    global _ghost_event_col
    _ghost_event_col = col


def _log_ghost_event(doc: dict) -> None:
    if _ghost_event_col is None:
        return
    try:
        _ghost_event_col.insert_one(doc)
    except Exception as e:
        print(f"[GHOST-EVENT][MONGO] {e}")


def compute_system_healthy(runtime_state: dict) -> bool:
    """
    Returns True if all active UEs have their RNTI visible
    in the current gNB indication reports.
    Ghost UEs are skipped.
    """
    ue_state = runtime_state.get("ue_state", {})
    ue_role = runtime_state.get("ue_role", {})
    ue_gnb_id = runtime_state.get("ue_gnb_id", {})
    current_rntis = runtime_state.get("current_rntis_by_gnb", {})
    ue_to_rnti = runtime_state.get("ue_to_rnti", {})
    for logical_id, ue_info in ue_state.items():
        role = ue_role.get(logical_id, "active")
        if role == "ghost":
            continue
        gnb_id = ue_gnb_id.get(logical_id) or ue_info.get("gnb_id")
        if not gnb_id:
            continue
        rnti_entry = ue_to_rnti.get(logical_id)
        if not rnti_entry:
            return False
        # ue_to_rnti stores (gnb_id_str, rnti_hex_str) tuples
        rnti_hex = rnti_entry[1] if isinstance(rnti_entry, tuple) else rnti_entry
        # current_rntis_by_gnb stores hex strings (e.g. "0x1a2b")
        gnb_rntis = current_rntis.get(str(gnb_id), set())
        try:
            int(rnti_hex, 16)  # validate parseable hex
        except (ValueError, TypeError):
            return False
        if rnti_hex not in gnb_rntis:
            return False
    return True


def _apply_pricing_to_state(*, gnb_state: GnbState, metrics_row: dict, op_id: str, bmin_by_op: dict, price_model: PriceModel):
    cap = float(getattr(gnb_state, "cap_effective_prb", 0.0) or 0.0)
    used = float(metrics_row.get("prbs", 0.0) or 0.0)
    bmin = float(bmin_by_op.get(op_id, 0.0))

    if cap <= 0.0:
        return
    if op_id not in getattr(price_model, "cfg_by_op", {}):
        return

    price_out = price_model.cost(op_id, cap=cap, bmin=bmin, used=used)
    metrics_row.update(price_out)
    metrics_row["bmin_prb"] = bmin

    for k, v in price_out.items():
        setattr(gnb_state, k, v)
    if hasattr(gnb_state, "bmin_prb"):
        gnb_state.bmin_prb = bmin


def apply_static_action_plan(*, action_plan: dict, runtime_state: dict, control_sck, sst: int, sd: int, logical_to_meid: dict) -> Tuple[dict, dict]:
    slice_ctrl_updates = action_plan.get("slice_ctrl_updates", {})

    last_ue_rates = runtime_state["last_ue_rates"]
    gnb_states = runtime_state["gnb_states"]

    rates_before = {op_id: int(last_ue_rates.get(op_id, 0)) for op_id in last_ue_rates.keys()}

    for op_id, upd in slice_ctrl_updates.items():
        meid = logical_to_meid.get(op_id)
        if not meid:
            continue
        send_slice_ctrl(
            control_sck,
            meid=meid,
            sst=sst,
            sd=sd,
            min_ratio=int(upd["min_ratio"]),
            max_ratio=int(upd["max_ratio"]),
        )

    actuation = {
        "actuated": False,
        "rates_before": rates_before,
        "rates_after": dict(rates_before),
        "restart_tasks": [],
    }

    for op_id, fixed_rate in runtime_state.get("fixed_rates", {}).items():
        last_ue_rates[op_id] = int(round(float(fixed_rate)))
        if op_id in gnb_states:
            gnb_states[op_id].offered_mbps = float(fixed_rate)

    actuation["rates_after"] = {op_id: int(last_ue_rates.get(op_id, 0)) for op_id in last_ue_rates.keys()}
    return runtime_state, actuation


def _refresh_offered_from_ue_state(runtime_state: dict) -> None:
    ue_state = runtime_state.get("ue_state", {})
    gnb_states = runtime_state["gnb_states"]
    last_ue_rates = runtime_state["last_ue_rates"]

    per_gnb = {op_id: 0.0 for op_id in gnb_states.keys()}
    ue_role = runtime_state.get("ue_role", {})
    ue_rate = runtime_state.get("ue_rate", {})
    ue_gnb_id = runtime_state.get("ue_gnb_id", {})

    for ue_id, ue in ue_state.items():
        op_id = str(ue.get("gnb_id", ""))
        rate = float(ue.get("rate_mbps", 0.0) or 0.0)
        if op_id in per_gnb:
            per_gnb[op_id] += rate
        ue_rate[str(ue_id)] = rate
        ue_gnb_id[str(ue_id)] = op_id
        ue_role.setdefault(str(ue_id), "ghost" if op_id == "Ghost_UE" else "active")

    for op_id, total in per_gnb.items():
        last_ue_rates[op_id] = int(round(total))
        gnb_states[op_id].offered_mbps = float(total)

def _apply_swap_moves(*, ue_moves: list, runtime_state: dict) -> Tuple[List, List]:
    ue_profiles = runtime_state.get("ue_profiles", {})
    ue_debug = bool(runtime_state.get("ue_debug", False))
    duration_s = int(runtime_state.get("duration_s", 0))

    by_ue = {m["ue_id"]: m for m in ue_moves if m.get("type") == "swap"}
    visited = set()
    executed = []
    skipped = []

    for ue_id, move in by_ue.items():
        if ue_id in visited:
            continue
        pair = None
        for cand_id, cand in by_ue.items():
            if cand_id == ue_id:
                continue
            if (
                cand.get("from_gnb") == move.get("to_gnb")
                and cand.get("to_gnb") == move.get("from_gnb")
            ):
                pair = cand_id
                break
        if pair is None:
            skipped.append({"move": move, "reason": "swap_pair_not_found"})
            continue

        move_a = move
        move_b = by_ue[pair]
        prof_a = ue_profiles.get(str(move_a["ue_id"]))
        prof_b = ue_profiles.get(str(move_b["ue_id"]))
        if not prof_a or not prof_b:
            skipped.append({"move": move_a, "reason": "missing_profile"})
            skipped.append({"move": move_b, "reason": "missing_profile"})
            continue

        old_a = float(runtime_state.get("ue_state", {}).get(str(move_a["ue_id"]), {}).get("rate_mbps", 0.0) or 0.0)
        old_b = float(runtime_state.get("ue_state", {}).get(str(move_b["ue_id"]), {}).get("rate_mbps", 0.0) or 0.0)

        def _start(profile, rate):
            start_ue_traffic(
                profile["pod"],
                rate_mbps=int(round(float(rate))),
                direction="downlink",
                port=int(profile["port"]),
                duration_s=duration_s,
                ensure_iperf=True,
                quiet=not ue_debug,
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(_start, prof_a, move_a.get("new_rate_mbps", 0.0))
                f2 = ex.submit(_start, prof_b, move_b.get("new_rate_mbps", 0.0))
                f1.result()
                f2.result()
        except Exception:
            # rollback both rates to preserve atomic swap behavior
            with ThreadPoolExecutor(max_workers=2) as ex:
                ex.submit(_start, prof_a, old_a)
                ex.submit(_start, prof_b, old_b)
            skipped.append({"move": move_a, "reason": "swap_failed_rollback"})
            skipped.append({"move": move_b, "reason": "swap_failed_rollback"})
            continue

        ue_rate_map = runtime_state.get("ue_rate", {})
        ue_state = runtime_state.get("ue_state", {})
        for m in (move_a, move_b):
            logical_id = str(m.get("ue_id"))
            new_rate = float(m.get("new_rate_mbps", 0.0) or 0.0)
            ue_rate_map[logical_id] = new_rate
            if logical_id in ue_state:
                ue_state[logical_id]["rate_mbps"] = new_rate

            ue_to_rnti = runtime_state.get("ue_to_rnti", {})
            rnti_entry = ue_to_rnti.get(logical_id)
            if rnti_entry is not None:
                gnb_id_str, rnti_hex_str = rnti_entry
                rnti_int = int(rnti_hex_str, 16)
                ue_demand_state_ref = runtime_state.get("ue_demand_state", {})
                gnb_bucket = ue_demand_state_ref.get(gnb_id_str, {})
                by_rnti = gnb_bucket.get("by_rnti", {})
                by_rnti[rnti_int] = float(new_rate)
                print(
                    f"[SWAP] Updated demand: {logical_id} RNTI {rnti_hex_str} "
                    f"on gNB {gnb_id_str} → {new_rate} Mbps"
                )

        visited.add(ue_id)
        visited.add(pair)
        executed.extend([move_a, move_b])
        if ue_debug:
            print(
                f"[UE-DEBUG][SWAP] {move_a['ue_id']}({move_a['from_gnb']}->{move_a['to_gnb']}) rate={float(move_a['new_rate_mbps']):.2f} "
                f"<-> {move_b['ue_id']}({move_b['from_gnb']}->{move_b['to_gnb']}) rate={float(move_b['new_rate_mbps']):.2f}"
            )

    return executed, skipped

def _apply_direct_move_with_ghost(*, move: dict, runtime_state: dict) -> bool:
    ue_profiles = runtime_state.get("ue_profiles", {})
    ghost_ue_id = runtime_state.get("ghost_ue_id")
    ue_debug = bool(runtime_state.get("ue_debug", False))
    duration_s = int(runtime_state.get("duration_s", 0))

    if not ghost_ue_id:
        return False

    src_ue_id = str(move.get("ue_id"))
    src_prof = ue_profiles.get(src_ue_id)
    ghost_prof = ue_profiles.get(str(ghost_ue_id))
    if not src_prof or not ghost_prof:
        return False

    target_gnb = str(move.get("to_gnb"))
    target_ip = str(runtime_state.get("gnb_ip_map", {}).get(target_gnb, ""))
    if not target_ip:
        return False

    # Bring ghost UE process up on target immediately, then complete move in background.
    start_ue_in_pod(ghost_prof["pod"], target_ip, ghost_prof.get("conf", ""))

    def _complete_direct_move() -> None:
        ghost_move_timeout_s = float(runtime_state.get("ghost_move_timeout_s", 30))
        ghost_retry_wait_s = float(runtime_state.get("ghost_retry_wait_s", 5))

        def wait_for_oaitun(timeout_s: float) -> bool:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if check_iface_in_pod(ghost_prof["pod"], "oaitun_ue1"):
                    return True
                time.sleep(1.0)
            return False

        try:
            if not wait_for_oaitun(ghost_move_timeout_s):
                PENDING_GHOST_MOVES[src_ue_id]["status"] = "timeout_retry"
                print(
                    f"[GHOST-MOVE] {src_ue_id} oaitun timeout, "
                    f"waiting {ghost_retry_wait_s}s then retrying"
                )
                time.sleep(ghost_retry_wait_s)
                _clear_ue_ready(ghost_prof["pod"])
                stop_ue_in_pod(ghost_prof["pod"])
                start_ue_in_pod(ghost_prof["pod"], target_ip, ghost_prof.get("conf", ""))
                if not wait_for_oaitun(ghost_move_timeout_s):
                    PENDING_GHOST_MOVES[src_ue_id]["status"] = "failed"
                    _log_ghost_event({
                        "event":     "ghost_move_failed",
                        "tick_ts":   time.time(),
                        "src_ue_id": src_ue_id,
                        "reason":    "timeout_after_retry",
                    })
                    print(
                        f"[GHOST-MOVE] {src_ue_id} FAILED after retry. "
                        f"Source UE continues unchanged."
                    )
                    stop_ue_in_pod(ghost_prof["pod"])
                    return

            stop_ue_in_pod(src_prof["pod"], direct_move_safe_stop=True)
            _clear_ue_ready(src_prof["pod"])
            start_ue_traffic(
                ghost_prof["pod"],
                rate_mbps=int(round(float(move.get("new_rate_mbps", 0.0)))),
                direction="downlink",
                port=int(ghost_prof["port"]),
                duration_s=duration_s,
                ensure_iperf=True,
                quiet=not ue_debug,
            )

            runtime_state["ghost_ue_id"] = src_ue_id
            src_prof["role"] = "ghost"
            src_prof["gnb_id"] = "Ghost_UE"
            src_prof["gnb_ip"] = target_ip

            ghost_prof["role"] = "active"
            ghost_prof["gnb_id"] = target_gnb
            ghost_prof["gnb_ip"] = target_ip

            ue_role = runtime_state.get("ue_role", {})
            ue_gnb_id = runtime_state.get("ue_gnb_id", {})
            ue_rate = runtime_state.get("ue_rate", {})
            moved_ue_rate = float(ue_rate.get(src_ue_id, move.get("new_rate_mbps", 0.0)) or 0.0)
            ue_role[src_ue_id] = "ghost"
            ue_gnb_id[src_ue_id] = "Ghost_UE"
            ue_rate[src_ue_id] = 0.0
            ue_role[str(ghost_ue_id)] = "active"
            ue_gnb_id[str(ghost_ue_id)] = target_gnb
            ue_rate[str(ghost_ue_id)] = float(move.get("new_rate_mbps", 0.0) or 0.0)

            ue_state = runtime_state.get("ue_state", {})
            if src_ue_id in ue_state:
                ue_state[src_ue_id]["gnb_id"] = "Ghost_UE"
                ue_state[src_ue_id]["rate_mbps"] = 0.0
            ghost_key = str(ghost_ue_id)
            if ghost_key in ue_state:
                ue_state[ghost_key]["gnb_id"] = target_gnb
                ue_state[ghost_key]["rate_mbps"] = float(move.get("new_rate_mbps", 0.0) or 0.0)

            ue_to_rnti = runtime_state.get("ue_to_rnti", {})
            old_entry = ue_to_rnti.get(src_ue_id)
            if old_entry is not None:
                old_gnb_str, old_rnti_hex = old_entry
                old_rnti_int = int(old_rnti_hex, 16)
                ue_demand_state_ref = runtime_state.get("ue_demand_state", {})
                old_bucket = ue_demand_state_ref.get(old_gnb_str, {})
                old_bucket.get("by_rnti", {}).pop(old_rnti_int, None)
                runtime_state.get("rnti_to_ue", {}).pop((old_gnb_str, old_rnti_hex), None)
                del runtime_state["ue_to_rnti"][src_ue_id]
                print(
                    f"[GHOST-MOVE] Removed old RNTI mapping: {src_ue_id} "
                    f"{old_rnti_hex} on gNB {old_gnb_str}"
                )

            target_gnb_str = str(target_gnb)
            ghost_logical_id = str(ghost_ue_id)
            detected_new_rnti_hex = None
            baseline = set(
                (PENDING_GHOST_MOVES.get(src_ue_id, {}) or {}).get("rntis_before", set())
            )
            deadline = time.time() + 15.0
            while time.time() < deadline:
                current_set = set(
                    runtime_state.get("current_rntis_by_gnb", {}).get(target_gnb_str, set())
                )
                new_rntis = current_set - baseline
                if new_rntis:
                    detected_new_rnti_hex = next(iter(new_rntis))
                    break
                time.sleep(1.0)

            if detected_new_rnti_hex is not None:
                new_rnti_int = int(detected_new_rnti_hex, 16)
                runtime_state["rnti_to_ue"][(target_gnb_str, detected_new_rnti_hex)] = ghost_logical_id
                runtime_state["ue_to_rnti"][ghost_logical_id] = (target_gnb_str, detected_new_rnti_hex)

                ue_demand_state_ref = runtime_state.get("ue_demand_state", {})
                target_bucket = ue_demand_state_ref.setdefault(
                    target_gnb_str,
                    {"rates": [], "next_idx": 0, "by_rnti": {}},
                )
                target_bucket["by_rnti"][new_rnti_int] = float(moved_ue_rate)

                print(
                    f"[GHOST-MOVE] Ghost {ghost_logical_id} new RNTI {detected_new_rnti_hex} "
                    f"on gNB {target_gnb_str} demand={moved_ue_rate} Mbps"
                )
            else:
                print(
                    f"[GHOST-MOVE][WARN] Could not detect new RNTI for {ghost_logical_id} "
                    f"on gNB {target_gnb_str} after 15s"
                )

            if src_ue_id in PENDING_GHOST_MOVES:
                PENDING_GHOST_MOVES[src_ue_id]["status"] = "done"
                _log_ghost_event({
                    "event":      "ghost_move_done",
                    "tick_ts":    time.time(),
                    "src_ue_id":  src_ue_id,
                    "target_gnb": target_gnb,
                    "duration_s": time.time() - PENDING_GHOST_MOVES.get(
                                      src_ue_id, {}).get("started_at", time.time()),
                })
        except Exception:
            if src_ue_id in PENDING_GHOST_MOVES:
                PENDING_GHOST_MOVES[src_ue_id]["status"] = "failed"
            traceback.print_exc()

    t = threading.Thread(target=_complete_direct_move, daemon=True)
    PENDING_GHOST_MOVES[src_ue_id] = {
        "thread": t,
        "status": "pending",
        "target_gnb": target_gnb,
        "ghost_ue_id": str(ghost_ue_id),
        "new_rate_mbps": float(move.get("new_rate_mbps", 0.0) or 0.0),
        "started_at": time.time(),
        "rntis_before": set(runtime_state.get("current_rntis_by_gnb", {}).get(target_gnb, set())),
    }
    _log_ghost_event({
        "event":         "ghost_move_start",
        "tick_ts":       time.time(),
        "src_ue_id":     src_ue_id,
        "ghost_ue_id":   str(ghost_ue_id),
        "target_gnb":    target_gnb,
        "new_rate_mbps": float(move.get("new_rate_mbps", 0.0)),
    })
    t.start()
    print(f"[UE-DEBUG][DIRECT-GHOST][PENDING] src={src_ue_id} ghost={ghost_ue_id} target_gnb={target_gnb}")
    return True


def _apply_ue_moves(*, action_plan: dict, runtime_state: dict) -> Tuple[List, List]:
    ue_moves = list(action_plan.get("ue_moves") or [])
    if not ue_moves:
        return [], [{"reason": "no_ue_moves"}]

    executed = []
    skipped = []

    swap_executed, swap_skipped = _apply_swap_moves(ue_moves=ue_moves, runtime_state=runtime_state)
    executed.extend(swap_executed)
    skipped.extend(swap_skipped)

    for move in ue_moves:
        if move.get("type") != "direct":
            continue
        src_ue_id = str(move.get("ue_id"))
        if src_ue_id in PENDING_GHOST_MOVES:
            skipped.append({"move": move, "reason": "ghost_move_in_progress"})
            continue
        if _apply_direct_move_with_ghost(move=move, runtime_state=runtime_state):
            executed.append(move)
        else:
            skipped.append({"move": move, "reason": "direct_move_failed"})

    _refresh_offered_from_ue_state(runtime_state)
    return executed, skipped

def apply_action_plan(*, action_plan: dict, runtime_state: dict, control_sck, sst: int, sd: int, logical_to_meid: dict) -> Tuple[dict, dict]:
    for src_ue_id, info in list(PENDING_GHOST_MOVES.items()):
        status = info.get("status")
        elapsed = time.time() - float(info.get("started_at", time.time()))
        if status == "pending":
            print(
                f"[UE-DEBUG][GHOST-MOVE][PENDING] src={src_ue_id} "
                f"target_gnb={info['target_gnb']} elapsed={elapsed:.1f}s"
            )
        elif status == "done":
            print(
                f"[UE-DEBUG][GHOST-MOVE][DONE] src={src_ue_id} "
                f"target_gnb={info['target_gnb']} elapsed={elapsed:.1f}s"
            )
            del PENDING_GHOST_MOVES[src_ue_id]
        elif status == "failed":
            print(
                f"[UE-DEBUG][GHOST-MOVE][FAILED] src={src_ue_id} "
                f"target_gnb={info['target_gnb']} elapsed={elapsed:.1f}s"
            )
            del PENDING_GHOST_MOVES[src_ue_id]

    slice_ctrl_updates = action_plan.get("slice_ctrl_updates", {})

    last_ue_rates = runtime_state["last_ue_rates"]
    gnb_states = runtime_state["gnb_states"]

    rates_before = {op_id: int(last_ue_rates.get(op_id, 0)) for op_id in last_ue_rates.keys()}

    for op_id, upd in slice_ctrl_updates.items():
        meid = logical_to_meid.get(op_id)
        if not meid:
            continue
        send_slice_ctrl(
            control_sck,
            meid=meid,
            sst=sst,
            sd=sd,
            min_ratio=int(upd["min_ratio"]),
            max_ratio=int(upd["max_ratio"]),
        )

    actuation = {
        "actuated": False,
        "rates_before": rates_before,
        "rates_after": dict(rates_before),
        "restart_tasks": [],
    }

    ue_debug = bool(runtime_state.get("ue_debug", False))
    ue_moves = list(action_plan.get("ue_moves") or [])
    if ue_debug:
        if ue_moves:
            print(f"[UE-DEBUG][GCSA-PLAN] {ue_moves}")
        else:
            print("[UE-DEBUG][GCSA-PLAN] no ue_moves")

    executed, skipped = _apply_ue_moves(action_plan=action_plan, runtime_state=runtime_state)
    if ue_debug:
        if executed:
            print(f"[UE-DEBUG][GCSA-EXECUTED] {executed}")
        if skipped:
            print(f"[UE-DEBUG][GCSA-SKIPPED] {skipped}")

    if executed:
        actuation["actuated"] = True
    actuation["restart_tasks"] = executed

    actuation["rates_after"] = {op_id: int(last_ue_rates.get(op_id, 0)) for op_id in last_ue_rates.keys()}
    return runtime_state, actuation


def _apply_eff_override_to_states(gnb_states: dict, eff_override: Optional[float]) -> None:
    if eff_override is not None:
        for st in gnb_states.values():
            st.gnb_eff_mbps_per_prb = float(eff_override)
            if hasattr(st, "_eff_mbps_per_prb_hist"):
                st._eff_mbps_per_prb_hist.append(float(eff_override))
            st._eff_override_mbps_per_prb = float(eff_override)
    else:
        for st in gnb_states.values():
            if hasattr(st, "_eff_override_mbps_per_prb"):
                st._eff_override_mbps_per_prb = None
