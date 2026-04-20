import json
import os
from pathlib import Path
from typing import Optional


def _find_config_file() -> Optional[Path]:
    candidates = [
        Path(os.environ.get("XAPP_CONFIG", "")),
        Path(__file__).resolve().parent.parent / "xapp_bs_connector" / "init" / "config-file.json",
        Path("/opt/route/config-file.json"),
    ]
    for p in candidates:
        if p and p.exists():
            return p
    return None


def log_target_gnb_from_config():
    cfg_path = _find_config_file()
    if not cfg_path:
        print("[CONFIG] No config-file.json found (default behavior).")
        return

    try:
        cfg = json.load(cfg_path.open("r"))
    except Exception as e:  # noqa: BLE001
        print(f"[CONFIG] Could not load config-file.json: {e}")
        return

    try:
        target_gnb = cfg["containers"][0]["target_gnb"]
        print(f"[CONFIG] target_gnb = '{target_gnb}'")
    except Exception:  # noqa: BLE001
        print("[CONFIG] target_gnb not present.")


def log_gnb_env_choice():
    g = os.environ.get("GNB_ID")
    if g:
        print(f"[CONFIG] Env GNB_ID = '{g}' (connector will target only this gNB)")
    else:
        print("[CONFIG] No GNB_ID set → connector will target ALL registered gNBs")
