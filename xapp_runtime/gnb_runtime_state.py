from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from collections import deque


@dataclass
class GnbState:
    gnb_id: str
    cell_total_prbs: float
    slots_per_second: float = 1000.0
    meid: Optional[str] = None
    report_index: int = 0
    tick_id: int = 0
    tick_ts: Optional[float] = None
    last_seen_ts: Optional[float] = None

    # --- measured KPIs ---
    goodput_mbps: float = 0.0
    throughput_mbps: float = 0.0
    prbs: float = 0.0
    gnb_eff: float = 0.0
    gnb_eff_mbps_per_prb: float = 0.0
    our_eff: float = 0.0
    dl_bler: float = 0.0
    dl_mcs: float = 0.0

    # --- steering outputs (Step-2) ---
    steering_target_mbps: float = 0.0
    steering_deficit_mbps: float = 0.0
    steering_expected_gain_mbps: float = 0.0
    steering_role: str = "none"
    steering_sla_violated: bool = False
    steering_total_demand_mbps: float = 0.0
    steering_gap_mbps: float = 0.0
    steering_active: bool = False
    steering_traffic_delta_mbps: float = 0.0
    offered_mbps: float = 0.0

    # --- CAP (already in your code) ---
    cap_effective_prb: float = 0.0
    cap_ratio: Optional[float] = None

    # --- pricing / cost abstraction (NEW) ---
    # This is your guaranteed PRB level B_min (can be fixed per gNB or per-slice policy)
    bmin_prb: float = 0.0

    # Outputs of the pricing model for this gNB at time t
    scarcity: float = 1.0
    cost: float = 0.0
    cost_min: float = 0.0
    cost_max: float = 0.0

    # Decomposition of used PRBs into guaranteed vs best-effort
    guaranteed_prb: float = 0.0
    best_effort_prb: float = 0.0

    # --- internal previous/current counters ---
    _prev_pdcp: float = 0.0
    _prev_mac: float = 0.0
    _prev_ts: Optional[float] = None

    _curr_pdcp: float = 0.0
    _curr_mac: float = 0.0
    _curr_ts: Optional[float] = None
    _curr_prbs: float = 0.0
    _curr_gnb_eff: float = 0.0

    # --- window accumulators so PRBs match the same window as byte deltas ---
    _prb_sum: float = 0.0
    _prb_n: int = 0

    # --- control window history ---
    _throughput_hist: deque = field(default_factory=lambda: deque(maxlen=200))
    _prbs_hist: deque = field(default_factory=lambda: deque(maxlen=200))
    _eff_mbps_per_prb_hist: deque = field(default_factory=lambda: deque(maxlen=200))

    # ---------------------------
    # Metrics computation (yours)
    # ---------------------------
    def calc_smoothed_metric(
        self, current_bytes: float, prev_bytes: float, dt: float, demand: float
    ) -> float:
        if dt <= 0:
            return 0.0

        delta = current_bytes - prev_bytes
        if delta < 0:
            return 0.0

        instant_mbps = (delta * 8) / (dt * 1e6)
        return float(instant_mbps)

    def calc_our_efficiency(self, delta_bytes: float, avg_prbs: float) -> float:
        if avg_prbs <= 0:
            return 0.0
        raw_bytes_per_prb = delta_bytes / avg_prbs
        return raw_bytes_per_prb / 1000.0

    def calc_mbps_per_prb(self) -> float:
        if self.gnb_eff <= 0 or self.slots_per_second <= 0:
            return 0.0
        return float(self.gnb_eff * 8.0 * self.slots_per_second / 1e6)

    def update_sample(self, ue: object, ts: float) -> None:
        self._curr_pdcp = float(getattr(ue, "dl_pdcp_sdu_bytes", 0.0))
        self._curr_mac = float(getattr(ue, "dl_total_bytes", 0.0))

        prb = float(getattr(ue, "avg_prbs_dl", 0.0))
        self._curr_prbs = prb

        self._curr_gnb_eff = float(getattr(ue, "avg_tbs_per_prb_dl", 0.0))
        self.dl_bler = float(getattr(ue, "dl_bler", 0.0) or 0.0)
        self.dl_mcs = float(getattr(ue, "dl_mcs", 0.0) or 0.0)
        self._curr_ts = ts
        self.last_seen_ts = ts

        # accumulate PRB samples across indications (so later we can average over the same window)
        self._prb_sum += prb
        self._prb_n += 1

    def compute_metrics(self, demand: float) -> dict:
        if self._curr_ts is None:
            return self.to_metrics_row()

        self.gnb_eff = self._curr_gnb_eff
        self.gnb_eff_mbps_per_prb = self.calc_mbps_per_prb()

        # Keep offered/target/gap consistent even when steering action is not triggered.
        if self.offered_mbps <= 0.0 and demand > 0.0:
            self.offered_mbps = float(demand)

        # Use PRBs averaged over the window since last compute (not just the last sample)
        if self._prb_n > 0:
            self.prbs = self._prb_sum / self._prb_n
        else:
            self.prbs = self._curr_prbs

        if self._prev_ts is None:
            self._prev_pdcp = self._curr_pdcp
            self._prev_mac = self._curr_mac
            self._prev_ts = self._curr_ts

            # reset window accumulators after initialization
            self._prb_sum = 0.0
            self._prb_n = 0

            self.steering_target_mbps = float(self.offered_mbps)
            self.steering_gap_mbps = float(self.throughput_mbps - self.offered_mbps)
            self.steering_deficit_mbps = max(0.0, float(self.offered_mbps - self.throughput_mbps))

            return self.to_metrics_row()

        dt = self._curr_ts - self._prev_ts

        self.goodput_mbps = self.calc_smoothed_metric(
            self._curr_pdcp, self._prev_pdcp, dt, demand
        )
        self.throughput_mbps = self.calc_smoothed_metric(
            self._curr_mac, self._prev_mac, dt, demand
        )

        delta_pdcp_bytes = self._curr_pdcp - self._prev_pdcp
        if delta_pdcp_bytes < 0:
            delta_pdcp_bytes = 0.0

        self.our_eff = self.calc_our_efficiency(delta_pdcp_bytes, self.prbs)

        # Baseline steering observability (overridden by compute_traffic_steering when it runs)
        self.steering_target_mbps = float(self.offered_mbps)
        self.steering_gap_mbps = float(self.throughput_mbps - self.offered_mbps)
        self.steering_deficit_mbps = max(0.0, float(self.offered_mbps - self.throughput_mbps))

        self._throughput_hist.append(self.throughput_mbps)
        self._prbs_hist.append(self.prbs)
        self._eff_mbps_per_prb_hist.append(self.gnb_eff_mbps_per_prb)

        # advance prev
        self._prev_pdcp = self._curr_pdcp
        self._prev_mac = self._curr_mac
        self._prev_ts = self._curr_ts

        # reset PRB window after computing
        self._prb_sum = 0.0
        self._prb_n = 0

        return self.to_metrics_row()

    # ---------------------------
    # CAP application (yours)
    # ---------------------------
    def apply_cap(self, cap_prb: Optional[float]) -> None:
        if cap_prb is None:
            return
        self.cap_effective_prb = float(cap_prb)
        if self.cell_total_prbs > 0:
            ratio = 100.0 * self.cap_effective_prb / self.cell_total_prbs
            self.cap_ratio = max(0.0, min(100.0, ratio))
        else:
            self.cap_ratio = None

    # ---------------------------
    # Export
    # ---------------------------
    def to_metrics_row(self) -> dict:
        return {
            "gnb_id": self.gnb_id,
            "goodput": self.goodput_mbps,
            "throughput": self.throughput_mbps,
            "prbs": self.prbs,
            "gnb_eff": self.gnb_eff,
            "gnb_eff_mbps_per_prb": self.gnb_eff_mbps_per_prb,
            "our_eff": self.our_eff,
            "dl_bler": self.dl_bler,
            "dl_mcs": self.dl_mcs,
            "cap_ratio": self.cap_ratio,
            "cap_effective_prb": self.cap_effective_prb,

            # steering outputs
            "steering_target_mbps": self.steering_target_mbps,
            "steering_deficit_mbps": self.steering_deficit_mbps,
            "steering_expected_gain_mbps": self.steering_expected_gain_mbps,
            "steering_role": self.steering_role,
            "steering_sla_violated": self.steering_sla_violated,
            "steering_total_demand_mbps": self.steering_total_demand_mbps,
            "steering_gap_mbps": self.steering_gap_mbps,
            "steering_active": self.steering_active,
            "steering_traffic_delta_mbps": self.steering_traffic_delta_mbps,
            "offered_mbps": self.offered_mbps,

            # pricing outputs
            "bmin_prb": self.bmin_prb,
            "scarcity": self.scarcity,
            "cost": self.cost,
            "cost_min": self.cost_min,
            "cost_max": self.cost_max,
            "guaranteed_prb": self.guaranteed_prb,
            "best_effort_prb": self.best_effort_prb,
        }
