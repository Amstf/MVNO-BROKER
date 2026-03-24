# modules/price_model.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any


@dataclass(frozen=True)
class PricingCfg:
    # scarcity model
    cap_base: float          # Cap_d^base
    nu: float = 1.0          # ν_d
    eps: float = 1e-6        # ε

    # tier prices
    pi_min: float = 1.0      # π_d^min
    pi_be: float = 0.5       # π_d^be  (<= pi_min)


class PriceModel:
    """
    Implements the paper:

      s_d(t) = ( Cap_base / (cap(t) + eps) )^nu

      C(t) = s_d(t) * [ pi_min * Bmin + pi_be * max(0, used - Bmin) ]

    Also exposes "bounds":
      - cost_min = cost if used=Bmin (guaranteed only)
      - cost_max = cost if used=cap  (use everything offered)
    """

    def __init__(self, cfg_by_op: Dict[str, PricingCfg]):
        self.cfg_by_op = cfg_by_op

    def scarcity(self, op_id: str, cap: float) -> float:
        cfg = self.cfg_by_op[op_id]
        return float((cfg.cap_base / (float(cap) + cfg.eps)) ** cfg.nu)

    def cost(self, op_id: str, *, cap: float, bmin: float, used: float) -> Dict[str, float]:
        cfg = self.cfg_by_op[op_id]
        cap = float(cap)
        bmin = float(bmin)
        used = float(used)

        # clamp used to feasible [0, cap] (important)
        if used < 0:
            used = 0.0
        if used > cap:
            used = cap

        s = self.scarcity(op_id, cap)

        guaranteed = min(bmin, used)
        best_effort = max(0.0, used - bmin)

        c = s * (cfg.pi_min * guaranteed + cfg.pi_be * best_effort)

        # bounds for plotting / broker input
        cost_min = s * (cfg.pi_min * min(bmin, cap))              # used=bmin (or cap if cap<bmin)
        cost_max = s * (cfg.pi_min * min(bmin, cap) + cfg.pi_be * max(0.0, cap - bmin))  # used=cap

        return {
            "scarcity": s,
            "cost": c,
            "cost_min": cost_min,
            "cost_max": cost_max,
            "guaranteed_prb": guaranteed,
            "best_effort_prb": best_effort,
        }

    @staticmethod
    def from_config_dict(pricing_cfg: Dict[str, Any]) -> "PriceModel":
        ops_cfg = {}
        for op_id, d in (pricing_cfg.get("ops") or {}).items():
            ops_cfg[op_id] = PricingCfg(
                cap_base=float(d["cap_base"]),
                nu=float(d.get("nu", 1.0)),
                eps=float(d.get("eps", 1e-6)),
                pi_min=float(d.get("pi_min", 1.0)),
                pi_be=float(d.get("pi_be", 0.5)),
            )
        return PriceModel(ops_cfg)
