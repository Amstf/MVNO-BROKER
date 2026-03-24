"""Config and target-resolution helpers (Phase-1 pure extraction)."""

from xapp_utils.config_loader import load_config
from xapp_runtime.targeting_constants import (
    BROKER_ACTIVE_KEYS,
    BROKER_DEPRECATED_KEYS,
    LOGICAL_TO_MEID,
    MEID_TO_LOGICAL,
    TARGET_LOGICAL_IDS,
)


def _load_loop_config(config_path: str) -> dict:
    cfg = load_config(config_path)
    if "ue" not in cfg:
        raise ValueError("config missing 'ue'")
    if "ue_control" not in cfg:
        raise ValueError("config missing 'ue_control'")
    if "broker" not in cfg:
        raise ValueError("config missing 'broker'")
    return cfg


def _validate_broker_cfg_keys(broker_cfg: dict) -> None:
    deprecated = sorted(set(broker_cfg.keys()) & BROKER_DEPRECATED_KEYS)
    if deprecated:
        raise ValueError(f"broker has deprecated keys not allowed in tick architecture: {deprecated}")
    unknown = sorted(set(broker_cfg.keys()) - BROKER_ACTIVE_KEYS)
    if unknown:
        raise ValueError(f"broker has unknown keys (strict mode): {unknown}")


def _build_id_maps(gnb_targets: dict):
    logical_to_meid = {}
    meid_to_logical = {}
    for logical_id, entry in gnb_targets.items():
        meid = str(entry.get("meid", "")).strip()
        if not meid:
            raise ValueError(f"gnb_targets[{logical_id}] missing meid")
        logical_to_meid[str(logical_id)] = meid
        meid_to_logical[meid] = str(logical_id)
    return logical_to_meid, meid_to_logical


def _resolve_targets(cfg: dict):
    gnb_targets_cfg = cfg.get("gnb_targets")
    if gnb_targets_cfg:
        logical_to_meid, meid_to_logical = _build_id_maps(gnb_targets_cfg)
        gnb_targets = {
            op_id: {
                "meid": logical_to_meid[op_id],
                "cell_total_prbs": float(gnb_targets_cfg[op_id].get("cell_total_prbs", 106.0)),
            }
            for op_id in logical_to_meid.keys()
        }
        return gnb_targets, logical_to_meid, meid_to_logical

    gnb_targets = {
        op_id: {
            "meid": LOGICAL_TO_MEID[op_id],
            "cell_total_prbs": 106.0,
        }
        for op_id in TARGET_LOGICAL_IDS
    }
    return gnb_targets, dict(LOGICAL_TO_MEID), dict(MEID_TO_LOGICAL)


def normalize_gnb_id(raw_id: str, logical_to_meid: dict, meid_to_logical: dict):
    if raw_id is None:
        return None, None
    s = str(raw_id).strip()
    if s in meid_to_logical:
        logical = meid_to_logical[s]
        return logical, s
    if s in logical_to_meid:
        return s, logical_to_meid[s]
    return None, None
