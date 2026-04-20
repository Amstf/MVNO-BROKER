"""Constants for the clean loop controller (Phase-1 pure extraction)."""

TARGET_LOGICAL_IDS = ("21", "223")
LOGICAL_TO_MEID = {
    "21": "gnb_734_733_30613230", # "a21"
    "223": "gnb_734_733_61323230", # "a223"
}
MEID_TO_LOGICAL = {v: k for k, v in LOGICAL_TO_MEID.items()}
TARGET_MEIDS = tuple(MEID_TO_LOGICAL.keys())

BROKER_ACTIVE_KEYS = {
    "tick_period_s",
    "snapshot_require_all_gnbs",
    "slice_sla_mbps",
    "steering_tolerance",
    "steering_step_mbps",
    "cost_hysteresis_ratio",
    "cost_headroom_fraction",
    "min_rate_delta_mbps",
    "max_rate_step_mbps_per_tick",
    "cost_min_headroom_prbs",
    "cost_headroom_usage_ratio",
    "max_freshness_ms",
    "observe_window_ticks",
    "ghost_move_timeout_s",
    "ghost_retry_wait_s",
    "warmup_ticks",
    "cap_step_period_s",
    "cap_control_period_s",
    "broker_decision_every_n_ticks",
}
BROKER_DEPRECATED_KEYS = {
    "decision_freq_reports",
    "decision_window_reports",
    "ctrl_freq_reports",
    "steering_cooldown_reports",
    "cost_cooldown_reports",
}
