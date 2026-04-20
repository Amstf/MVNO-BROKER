"""Greedy capacity-safe UE steering assignment (GCSA)."""

import logging

logger = logging.getLogger(__name__)

ATTACH_PRB_RESERVE_PER_UE = 5.0

def _effective_eff(g: dict) -> float:
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
    return max(0.05, min(eff, 3.0))


def compute_gcsa_moves(ue_state, desired_rates, policy_cfg, per_gnb=None):
    """Compute UE direct/swap moves to realize desired per-gNB rates."""
    if not ue_state:
        return []

    per_gnb = per_gnb or {}
    ue_debug = bool(policy_cfg.get("ue_debug", False))
    max_direct_moves = max(0, int(policy_cfg.get("max_direct_moves_per_decision", 1) or 0))
    prefer_swap_first = bool(policy_cfg.get("prefer_swap_first", False))

    attached_ues = {
        op_id: max(0.0, float((per_gnb.get(op_id, {}) or {}).get("attached_ues", 0.0) or 0.0))
        for op_id in desired_rates
    }
    projected_prbs = {
        op_id: (
            max(0.0, float((per_gnb.get(op_id, {}) or {}).get("prbs", 0.0) or 0.0))
            + ATTACH_PRB_RESERVE_PER_UE * attached_ues[op_id]
        )
        for op_id in desired_rates
    }
    cap_prbs = {
        op_id: max(0.0, float((per_gnb.get(op_id, {}) or {}).get("cap_effective_prb", 0.0) or 0.0))
        for op_id in desired_rates
    }
    eff_mbps_per_prb = {
        op_id: _effective_eff(per_gnb.get(op_id, {}) or {})
        for op_id in desired_rates
    }

    L = {}
    gap = {}

    for op_id in desired_rates:
        L[op_id] = sum(
            float(ue.get("rate_mbps", 0.0) or 0.0)
            for ue in ue_state.values()
            if str(ue.get("gnb_id")) == str(op_id)
        )
        gap[op_id] = float(desired_rates.get(op_id, 0.0) or 0.0) - L[op_id]
        logger.debug(
            "gcsa step1 op_id=%s L=%.2f desired=%.2f gap=%.2f",
            op_id,
            L[op_id],
            float(desired_rates.get(op_id, 0.0) or 0.0),
            gap[op_id],
        )

    if all(abs(float(g)) < 0.1 for g in gap.values()):
        return []

    sources = sorted([d for d in gap if gap[d] < -0.1], key=lambda d: abs(gap[d]), reverse=True)
    targets = [d for d in gap if gap[d] > 0.1]

    def _apply_direct_once():
        for d_minus in sources:
            ues_at_source = sorted(
                [uid for uid, u in ue_state.items() if str(u.get("gnb_id")) == str(d_minus)],
                key=lambda uid: float(ue_state[uid].get("rate_mbps", 0.0) or 0.0),
                reverse=True,
            )
            for ue_id in ues_at_source:
                if abs(gap[d_minus]) < 0.1:
                    break
                r_u = float(ue_state[ue_id].get("rate_mbps", 0.0) or 0.0)
                for d_plus in sorted(targets, key=lambda d: gap[d], reverse=True):
                    if not (r_u <= gap[d_plus] and r_u <= abs(gap[d_minus])):
                        continue

                    g_target = per_gnb.get(d_plus)
                    payload_prbs_needed = 0.0
                    if g_target is not None:
                        eff = eff_mbps_per_prb[d_plus]
                        payload_prbs_needed = r_u / max(1e-6, eff)
                        free_prbs = max(0.0, cap_prbs[d_plus] - projected_prbs[d_plus])
                        raw_prbs = max(0.0, float((g_target or {}).get("prbs", 0.0) or 0.0))
                        att = attached_ues.get(d_plus, 0.0)
                        reserve_need = ATTACH_PRB_RESERVE_PER_UE
                        if ue_debug:
                            print(
                                f"[UE-DEBUG][GCSA-CANDIDATE] type=direct src={d_minus} tgt={d_plus} ue={ue_id} "
                                f"rate={r_u:.2f} gap_src={gap[d_minus]:.2f} gap_tgt={gap[d_plus]:.2f} "
                                f"cap_prbs={cap_prbs[d_plus]:.2f} raw_prbs={raw_prbs:.2f} attached_ues={att:.1f} "
                                f"used_total_prbs={projected_prbs[d_plus]:.2f} free_prbs={free_prbs:.2f} eff={eff:.3f} "
                                f"payload_prbs_needed={payload_prbs_needed:.2f} reserve_prbs_needed={reserve_need:.2f}"
                            )
                        if free_prbs < payload_prbs_needed + reserve_need:
                            if ue_debug:
                                print(
                                    f"[UE-DEBUG][GCSA-REJECT] type=direct ue={ue_id} src={d_minus} tgt={d_plus} reason=insufficient_prbs"
                                )
                            continue

                    move = {
                        "ue_id": ue_id,
                        "from_gnb": d_minus,
                        "to_gnb": d_plus,
                        "new_rate_mbps": r_u,
                        "type": "direct",
                    }
                    gap[d_plus] -= r_u
                    gap[d_minus] += r_u

                    if per_gnb.get(d_plus) is not None:
                        projected_prbs[d_plus] += payload_prbs_needed + ATTACH_PRB_RESERVE_PER_UE
                    if per_gnb.get(d_minus) is not None:
                        eff_src = eff_mbps_per_prb[d_minus]
                        projected_prbs[d_minus] = max(
                            0.0,
                            projected_prbs[d_minus]
                            - ((r_u / max(1e-6, eff_src)) + ATTACH_PRB_RESERVE_PER_UE),
                        )

                    ue_state[move["ue_id"]]["gnb_id"] = move["to_gnb"]
                    ue_state[move["ue_id"]]["rate_mbps"] = float(move["new_rate_mbps"])
                    if ue_debug:
                        print(
                            f"[UE-DEBUG][GCSA-ACCEPT] type=direct ue={ue_id} src={d_minus} tgt={d_plus} "
                            f"proj_src_prbs={projected_prbs.get(d_minus, 0.0):.2f} proj_tgt_prbs={projected_prbs.get(d_plus, 0.0):.2f} "
                            f"gap_src={gap[d_minus]:.2f} gap_tgt={gap[d_plus]:.2f}"
                        )
                    return [move]
        return []

    def _apply_swap_once():
        for d_minus in [d for d in gap if gap[d] < -0.1]:
            if abs(gap[d_minus]) < 0.1:
                continue
            for d_plus in [d for d in gap if gap[d] > 0.1]:
                if gap[d_plus] < 0.1:
                    continue

                ues_at_source = [
                    uid for uid, u in ue_state.items()
                    if str(u.get("gnb_id")) == str(d_minus)
                ]
                ues_at_target = [
                    uid for uid, u in ue_state.items()
                    if str(u.get("gnb_id")) == str(d_plus)
                ]

                best_pair = None
                best_residual = float("inf")

                for u_id in ues_at_source:
                    for v_id in ues_at_target:
                        if u_id == v_id:
                            continue
                        r_u = float(ue_state[u_id].get("rate_mbps", 0.0) or 0.0)
                        r_v = float(ue_state[v_id].get("rate_mbps", 0.0) or 0.0)
                        net = r_u - r_v
                        if net <= 0.0:
                            continue
                        if not (net <= gap[d_plus] and net <= abs(gap[d_minus])):
                            continue

                        if per_gnb.get(d_plus) is not None and per_gnb.get(d_minus) is not None:
                            eff_plus = eff_mbps_per_prb[d_plus]
                            eff_minus = eff_mbps_per_prb[d_minus]
                            delta_plus_prb = net / max(1e-6, eff_plus)
                            delta_minus_prb = -net / max(1e-6, eff_minus)
                            new_plus = projected_prbs[d_plus] + delta_plus_prb
                            new_minus = projected_prbs[d_minus] + delta_minus_prb
                            if ue_debug:
                                print(
                                    f"[UE-DEBUG][GCSA-CANDIDATE] type=swap src={d_minus} tgt={d_plus} ue={u_id} pair_ue={v_id} "
                                    f"rate={r_u:.2f} pair_rate={r_v:.2f} gap_src={gap[d_minus]:.2f} gap_tgt={gap[d_plus]:.2f} "
                                    f"cap_prbs_tgt={cap_prbs[d_plus]:.2f} raw_prbs_tgt={float((per_gnb.get(d_plus,{}) or {}).get('prbs',0.0) or 0.0):.2f} "
                                    f"attached_ues_tgt={attached_ues.get(d_plus,0.0):.1f} used_total_prbs_tgt={projected_prbs[d_plus]:.2f} "
                                    f"free_prbs_tgt={max(0.0, cap_prbs[d_plus]-projected_prbs[d_plus]):.2f} eff_tgt={eff_plus:.3f} "
                                    f"payload_prbs_needed={delta_plus_prb:.2f} reserve_prbs_needed=0.00"
                                )
                            if new_plus > cap_prbs[d_plus] or new_minus > cap_prbs[d_minus] or new_plus < 0.0 or new_minus < 0.0:
                                if ue_debug:
                                    print(
                                        f"[UE-DEBUG][GCSA-REJECT] type=swap ue={u_id} pair_ue={v_id} src={d_minus} tgt={d_plus} reason=capacity_violation"
                                    )
                                continue

                        residual = abs(gap[d_plus] - net)
                        if residual < best_residual:
                            best_residual = residual
                            best_pair = (u_id, v_id, net)

                if best_pair is not None:
                    u_id, v_id, net = best_pair
                    r_u = float(ue_state[u_id].get("rate_mbps", 0.0) or 0.0)
                    r_v = float(ue_state[v_id].get("rate_mbps", 0.0) or 0.0)

                    move_a = {
                        "ue_id": u_id,
                        "from_gnb": d_minus,
                        "to_gnb": d_plus,
                        "new_rate_mbps": r_v,
                        "type": "swap",
                    }
                    move_b = {
                        "ue_id": v_id,
                        "from_gnb": d_plus,
                        "to_gnb": d_minus,
                        "new_rate_mbps": r_u,
                        "type": "swap",
                    }

                    gap[d_plus] -= net
                    gap[d_minus] += net

                    if per_gnb.get(d_plus) is not None and per_gnb.get(d_minus) is not None:
                        eff_plus = eff_mbps_per_prb[d_plus]
                        eff_minus = eff_mbps_per_prb[d_minus]
                        projected_prbs[d_plus] = max(0.0, projected_prbs[d_plus] + (net / max(1e-6, eff_plus)))
                        projected_prbs[d_minus] = max(0.0, projected_prbs[d_minus] - (net / max(1e-6, eff_minus)))

                    ue_state[move_a["ue_id"]]["rate_mbps"] = float(move_a["new_rate_mbps"])
                    ue_state[move_b["ue_id"]]["rate_mbps"] = float(move_b["new_rate_mbps"])

                    if ue_debug:
                        print(
                            f"[UE-DEBUG][GCSA-ACCEPT] type=swap ue={u_id} pair_ue={v_id} src={d_minus} tgt={d_plus} "
                            f"proj_src_prbs={projected_prbs.get(d_minus, 0.0):.2f} proj_tgt_prbs={projected_prbs.get(d_plus, 0.0):.2f} "
                            f"gap_src={gap[d_minus]:.2f} gap_tgt={gap[d_plus]:.2f}"
                        )
                    return [move_a, move_b]
        return []

    def _gap_score() -> float:
        return float(sum(abs(float(v)) for v in gap.values()))

    def _snapshot_runtime_state():
        return (
            dict(gap),
            dict(projected_prbs),
            {uid: dict(u) for uid, u in ue_state.items()},
        )

    def _restore_runtime_state(snapshot):
        gap_snapshot, projected_snapshot, ue_snapshot = snapshot
        gap.clear()
        gap.update(gap_snapshot)
        projected_prbs.clear()
        projected_prbs.update(projected_snapshot)
        ue_state.clear()
        ue_state.update({uid: dict(u) for uid, u in ue_snapshot.items()})

    all_moves = []
    direct_used = max_direct_moves <= 0
    max_iterations = max(1, len(ue_state) * 2)

    for _ in range(max_iterations):
        baseline_snapshot = _snapshot_runtime_state()
        baseline_score = _gap_score()

        if direct_used:
            direct_moves = []
            direct_score = baseline_score
            direct_snapshot = baseline_snapshot
        else:
            _restore_runtime_state(baseline_snapshot)
            direct_moves = _apply_direct_once()
            direct_score = _gap_score() if direct_moves else baseline_score
            direct_snapshot = _snapshot_runtime_state()

        _restore_runtime_state(baseline_snapshot)
        swap_moves = _apply_swap_once()
        swap_score = _gap_score() if swap_moves else baseline_score
        swap_snapshot = _snapshot_runtime_state()

        if direct_moves and swap_moves:
            use_direct = False if prefer_swap_first else (direct_score <= swap_score)
        elif direct_moves:
            use_direct = True
        elif swap_moves:
            use_direct = False
        else:
            _restore_runtime_state(baseline_snapshot)
            break

        if use_direct:
            _restore_runtime_state(direct_snapshot)
            chosen_moves = direct_moves
            chosen_score = direct_score
            chosen_kind = "direct"
            max_direct_moves -= 1
            direct_used = max_direct_moves <= 0
        else:
            _restore_runtime_state(swap_snapshot)
            chosen_moves = swap_moves
            chosen_score = swap_score
            chosen_kind = "swap"

        if chosen_score >= baseline_score:
            _restore_runtime_state(baseline_snapshot)
            break

        all_moves.extend(chosen_moves)

        if ue_debug:
            print(
                f"[UE-DEBUG][GCSA-ACCEPT] iter_choice={chosen_kind} "
                f"iter_moves={chosen_moves} score_before={baseline_score:.2f} score_after={chosen_score:.2f}"
            )

        if _gap_score() < 0.1:
            break

    if ue_debug:
        print(f"[UE-DEBUG][GCSA-ACCEPT] final_moves={all_moves}")
    return all_moves
