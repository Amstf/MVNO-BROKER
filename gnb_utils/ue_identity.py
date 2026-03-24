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
from typing import Any, Dict, Iterable, Optional, Set, Tuple

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
