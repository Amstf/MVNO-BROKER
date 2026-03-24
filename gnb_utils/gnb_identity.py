def canonicalize_gnb_id(raw: str) -> str:
    """Normalize a gNB identifier so all modules use the same value.

    The current deployment reports the gNB as an IP address; we map that to the
    last octet (e.g., ``10.156.12.21`` -> ``"21"``). If the input is already a
    short ID (e.g., ``"21"``), it is returned unchanged.
    """

    if raw is None:
        return "unknown"

    text = str(raw).strip()
    if text.count(".") >= 3:
        try:
            return text.split(".")[-1]
        except Exception:  # noqa: BLE001
            pass

    return text or "unknown"


def extract_gnb_id(entry):
    """
    Extract and normalize the gNB identifier from a RAN_parameter entry.
    """

    if hasattr(entry, "string_value") and entry.string_value:
        return canonicalize_gnb_id(entry.string_value)

    return canonicalize_gnb_id(str(entry))
