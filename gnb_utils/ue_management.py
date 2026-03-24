# ue_management.py
import time
from typing import Dict, Optional

from kubernetes import client, config
from kubernetes.stream import stream

from .gnb_identity import canonicalize_gnb_id
from .ue_identity import (
    configure_identity_from_placements,
    mark_ue_moving,
)

UE_NAMESPACE = "ue"

DEFAULT_PLACEMENTS = [
    {"pod": "ue1", "logical_id": "ue-1", "gnb_ip": "10.156.12.21", "conf": "nrUE_slice1.conf"},
    {"pod": "ue2", "logical_id": "ue-2", "gnb_ip": "10.156.12.223", "conf": "nrUE1_slice1.conf"},
]
DN_IP_DEFAULT = "192.168.70.135"
TRAFFIC_IFACE = "oaitun_ue1"
ROUTE_SUBNET_DEFAULT = "192.168.70.0/24"

core_v1: Optional[client.CoreV1Api] = None
UE_READY = {"iface": set(), "iperf3": set()}

ue_settings: Dict = {"placements": DEFAULT_PLACEMENTS}


def configure_ue_settings(config: Dict):
    global ue_settings
    placements = config.get("placements", DEFAULT_PLACEMENTS)
    ue_settings = {"placements": placements}

    # Initialize identity map with configured placements
    configure_identity_from_placements(placements)


def init_k8s():
    global core_v1
    config.load_incluster_config()
    core_v1 = client.CoreV1Api()
    print("[K8S] In-cluster Kubernetes client initialized.")


def _exec_in_ue_pod(pod_name: str, cmd_str: str) -> str:
    """Run a bash command inside a UE pod and return stdout+stderr (stream merges them)."""
    global core_v1
    if core_v1 is None:
        raise RuntimeError("CoreV1Api not initialized")

    cmd = ["/bin/bash", "-lc", cmd_str]
    return stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name,
        UE_NAMESPACE,
        command=cmd,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def wait_for_iface_in_pod(
    pod_name: str,
    iface: str,
    *,
    timeout_s: int = 30,
    poll_s: float = 1.0,
) -> bool:
    if pod_name in UE_READY["iface"]:
        return True
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            out = _exec_in_ue_pod(
                pod_name,
                f"ip link show {iface} >/dev/null 2>&1 && echo OK || echo NO",
            )
            if "OK" in out:
                UE_READY["iface"].add(pod_name)
                return True
        except Exception as exc:  # noqa: BLE001
            print(f"[UE][WAIT][WARN] {pod_name}: iface check error: {exc}")
        time.sleep(poll_s)
    return False


def _clear_ue_ready(pod_name: str) -> None:
    UE_READY["iface"].discard(pod_name)
    UE_READY["iperf3"].discard(pod_name)





def ensure_iperf3_in_pod(pod_name: str) -> None:
    """
    Best-effort install. Works only if pod has pkg manager + repo access.
    If already present, does nothing.
    """
    if pod_name in UE_READY["iperf3"]:
        return
    cmd = r"""
set -euo pipefail
if command -v iperf3 >/dev/null 2>&1; then
  echo "[ensure_iperf3] iperf3 already present: $(command -v iperf3)"
  exit 0
fi

echo "[ensure_iperf3] iperf3 missing; attempting install..."

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y iperf3
  exit 0
fi

if command -v apk >/dev/null 2>&1; then
  apk add --no-cache iperf3
  exit 0
fi

if command -v dnf >/dev/null 2>&1; then
  dnf install -y iperf3
  exit 0
fi

if command -v yum >/dev/null 2>&1; then
  yum install -y iperf3
  exit 0
fi

echo "[ensure_iperf3][WARN] No supported package manager found; cannot install iperf3."
exit 0
"""
    out = _exec_in_ue_pod(pod_name, cmd)
    if "iperf3 already present" in out or "attempting install" in out:
        UE_READY["iperf3"].add(pod_name)
    print(f"[UE][IPERF][ENSURE] {pod_name}:\n{out}")


def start_ue_traffic(
    pod_name: str,
    *,
    rate_mbps: int,
    direction: str,
    port: Optional[int] = None,
    duration_s: Optional[int] = None,
    dn_ip: str = DN_IP_DEFAULT,
    iface: str = TRAFFIC_IFACE,
    route_subnet: str = ROUTE_SUBNET_DEFAULT,
    set_route: bool = True,
    ensure_iperf: bool = True,
    iface_timeout_s: int = 30,
    quiet: bool = False,
    force_checks: bool = False,
) -> None:
    """
    Pure xApp-driven traffic start. No UE-side script file required.
    Writes logs under /tmp in the UE pod.
    """
    if direction not in ("uplink", "downlink", "bidir"):
        print(f"[UE][IPERF] {pod_name}: invalid direction={direction}")
        return
    if rate_mbps <= 0:
        print(f"[UE][IPERF] {pod_name}: invalid rate_mbps={rate_mbps}")
        return
    if port is None:
        port = POD_PORT_MAP.get(pod_name, 5502)

    if force_checks:
        _clear_ue_ready(pod_name)

    if pod_name not in UE_READY["iface"]:
        if not quiet:
            print(f"[UE][IPERF] {pod_name}: waiting for iface {iface}...")
        if not wait_for_iface_in_pod(pod_name, iface, timeout_s=iface_timeout_s):
            if not quiet:
                print(f"[UE][IPERF][WARN] {pod_name}: iface {iface} not up; skipping traffic start.")
            return

    if ensure_iperf:
        try:
            ensure_iperf3_in_pod(pod_name)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"[UE][IPERF][WARN] {pod_name}: ensure_iperf3 failed: {exc}")
            _clear_ue_ready(pod_name)

    # Build iperf flags
    if direction == "bidir":
        dir_flag = "--bidir"
    elif direction == "downlink":
        dir_flag = "--reverse"
    else:
        dir_flag = ""

    t_flag = "" if duration_s is None else f"-t {int(duration_s)}"

    cmd = f"""
set -euo pipefail

PDU_IP="$(ip -4 -o addr show "{iface}" | awk '{{print $4}}' | cut -d/ -f1)"
test -n "$PDU_IP"

if {str(set_route).lower()}; then
  ip route replace "{route_subnet}" dev "{iface}" >/dev/null 2>&1 || true
fi

PIDFILE="/tmp/iperf_{port}.pid"
if [ -f "$PIDFILE" ]; then
  kill -TERM "$(cat "$PIDFILE")" 2>/dev/null || true
fi
sleep 0.02

nohup setsid iperf3 -c "{dn_ip}" -p "{port}" {dir_flag} -u -b "{rate_mbps}M" {t_flag} -B "$PDU_IP" >/dev/null 2>&1 &
echo $! > "$PIDFILE"
"""
    try:
        out = _exec_in_ue_pod(pod_name, cmd)
    except Exception as exc:  # noqa: BLE001
        _clear_ue_ready(pod_name)
        if not quiet:
            print(f"[UE][IPERF][WARN] {pod_name}: iperf3 start failed: {exc}")
        return
    if "not found" in out or "command not found" in out:
        _clear_ue_ready(pod_name)
    if not quiet:
        print(f"[UE][IPERF][START] {pod_name} -> {dn_ip}:{port}:\n{out}")


def stop_ue_in_pod(pod_name: str, *, direct_move_safe_stop: bool = False):
    global core_v1
    if core_v1 is None:
        print(f"[K8S] CoreV1Api not initialized, cannot stop UE in {pod_name}.")
        return

    _exec_in_ue_pod(pod_name, "pkill -9 -f iperf3 || true")
    if direct_move_safe_stop:
        time.sleep(1.5)

    _exec_in_ue_pod(pod_name, "pkill -9 -f start_ue.sh || true")
    _exec_in_ue_pod(pod_name, "pkill -9 -f nr-uesoftmodem || true")

    cmd = ["/bin/bash", "-c", "pkill -9 -f nr-uesoftmodem || true"]
    print(f"[K8S] Stopping UE in pod: {pod_name}")

    resp = stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name,
        UE_NAMESPACE,
        command=cmd,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )
    print(f"[K8S] Stop response from {pod_name}:\n{resp}")

def start_ue_in_pod(pod_name: str, gnb_ip: str, conf_file: str):
    global core_v1
    if core_v1 is None:
        print(f"[K8S] CoreV1Api not initialized, cannot start UE in {pod_name}.")
        return

    cmd_str = (
        f"nohup ./start_ue.sh -m rfsim -g {gnb_ip} -c {conf_file} "
        f"> /tmp/{pod_name}_{conf_file}.log 2>&1 &"
    )
    cmd = ["/bin/bash", "-c", cmd_str]

    print(f"[K8S] Starting UE in pod={pod_name} with gNB={gnb_ip}, conf={conf_file}")

    resp = stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name,
        UE_NAMESPACE,
        command=cmd,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )
    print(f"[K8S] Start response from {pod_name}:\n{resp}")


def get_ordered_startup_placements() -> list:
    placements = list(ue_settings.get("placements", DEFAULT_PLACEMENTS))
    ordered = []
    for placement in placements:
        gnb_id = str(placement.get("gnb_id", "")).strip()
        role = str(placement.get("role", "")).strip().lower()
        if gnb_id == "Ghost_UE" or role == "ghost":
            continue
        ordered.append(placement)
    return ordered


def check_iface_in_pod(pod_name: str, iface: str = "oaitun_ue1") -> bool:
    try:
        out = _exec_in_ue_pod(
            pod_name,
            f"ip link show {iface} >/dev/null 2>&1 && echo OK || echo NO",
        )
        return "OK" in out
    except Exception as exc:  # noqa: BLE001
        print(f"[UE][IFACE][WARN] {pod_name}: iface check error: {exc}")
        return False


def cleanup_ues():
    print("[CLEANUP] Stopping UE processes before exit...")
    for placement in ue_settings.get("placements", DEFAULT_PLACEMENTS):
        pod = placement.get("pod")
        try:
            stop_ue_in_pod(pod)
        except Exception as e:  # noqa: BLE001
            print(f"[CLEANUP] Error stopping {pod}: {e}")


