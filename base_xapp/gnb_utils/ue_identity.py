"""UE identity abstraction to keep stable logical UE IDs across gNB swaps.

This module introduces a *logical UE ID* that remains stable across
gNB/RNTI changes. It keeps track of:

* Logical UE state (current gNB, current RNTI, status, pod, IMSI, timestamps).
* Reverse session index: (gNB ID, RNTI) -> logical UE ID.
* Ignored sessions: session keys that should be filtered once a swap has
  completed so stale reports don't pollute metrics or MongoDB.

It is intentionally stateful (module-level dicts) so both the controller loop
and UE management helpers can share the same view of UE identity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from time import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .gnb_identity import canonicalize_gnb_id


class UEStatus(str, Enum):
    ACTIVE = "ACTIVE"
    MOVING = "MOVING"
    DETACHED = "DETACHED"


@dataclass
class UEState:
    logical_id: str
    pod: Optional[str] = None
    current_gnb: Optional[str] = None
    current_rnti: Optional[int] = None
    pending_target_gnb: Optional[str] = None
    status: UEStatus = UEStatus.ACTIVE
    last_seen_ts: float = 0.0
    imsi: Optional[str] = None

    def session_key(self) -> Optional[Tuple[str, int]]:
        if self.current_gnb is None or self.current_rnti is None:
            return None
        return (str(self.current_gnb), int(self.current_rnti))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


logical_states: Dict[str, UEState] = {}
session_index: Dict[Tuple[str, int], str] = {}
ignored_sessions: Set[Tuple[str, int]] = set()
imsi_index: Dict[str, str] = {}
configured_ids: Set[str] = set()
_dynamic_counter = 0


def _normalize_session_key(gnb_id: str, rnti: int) -> Tuple[str, int]:
    return (canonicalize_gnb_id(gnb_id), int(rnti))


def _safe_get(msg: Any, field: str, default=None):
    try:
        if msg.HasField(field):
            return getattr(msg, field)
    except ValueError:
        # Fields like repeated/int may not track presence but still exist
        if hasattr(msg, field):
            return getattr(msg, field)
    except Exception:
        pass
    return default


def _extract_rnti(ue_msg: Any) -> int:
    return int(_safe_get(ue_msg, "rnti", 0) or 0)


def _extract_imsi(ue_msg: Any) -> Optional[str]:
    imsi = _safe_get(ue_msg, "imsi", None)
    if imsi:
        return str(imsi)
    # Some messages may encode IMSI under ue_id
    alt = _safe_get(ue_msg, "ue_id", None)
    return str(alt) if alt else None


def configure_identity_from_placements(placements: Iterable[Dict[str, Any]]):
    """Initialize logical UE states from placement configuration.

    Each placement may optionally provide ``logical_id``. If missing, the pod
    name is used as the stable logical identifier to preserve backward
    compatibility.
    """

    logical_states.clear()
    session_index.clear()
    ignored_sessions.clear()
    imsi_index.clear()
    configured_ids.clear()

    for placement in placements:
        logical_id = placement.get("logical_id") or placement.get("pod") or placement.get("ue_id")
        if not logical_id:
            continue

        canonical_gnb = canonicalize_gnb_id(placement.get("gnb_ip"))
        state = UEState(
            logical_id=str(logical_id),
            pod=placement.get("pod"),
            current_gnb=canonical_gnb,
            current_rnti=None,
            status=UEStatus.ACTIVE,
            pending_target_gnb=None,
            last_seen_ts=0.0,
            imsi=str(placement.get("imsi")) if placement.get("imsi") else None,
        )

        logical_states[state.logical_id] = state
        configured_ids.add(state.logical_id)
        if state.imsi:
            imsi_index[state.imsi] = state.logical_id

    print(f"[UE-ID] Loaded logical UE identities: {sorted(logical_states.keys())}")


def _register_dynamic_identity(base_hint: str, imsi: Optional[str] = None) -> str:
    global _dynamic_counter
    _dynamic_counter += 1
    logical_id = f"{base_hint}-{_dynamic_counter}"
    logical_states[logical_id] = UEState(logical_id=logical_id, imsi=imsi)
    if imsi:
        imsi_index[imsi] = logical_id
    print(f"[UE-ID][NEW] Registered dynamic UE identity '{logical_id}' (imsi={imsi})")
    return logical_id


def get_state(logical_id: str) -> Optional[Dict[str, Any]]:
    state = logical_states.get(logical_id)
    return state.to_dict() if state else None


def get_current_session(logical_id: str) -> Tuple[Optional[str], Optional[int]]:
    state = logical_states.get(logical_id)
    if not state:
        return None, None
    return state.current_gnb, state.current_rnti


def _mark_session_as_ignored(session_key: Tuple[str, int], reason: str = ""):
    ignored_sessions.add(session_key)
    if reason:
        print(f"[UE-ID][STALE] Marked session {session_key} as ignored ({reason})")
    else:
        print(f"[UE-ID][STALE] Marked session {session_key} as ignored")

def mark_ue_moving(logical_id: str, target_gnb: str):
    """Mark a logical UE as moving toward ``target_gnb``.

    The current session (if known) is immediately placed into the ignored set
    so that lingering reports from the old gNB/RNTI are filtered.
    """

    state = logical_states.get(logical_id)
    if not state:
        print(f"[UE-ID][WARN] Cannot mark moving; unknown UE '{logical_id}'")
        return

    old_session = state.session_key()
    if old_session:
        _mark_session_as_ignored(old_session, reason="swap initiated")

    state.status = UEStatus.MOVING
    state.pending_target_gnb = canonicalize_gnb_id(target_gnb)
    state.last_seen_ts = time()
    print(
        f"[UE-ID][MOVE] UE '{logical_id}' moving toward gNB {target_gnb}; "
        f"old session={old_session}"
    )


def _bind_session(logical_id: str, gnb_id: str, rnti: int, current_ts: float, imsi: Optional[str] = None) -> str:
    gnb_id = canonicalize_gnb_id(gnb_id)
    state = logical_states.setdefault(logical_id, UEState(logical_id=logical_id))
    new_session = _normalize_session_key(gnb_id, rnti)

    # If the UE was previously on a different session, mark that session stale
    old_session = state.session_key()
    if old_session and old_session != new_session:
        _mark_session_as_ignored(old_session, reason="superseded by new session")

    session_index[new_session] = logical_id
    state.current_gnb = gnb_id
    state.current_rnti = rnti
    state.pending_target_gnb = None
    state.status = UEStatus.ACTIVE
    state.last_seen_ts = current_ts
    if imsi:
        state.imsi = imsi
        imsi_index[imsi] = logical_id

    return logical_id


def resolve_logical_ue(gnb_id: str, ue_entry: Any, current_ts: float) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Resolve the logical UE ID for an incoming (gNB, RNTI) pair.

    Resolution order:
    1) If the session key is explicitly ignored -> skip/None
    2) If session already mapped -> reuse logical ID
    3) If IMSI known -> bind to that logical ID (existing or new)
    4) If exactly one UE is in MOVING state targeting this gNB -> bind it
    5) Otherwise, create a dynamic logical UE ID
    """

    gnb_id = canonicalize_gnb_id(gnb_id)
    rnti = _extract_rnti(ue_entry)
    imsi = _extract_imsi(ue_entry)
    session_key = _normalize_session_key(gnb_id, rnti)

    if session_key in ignored_sessions:
        print(f"[UE-ID][IGNORE] Stale session {session_key} kept (no skip)")

    if session_key in session_index:
        logical_id = session_index[session_key]
        _bind_session(logical_id, gnb_id, rnti, current_ts, imsi=imsi)
        state = logical_states[logical_id]
        return logical_id, state.to_dict()

    # If exactly one configured UE on this gNB has never been bound to a session,
    # bind it instead of creating a dynamic identity.
    cold_candidates: List[str] = [
        lid
        for lid, st in logical_states.items()
        if st.current_gnb == gnb_id and st.current_rnti is None and st.status != UEStatus.MOVING
    ]
    if len(cold_candidates) == 1:
        logical_id = cold_candidates[0]
        _bind_session(logical_id, gnb_id, rnti, current_ts, imsi=imsi)
        print(f"[UE-ID][MAP] First session for preconfigured UE '{logical_id}' on {session_key}")
        return logical_id, logical_states[logical_id].to_dict()

    if imsi:
        logical_id = imsi_index.get(imsi) or _register_dynamic_identity(f"imsi-{imsi}", imsi=imsi)
        _bind_session(logical_id, gnb_id, rnti, current_ts, imsi=imsi)
        print(f"[UE-ID][MAP] IMSI {imsi} -> logical '{logical_id}' via session {session_key}")
        return logical_id, logical_states[logical_id].to_dict()

    # If we have preconfigured UEs, prefer re-binding to one of them (to avoid minting new IDs)
    if configured_ids:
        preferred: List[str] = [
            lid for lid, st in logical_states.items() if lid in configured_ids and st.pending_target_gnb == gnb_id
        ] or [
            lid for lid, st in logical_states.items() if lid in configured_ids and st.current_gnb == gnb_id
        ] or [lid for lid in configured_ids if lid in logical_states]

        if preferred:
            logical_id = preferred[0]
            _bind_session(logical_id, gnb_id, rnti, current_ts, imsi=imsi)
            print(f"[UE-ID][MAP] Rebinding unknown session {session_key} to configured UE '{logical_id}'")
            return logical_id, logical_states[logical_id].to_dict()

    moving_matches: List[str] = [
        lid
        for lid, st in logical_states.items()
        if st.status == UEStatus.MOVING and st.pending_target_gnb == gnb_id
    ]

    if len(moving_matches) == 1:
        logical_id = moving_matches[0]
        _bind_session(logical_id, gnb_id, rnti, current_ts, imsi=imsi)
        print(f"[UE-ID][MAP] Moving UE '{logical_id}' now active on {session_key}")
        return logical_id, logical_states[logical_id].to_dict()
    if len(moving_matches) > 1:
        print(
            f"[UE-ID][WARN] Multiple moving UEs targeting gNB {gnb_id}; "
            f"assigning dynamic ID for session {session_key}"
        )

    logical_id = _register_dynamic_identity("ue", imsi=imsi)
    _bind_session(logical_id, gnb_id, rnti, current_ts, imsi=imsi)
    return logical_id, logical_states[logical_id].to_dict()


def snapshot_states() -> Dict[str, Dict[str, Any]]:
    return {lid: state.to_dict() for lid, state in logical_states.items()}
