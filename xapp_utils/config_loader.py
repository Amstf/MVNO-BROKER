# config_loader.py

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def load_config(config_path: Optional[str]) -> Dict[str, Any]:
    """
    Load a JSON config file. No internal defaults are applied anymore.
    The caller must provide a valid path (either via CLI or BASE_XAPP_CONFIG).
    """
    # If caller didn't pass a path, try the environment variable
    if not config_path:
        config_path = os.environ.get("BASE_XAPP_CONFIG")

    if not config_path:
        raise ValueError(
            "No config path provided. Use --config or set BASE_XAPP_CONFIG."
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with path.open("r", encoding="utf-8") as fp:
        loaded = json.load(fp)

    if not isinstance(loaded, dict):
        raise ValueError("Config file must contain a JSON object at the top level")

    return loaded
