# gnb_utils/

Utilities for gNB identity resolution and UE lifecycle management on Kubernetes.

| File | Purpose |
|------|---------|
| `gnb_identity.py` | Normalizes and extracts gNB identifiers from various RAN formats (IP addresses, MEID strings) into canonical short IDs. |
| `ue_identity.py` | Maintains stable logical UE IDs across gNB/RNTI changes; tracks per-UE state (current gNB, RNTI, pod, status) and reverse session indexes. |
| `ue_management.py` | Manages UE pod operations via Kubernetes: initializes k8s clients, starts/stops UE processes in pods, launches iperf3 traffic, checks interface readiness, and cleans up UEs on exit. |
