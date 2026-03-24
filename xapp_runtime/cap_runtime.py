"""CAP generation setup and per-tick enforcement planning."""

from modules.cap_generator import BurstCfg, CapProcessGenerator, ScenarioController


def _filter_cap_cfg(cap_cfg: dict, keep_op_ids: tuple) -> dict:
    def keep_map(m: dict) -> dict:
        return {op: m[op] for op in keep_op_ids if op in m} if m else {}

    out = dict(cap_cfg or {})
    for key in ["ops", "lam", "max_step", "kappa", "baseline", "floor_frac"]:
        out[key] = keep_map(out.get(key, {}))

    bursts = out.get("bursts") or {}
    out["bursts"] = {op: bursts[op] for op in keep_op_ids if op in bursts}
    upbursts = out.get("upbursts") or {}
    out["upbursts"] = {op: upbursts[op] for op in keep_op_ids if op in upbursts}
    return out


def _build_bursts_cfg(cap_cfg: dict, key: str = "bursts") -> dict:
    bursts_cfg = {}
    for op_id, bc in (cap_cfg.get(key) or {}).items():
        dur_min = int(bc.get("dur_min", 0))
        dur_max = int(bc.get("dur_max", 0))
        if dur_max < dur_min:
            dur_min, dur_max = dur_max, dur_min

        depth_min = int(bc.get("depth_min", 0))
        depth_max = int(bc.get("depth_max", 0))
        if depth_max < depth_min:
            depth_min, depth_max = depth_max, depth_min

        bursts_cfg[op_id] = BurstCfg(
            burst_rate=bc.get("burst_rate", 0.0),
            dur_min=dur_min,
            dur_max=dur_max,
            depth_min=depth_min,
            depth_max=depth_max,
            recover=bc.get("recover", True),
        )
    return bursts_cfg


def build_cap_runtime(*, cap_cfg_raw: dict, target_logical_ids: tuple):
    cap_generators = {}
    cap_scenarios = {}
    seeds = cap_cfg_raw.get("seeds")
    seed_list = list(seeds) if isinstance(seeds, (list, tuple)) else None
    seed_base = int(cap_cfg_raw.get("seed", 42))
    time_scale = int(cap_cfg_raw.get("time_scale", 1))
    scenario_cfg = cap_cfg_raw.get("scenario")

    for idx, op_id in enumerate(target_logical_ids):
        cap_cfg = _filter_cap_cfg(cap_cfg_raw, (op_id,))
        bursts_cfg = _build_bursts_cfg(cap_cfg, "bursts")
        upbursts_cfg = _build_bursts_cfg(cap_cfg, "upbursts")
        seed = seed_base
        if seed_list:
            seed = int(seed_list[idx % len(seed_list)])
        cap_generators[op_id] = CapProcessGenerator(
            seed=seed,
            ops=cap_cfg.get("ops", {}),
            lam=cap_cfg.get("lam", {}),
            max_step=cap_cfg.get("max_step", {}),
            kappa=cap_cfg.get("kappa", {}),
            baseline=cap_cfg.get("baseline", {}),
            floor_frac=cap_cfg.get("floor_frac", {}),
            bursts=bursts_cfg,
            upbursts=upbursts_cfg,
            time_scale=time_scale,
        )
        if scenario_cfg:
            cap_scenarios[op_id] = ScenarioController(scenario_cfg, base_bursts=bursts_cfg)

    return cap_generators, cap_scenarios


def plan_cap_for_tick(*, target_logical_ids: tuple, gnb_states: dict, cap_generators: dict, cap_scenarios: dict, min_prb_by_op: dict):
    slice_ctrl_updates = {}
    for op_id in target_logical_ids:
        gen = cap_generators.get(op_id)
        if gen is None:
            continue
        scenario = cap_scenarios.get(op_id)
        if scenario is not None:
            scenario.apply(gen)
        out = gen.step()
        cap_prb = float(out.get(op_id, 0.0) or 0.0)
        gnb_states[op_id].apply_cap(cap_prb)
        cap_ratio = gnb_states[op_id].cap_ratio
        if cap_ratio is not None:
            cap_ratio_int = int(round(cap_ratio))
            cell_total_prbs = float(getattr(gnb_states[op_id], "cell_total_prbs", 0.0) or 0.0)
            min_prb = float(min_prb_by_op.get(op_id, 0.0))
            min_ratio_int = 0
            if cell_total_prbs > 0.0:
                min_ratio = 100.0 * min_prb / cell_total_prbs
                min_ratio_int = int(round(max(0.0, min_ratio)))
            min_ratio_int = min(min_ratio_int, cap_ratio_int)
            slice_ctrl_updates[op_id] = {
                "min_ratio": min_ratio_int,
                "max_ratio": cap_ratio_int,
            }
    return {"slice_ctrl_updates": slice_ctrl_updates}
