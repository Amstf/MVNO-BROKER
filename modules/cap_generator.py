from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import math
import numpy as np


@dataclass(frozen=True)
class BurstCfg:
    burst_rate: float = 0.03
    dur_min: int = 6
    dur_max: int = 18
    depth_min: int = 6
    depth_max: int = 16
    recover: bool = True


@dataclass
class _BurstInstance:
    duration: int
    depth: int
    recover: bool
    age: int = 0


@dataclass
class _OpState:
    max_cap: int
    min_cap_cfg: int
    baseline: int
    floor_frac: float
    lam: float
    max_step: int
    kappa: float
    cap: int
    bursts_cfg: Optional[BurstCfg]
    upbursts_cfg: Optional[BurstCfg]
    active_bursts: List[_BurstInstance]
    active_upbursts: List[_BurstInstance]


class CapProcessGenerator:
    """
    Time-scaled PRB capacity generator.

    If time_scale = S:
      • One step ≈ S old steps
      • Preserves distribution + burst statistics
    """

    def __init__(
        self,
        *,
        seed: int,
        ops: Dict[str, Dict[str, int]],
        lam: Dict[str, float],
        max_step: Dict[str, int],
        kappa: Dict[str, float],
        baseline: Optional[Dict[str, int]] = None,
        floor_frac: Optional[Dict[str, float]] = None,
        bursts: Optional[Dict[str, BurstCfg]] = None,
        upbursts: Optional[Dict[str, BurstCfg]] = None,
        time_scale: int = 1,
    ):
        self.rng = np.random.default_rng(seed)
        baseline = baseline or {}
        floor_frac = floor_frac or {}
        bursts = bursts or {}
        upbursts = upbursts or {}

        self.t = 0
        self.ops_state: Dict[str, _OpState] = {}

        scale = max(1, int(time_scale))

        for op_id, cfg in ops.items():
            max_cap = int(cfg["max_cap"])
            min_cap_cfg = int(cfg.get("min_cap", 0))
            init_cap = int(cfg.get("init_cap", max_cap))

            b = int(baseline.get(op_id, round(0.8 * max_cap)))
            b = max(0, min(max_cap, b))

            ff = float(floor_frac.get(op_id, 0.40))
            ff = max(0.0, min(1.0, ff))

            lam_o = max(0.0, float(lam.get(op_id, 1.0)) * scale)
            max_step_o = max(0, int(max_step.get(op_id, 2) * scale))
            k = max(0.0, min(1.0, float(kappa.get(op_id, 0.15))))
            kappa_o = 1.0 - (1.0 - k) ** scale
            kappa_o = min(1.0, kappa_o)

            burst_cfg = bursts.get(op_id)
            if burst_cfg is not None:
                burst_cfg = BurstCfg(
                    burst_rate=burst_cfg.burst_rate * scale,
                    dur_min=max(1, math.ceil(burst_cfg.dur_min / scale)),
                    dur_max=max(1, math.ceil(burst_cfg.dur_max / scale)),
                    depth_min=burst_cfg.depth_min,
                    depth_max=burst_cfg.depth_max,
                    recover=burst_cfg.recover,
                )

            upburst_cfg = upbursts.get(op_id)
            if upburst_cfg is not None:
                upburst_cfg = BurstCfg(
                    burst_rate=upburst_cfg.burst_rate * scale,
                    dur_min=max(1, math.ceil(upburst_cfg.dur_min / scale)),
                    dur_max=max(1, math.ceil(upburst_cfg.dur_max / scale)),
                    depth_min=upburst_cfg.depth_min,
                    depth_max=upburst_cfg.depth_max,
                    recover=upburst_cfg.recover,
                )

            cap0 = max(int(ff * max_cap), min(max_cap, init_cap))

            self.ops_state[op_id] = _OpState(
                max_cap=max_cap,
                min_cap_cfg=min_cap_cfg,
                baseline=b,
                floor_frac=ff,
                lam=lam_o,
                max_step=max_step_o,
                kappa=kappa_o,
                cap=cap0,
                bursts_cfg=burst_cfg,
                upbursts_cfg=upburst_cfg,
                active_bursts=[],
                active_upbursts=[],
            )

    def _poisson_mean_revert(self, st: _OpState) -> int:
        cap = st.cap

        k = int(self.rng.poisson(st.lam)) if st.lam > 0 else 0
        step_mag = min(k, st.max_step)

        if step_mag > 0:
            cap += step_mag if self.rng.random() < 0.5 else -step_mag

        gap = st.baseline - cap
        cap += int(round(st.kappa * gap))

        return cap

    def _burst_offset(
        self,
        cfg: Optional[BurstCfg],
        active: List[_BurstInstance],
        direction: int,
    ) -> int:
        bc = cfg
        if bc is None:
            return 0

        # new bursts
        nb = int(self.rng.poisson(bc.burst_rate))
        for _ in range(nb):
            active.append(
                _BurstInstance(
                    duration=int(self.rng.integers(bc.dur_min, bc.dur_max + 1)),
                    depth=int(self.rng.integers(bc.depth_min, bc.depth_max + 1)),
                    recover=bc.recover,
                )
            )

        offset = 0
        remaining: List[_BurstInstance] = []

        for inst in active:
            if inst.age < inst.duration:
                if inst.recover and inst.duration >= 3:
                    mid = inst.duration // 2
                    if inst.age <= mid:
                        frac = inst.age / max(1, mid)
                    else:
                        frac = (inst.duration - 1 - inst.age) / max(
                            1, inst.duration - 1 - mid
                        )
                else:
                    frac = 1.0

                offset += direction * int(round(inst.depth * frac))

                inst.age += 1
                if inst.age < inst.duration:
                    remaining.append(inst)

        active[:] = remaining
        return offset

    def step(self) -> Dict[str, int]:
        self.t += 1
        out: Dict[str, int] = {}

        for op_id, st in self.ops_state.items():
            cap = self._poisson_mean_revert(st)
            cap += self._burst_offset(st.bursts_cfg, st.active_bursts, direction=-1)
            cap += self._burst_offset(st.upbursts_cfg, st.active_upbursts, direction=1)

            min_cap = max(st.min_cap_cfg, int(round(st.floor_frac * st.max_cap)))
            cap = max(min_cap, min(st.max_cap, cap))

            st.cap = cap
            out[op_id] = cap

        return out


class ScenarioController:
    """
    Updates generator parameters at runtime based on current step (gen.t).

    Minimal-intrusion approach:
    - Mutate st.baseline
    - Optionally scale bursts_cfg for "episode texture"
    """

    def __init__(self, scenario_cfg: Dict[str, Any], *, base_bursts: Dict[str, BurstCfg]):
        self.phases = scenario_cfg.get("phases", [])
        self.base_bursts = base_bursts
        self.debug = bool(scenario_cfg.get("debug", False))
        self._last_phase_by_op: Dict[str, Optional[str]] = {}

    @staticmethod
    def _lin_interp(a: float, b: float, frac: float) -> float:
        return a + frac * (b - a)

    def _find_phase(self, t: int) -> Optional[Dict[str, Any]]:
        step_idx = t - 1
        for ph in self.phases:
            if ph["start"] <= step_idx <= ph["end"]:
                return ph
        return None

    def apply(self, gen: CapProcessGenerator) -> None:
        ph = self._find_phase(gen.t)
        if not ph:
            return

        step_idx = gen.t - 1
        start = int(ph["start"])
        end = int(ph["end"])
        denom = max(1, end - start)
        frac = (step_idx - start) / denom

        if "baseline" in ph:
            for op, b in ph["baseline"].items():
                if op in gen.ops_state:
                    st = gen.ops_state[op]
                    st.baseline = int(max(0, min(st.max_cap, int(b))))
        elif "baseline_ramp" in ph:
            for op, rr in ph["baseline_ramp"].items():
                if op in gen.ops_state:
                    st = gen.ops_state[op]
                    b0 = float(rr["from"])
                    b1 = float(rr["to"])
                    b = self._lin_interp(b0, b1, frac)
                    st.baseline = int(max(0, min(st.max_cap, int(round(b)))))

        mults = ph.get("burst_mult", {})
        for op, m in mults.items():
            if op not in gen.ops_state:
                continue
            st = gen.ops_state[op]
            base = self.base_bursts.get(op)
            if base is None:
                continue
            m = float(m)
            st.bursts_cfg = BurstCfg(
                burst_rate=max(0.0, base.burst_rate * m),
                dur_min=base.dur_min,
                dur_max=base.dur_max,
                depth_min=max(0, int(round(base.depth_min * m))),
                depth_max=max(0, int(round(base.depth_max * m))),
                recover=base.recover,
            )

        if self.debug:
            phase_name = ph.get("name")
            for op in gen.ops_state.keys():
                if self._last_phase_by_op.get(op) != phase_name:
                    st = gen.ops_state[op]
                    print(
                        f"[SCENARIO] gNB={op} phase={phase_name} step={step_idx} "
                        f"baseline={st.baseline}"
                    )
                    self._last_phase_by_op[op] = phase_name
