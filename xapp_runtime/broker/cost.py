"""Cost rebalancing logic.

Implements economically-driven load balancing using:
  ρ(t) = max(0, (c⁻ - c⁺) / c⁻)              [normalized savings ratio]
  M_econ(t) = ρ(t) × L⁻                       [economic desire to move]
  allowed_headroom = H⁺_mbps                  [coarse cap from cheap-side planner headroom]
  Δ_cost(t) = min(M_econ(t), allowed_headroom) [final shift amount]

where:
  c⁻ = cost_per_mbps of expensive domain
  c⁺ = cost_per_mbps of cheap domain
  L⁻ = current load on expensive domain
  H⁺_mbps = payload-safe headroom on cheap domain converted to Mbps
"""

import logging

logger = logging.getLogger(__name__)

ATTACH_PRB_RESERVE_PER_UE = 5.0


def _fmt_map(d: dict) -> str:
    return ", ".join(f"{k}={float(v):.2f}" for k, v in sorted(d.items()))


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


def _attached_ues_from_policy(op_id: str, g: dict, policy_cfg: dict) -> float:
    attached_map = policy_cfg.get("attached_ues_by_gnb", {}) or {}
    if str(op_id) in attached_map:
        return max(0.0, float(attached_map.get(str(op_id), 0.0) or 0.0))
    raw = g.get("attached_ues", None)
    if raw is None:
        raw = g.get("num_ues", 0.0)
    return max(0.0, float(raw or 0.0))


def _usable_headroom_prbs(g: dict, attached_ues: float) -> float:
    cap_prbs = max(0.0, float(g.get("cap_effective_prb", 0.0) or 0.0))
    traffic_used_prbs = max(0.0, float(g.get("prbs", 0.0) or 0.0))
    attached_ues = max(0.0, float(attached_ues or 0.0))
    reserved_existing_prbs = attached_ues * ATTACH_PRB_RESERVE_PER_UE
    reserved_incoming_prbs = ATTACH_PRB_RESERVE_PER_UE
    usable_headroom_prbs = (
        cap_prbs - traffic_used_prbs - reserved_existing_prbs - reserved_incoming_prbs
    )
    return max(0.0, usable_headroom_prbs)


def compute_cost_rebalance_targets(snapshot: dict, policy_cfg: dict) -> dict:
    """Compute coarse load-shift targets to reduce cost (final PRB admission is in GCSA)."""
    per_gnb = snapshot["per_gnb"]
    offered = {op_id: float(g.get("offered_mbps", 0.0) or 0.0) for op_id, g in per_gnb.items()}
    ue_debug = bool(policy_cfg.get("ue_debug", False))

    # ──────────────────────────────────────────────────────────────────────
    # Step 1: Calculate cost per unit efficiency for each domain
    # ──────────────────────────────────────────────────────────────────────
    cost_eff = {}
    for op_id, g in per_gnb.items():
        used_prbs = max(0.0, float(g.get("prbs", 0.0) or 0.0))
        if used_prbs <= 0.0:
            continue
        eff = _effective_eff(g)
        cost = float(g.get("cost", 0.0) or 0.0)
        if cost <= 0.0:
            continue
        cost_eff[op_id] = cost / eff

    if ue_debug:
        for op_id, g in per_gnb.items():
            eff_dbg = _effective_eff(g)
            raw_prb_dbg = max(0.0, float(g.get("prbs", 0.0) or 0.0))
            attached_dbg = _attached_ues_from_policy(op_id, g, policy_cfg)
            used_total_dbg = raw_prb_dbg + ATTACH_PRB_RESERVE_PER_UE * attached_dbg
            print(
                f"[UE-DEBUG][COST] op={op_id} cost={float(g.get('cost', 0.0) or 0.0):.4f} "
                f"eff={eff_dbg:.3f} raw_prbs={raw_prb_dbg:.2f} attached_ues={attached_dbg:.1f} "
                f"used_total_prbs={used_total_dbg:.2f}"
            )

    if len(cost_eff) < 2:
        return dict(offered)

    # ──────────────────────────────────────────────────────────────────────
    # Step 2: Identify expensive (source) and cheap (target) domains
    # ──────────────────────────────────────────────────────────────────────
    expensive_id = max(cost_eff, key=cost_eff.get)
    feasible_cheap = []

    for op_id in cost_eff.keys():
        if op_id == expensive_id:
            continue
        usable_headroom = _usable_headroom_prbs(per_gnb[op_id], _attached_ues_from_policy(op_id, per_gnb[op_id], policy_cfg))
        if usable_headroom > 0.0:
            feasible_cheap.append((op_id, usable_headroom))

    if not feasible_cheap:
        return dict(offered)

    cheap_id, cheap_usable_headroom_prbs = min(
        feasible_cheap, key=lambda x: (cost_eff.get(x[0], float("inf")), -x[1])
    )

    # ──────────────────────────────────────────────────────────────────────
    # Step 3: Calculate normalized savings ratio ρ
    # ──────────────────────────────────────────────────────────────────────
    expensive_val = float(cost_eff.get(expensive_id, 0.0))
    cheap_val = float(cost_eff.get(cheap_id, 0.0))
    if expensive_val <= 0.0:
        return dict(offered)

    rho = max(0.0, (expensive_val - cheap_val) / expensive_val)

    # ──────────────────────────────────────────────────────────────────────
    # Step 4: Compute economic desire M_econ = ρ × L⁻
    # ──────────────────────────────────────────────────────────────────────
    expensive_load = float(offered.get(expensive_id, 0.0))
    m_econ = rho * expensive_load

    # ──────────────────────────────────────────────────────────────────────
    # Step 5: Compute allowed headroom from payload-safe cheap-side PRBs
    # ──────────────────────────────────────────────────────────────────────
    cheap_g = per_gnb[cheap_id]
    cheap_eff = _effective_eff(cheap_g)
    headroom_usage_ratio = float(policy_cfg.get("cost_headroom_usage_ratio", 0.9) or 0.9)
    headroom_usage_ratio = max(0.0, min(1.0, headroom_usage_ratio))
    allowed_headroom_mbps = cheap_usable_headroom_prbs * cheap_eff * headroom_usage_ratio

    # ──────────────────────────────────────────────────────────────────────
    # Step 6: Final cost-phase shift Δ_cost = min(M_econ, allowed_headroom)
    # ──────────────────────────────────────────────────────────────────────
    delta_cost = min(m_econ, allowed_headroom_mbps)

    cap_prbs = max(0.0, float(cheap_g.get("cap_effective_prb", 0.0) or 0.0))
    traffic_used_prbs = max(0.0, float(cheap_g.get("prbs", 0.0) or 0.0))
    attached_ues = _attached_ues_from_policy(cheap_id, cheap_g, policy_cfg)
    reserved_existing_prbs = attached_ues * ATTACH_PRB_RESERVE_PER_UE
    reserved_incoming_prbs = ATTACH_PRB_RESERVE_PER_UE

    logger.debug(
        "cost: exp=%s cheap=%s cost_eff=[%.3f,%.3f] rho=%.4f "
        "L⁻=%.1f cap_prb=%.1f used_prb=%.1f attached_ues=%.1f "
        "reserved_existing=%.1f reserved_incoming=%.1f usable_prb=%.1f "
        "eff=%.3f allowed=%.1f m_econ=%.1f Δ=%.1f",
        expensive_id,
        cheap_id,
        expensive_val,
        cheap_val,
        rho,
        expensive_load,
        cap_prbs,
        traffic_used_prbs,
        attached_ues,
        reserved_existing_prbs,
        reserved_incoming_prbs,
        cheap_usable_headroom_prbs,
        cheap_eff,
        allowed_headroom_mbps,
        m_econ,
        delta_cost,
    )
    if ue_debug:
        print(
            f"[UE-DEBUG][COST] expensive={expensive_id} cheap={cheap_id} rho={rho:.4f} "
            f"m_econ={m_econ:.2f} headroom_usage_ratio={headroom_usage_ratio:.2f} "
            f"allowed_headroom_mbps={allowed_headroom_mbps:.2f} delta_cost={delta_cost:.2f}"
        )

    if delta_cost < 0.01:
        return dict(offered)

    # ──────────────────────────────────────────────────────────────────────
    # Step 7: Update load targets
    # ──────────────────────────────────────────────────────────────────────
    targets = dict(offered)
    targets[expensive_id] = max(1.0, targets[expensive_id] - delta_cost)
    targets[cheap_id] = max(1.0, targets[cheap_id] + delta_cost)

    logger.debug(
        "cost: targets exp=%s→%.1f cheap=%s→%.1f",
        expensive_id,
        targets[expensive_id],
        cheap_id,
        targets[cheap_id],
    )
    if ue_debug:
        print(f"[UE-DEBUG][COST] desired_targets=[{_fmt_map(targets)}]")

    return targets
