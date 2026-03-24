#!/usr/bin/env python3

import argparse
import importlib
import json
import os
import socket
import time
from time import sleep
from xapp_runtime.gnb_runtime_state import GnbState
from gnb_utils.gnb_identity import extract_gnb_id
from gnb_utils.ue_management import (
    check_iface_in_pod,
    cleanup_ues,
    configure_ue_settings,
    get_ordered_startup_placements,
    init_k8s,
    start_ue_in_pod,
    start_ue_traffic,
)
from modules.price_model import PriceModel
from xapp_utils.control_signaling import trigger_indication
from xapp_utils.metrics_utils import print_report, print_ue_report
from xapp_utils.xapp_control import open_control_socket, receive_from_socket

from xapp_runtime.actuation_engine import (
    _apply_eff_override_to_states,
    _apply_pricing_to_state,
    apply_action_plan,
    apply_static_action_plan,
    compute_system_healthy,
)
from xapp_runtime.config_contract import (
    _load_loop_config,
    _resolve_targets,
    _validate_broker_cfg_keys,
    normalize_gnb_id,
)
from xapp_runtime.cap_runtime import build_cap_runtime, plan_cap_for_tick
from xapp_runtime.broker.decision import broker_step
from xapp_runtime.broker.static_decision import static_broker_step

from xapp_runtime.persistence_bridge import (
    _build_decision_doc,
    build_gnb_state_doc,
    init_gnb_state_collection,
    mongo_insert_one,
)
from xapp_runtime.snapshot import _build_tick_snapshot, evaluate_snapshot_freshness
from xapp_runtime.ue_aggregation import build_aggregated_ue_sample

ran_messages_pb2 = importlib.import_module("oai-oran-protolib.builds.ran_messages_pb2")


def parse_args():
    p = argparse.ArgumentParser(description="xApp main: dynamic (default) or static (--static)")
    p.add_argument("--static", action="store_true", help="Run static behavior (equivalent to legacy static loop)")
    p.add_argument("--config", dest="config_path", default="conf/config_loop.json", help="Path to config_loop.json")
    p.add_argument("--traffic", dest="traffic_config_path", default=None, help="Path to background traffic config")
    p.add_argument("--ue-placment", "--ue-placement", dest="ue_placement_path", default=None, help="Path to UE placement config")
    p.add_argument("--ue-rate", type=float, default=None, help="Override initial UE rate (Mbps)")
    p.add_argument("--eff", type=float, default=None, help="Override efficiency (Mbps/PRB)")
    p.add_argument("--collection", default="gnb_state", help="Mongo collection name for gNB state docs")
    p.add_argument("--reports", "--report", dest="reports", type=int, default=None, help="Stop after N ticks")
    p.add_argument("--sla", action="store_true", help="SLA-only mode: disable cost rebalancing")
    p.add_argument("--ue-debug", action="store_true", help="Print per-UE indication values instead of gNB report")
    return p.parse_args()


def _send_indication(udp_sock):
    buf = trigger_indication()
    udp_sock.sendto(buf, ("127.0.0.1", 7001))


def _advance_startup_state(runtime_state: dict, duration_s: int) -> None:
    ss = runtime_state["startup_state"]

    if ss["phase"] == "init":
        ordered = get_ordered_startup_placements()
        ss["ordered_ues"] = ordered
        ss["pending_idx"] = 0
        if not ordered:
            runtime_state["startup_complete"] = True
            ss["phase"] = "all_done"
            return
        first = ordered[0]
        gnb_id = str(first["gnb_id"])
        ss["rntis_before"] = set(runtime_state["current_rntis_by_gnb"].get(gnb_id, set()))
        start_ue_in_pod(first["pod"], first["gnb_ip"], first["conf"])
        print(f"[STARTUP] Starting UE {first.get('logical_id')} on gNB {gnb_id} (pod={first['pod']})")
        ss["phase"] = "waiting_oaitun"
        return

    if ss["phase"] == "waiting_oaitun":
        idx = ss["pending_idx"]
        current_ue = ss["ordered_ues"][idx]
        pod = current_ue["pod"]
        logical_id = str(current_ue.get("logical_id") or pod)
        if check_iface_in_pod(pod, "oaitun_ue1"):
            print(f"[STARTUP] {logical_id} oaitun_ue1 is UP — waiting for RNTI")
            ss["phase"] = "waiting_rnti"
        return

    if ss["phase"] == "waiting_rnti":
        idx = ss["pending_idx"]
        current_ue = ss["ordered_ues"][idx]
        gnb_id = str(current_ue["gnb_id"])
        logical_id = str(current_ue.get("logical_id") or current_ue["pod"])
        current_rntis = runtime_state["current_rntis_by_gnb"].get(gnb_id, set())
        new_rntis = current_rntis - ss["rntis_before"]
        if not new_rntis:
            return
        new_rnti = next(iter(new_rntis))
        runtime_state["rnti_to_ue"][(gnb_id, new_rnti)] = logical_id
        runtime_state["ue_to_rnti"][logical_id] = (gnb_id, new_rnti)
        rnti_int = int(new_rnti, 16)
        correct_rate = float(runtime_state.get("ue_rate", {}).get(logical_id, 0.0))
        ue_demand_state_ref = runtime_state.get("ue_demand_state", {})
        ue_demand_state_ref.setdefault(
            gnb_id, {"rates": [], "next_idx": 0, "by_rnti": {}}
        )["by_rnti"][rnti_int] = correct_rate
        print(f"[STARTUP] Set demand: {logical_id} RNTI {new_rnti} "
              f"on gNB {gnb_id} → {correct_rate} Mbps")
        print(f"[STARTUP] Mapped {logical_id} → RNTI {new_rnti} on gNB {gnb_id}")
        next_idx = idx + 1
        if next_idx < len(ss["ordered_ues"]):
            ss["pending_idx"] = next_idx
            next_ue = ss["ordered_ues"][next_idx]
            next_gnb = str(next_ue["gnb_id"])
            ss["rntis_before"] = set(runtime_state["current_rntis_by_gnb"].get(next_gnb, set()))
            start_ue_in_pod(next_ue["pod"], next_ue["gnb_ip"], next_ue["conf"])
            print(f"[STARTUP] Starting next UE {next_ue.get('logical_id')} on gNB {next_gnb} (pod={next_ue['pod']})")
            ss["phase"] = "waiting_oaitun"
        else:
            print("[STARTUP] All UEs mapped. Starting iperf3 on all UEs in parallel.")
            from concurrent.futures import ThreadPoolExecutor

            def _start_one(pl):
                start_ue_traffic(
                    str(pl["pod"]),
                    rate_mbps=int(round(float(pl.get("initial_rate_mbps", 10)))),
                    direction="downlink",
                    port=int(pl["port"]),
                    duration_s=duration_s,
                )

            with ThreadPoolExecutor(max_workers=len(ss["ordered_ues"])) as ex:
                list(ex.map(_start_one, ss["ordered_ues"]))
            runtime_state["startup_complete"] = True
            ss["phase"] = "all_done"
            print("[STARTUP] startup_complete=True. Ticks are now valid.")
        return


def main():
    args = parse_args()
    static_mode = bool(args.static)

    config = _load_loop_config(args.config_path)
    conf_root = os.path.dirname(__file__)
    traffic_path = args.traffic_config_path or os.path.join(conf_root, "conf", "background_traffic_gnb.json")
    with open(traffic_path, "r", encoding="utf-8") as fp:
        traffic_cfg = json.load(fp)

    ue_placement_path = args.ue_placement_path or os.path.join(conf_root, "conf", "ue_placement.conf")
    with open(ue_placement_path, "r", encoding="utf-8") as fp:
        ue_placement_cfg = json.load(fp)

    gnb_targets, logical_to_meid, meid_to_logical = _resolve_targets(config)
    target_logical_ids = tuple(logical_to_meid.keys())
    target_set = set(target_logical_ids)

    broker_cfg = config.get("broker", {})
    _validate_broker_cfg_keys(broker_cfg)

    tick_period_s = float(broker_cfg.get("tick_period_s", 0.5))
    if tick_period_s <= 0:
        raise ValueError("broker.tick_period_s must be > 0")

    cap_step_period_s = float(broker_cfg.get("cap_step_period_s", tick_period_s))
    if cap_step_period_s <= 0:
        raise ValueError("broker.cap_step_period_s must be > 0")

    cap_control_period_s = float(broker_cfg.get("cap_control_period_s", tick_period_s))
    if cap_control_period_s <= 0:
        raise ValueError("broker.cap_control_period_s must be > 0")

    broker_decision_every_n_ticks = int(broker_cfg.get("broker_decision_every_n_ticks", 1))
    if broker_decision_every_n_ticks <= 0:
        raise ValueError("broker.broker_decision_every_n_ticks must be > 0")

    snapshot_require_all_gnbs = bool(broker_cfg.get("snapshot_require_all_gnbs", True))
    max_freshness_ms = float(broker_cfg.get("max_freshness_ms", 2.0 * tick_period_s * 1000.0))
    warmup_ticks = int(broker_cfg.get("warmup_ticks", 0))

    policy_cfg = {
        "steering_tolerance": float(broker_cfg.get("steering_tolerance", 0.1)),
        "steering_step_mbps": float(broker_cfg.get("steering_step_mbps", 5.0)),
        "observe_window_ticks": int(broker_cfg.get("observe_window_ticks", 3)),
        "cost_headroom_usage_ratio": float(broker_cfg.get("cost_headroom_usage_ratio", 0.9)),
        "sla_only": bool(args.sla),
        "ue_debug": bool(args.ue_debug),
    }


    pricing_cfg = traffic_cfg.get("pricing", config.get("pricing", {}))
    price_model = PriceModel.from_config_dict(pricing_cfg)
    bmin_by_op = config.get("bmin_prb", {"21": 10.0, "223": 10.0})
    clamp_throughput_cfg_enabled = bool(config.get("clamp_throughput_enabled", True))
    clamp_throughput_enabled = bool(static_mode and clamp_throughput_cfg_enabled)
    clamp_throughput_mbps = float(config.get("clamp_throughput_mbps", 35.0))
    min_prb_by_op = config.get("min_prb", {"21": 10.0, "223": 10.0})

    cap_cfg_raw = traffic_cfg.get("cap_generator", {})
    scenario_step_limit = int(cap_cfg_raw.get("n_steps", 0) or 0)
    cap_generators, cap_scenarios = build_cap_runtime(
        cap_cfg_raw=cap_cfg_raw,
        target_logical_ids=target_logical_ids,
    )

    ue_control = config["ue_control"]
    by_operator = ue_control.get("by_operator", {})
    initial_rate_mbps = int(round(float(ue_control.get("initial_rate_mbps", 10))))
    if args.ue_rate is not None:
        initial_rate_mbps = int(round(float(args.ue_rate)))
    duration_s = int(ue_control.get("duration_s", 8000))

    placements = list(ue_placement_cfg.get("placements", []))
    if not placements:
        raise ValueError("ue placement config missing placements")

    per_op_initial_rate = {op_id: 0 for op_id in target_logical_ids}
    for pl in placements:
        pod = str(pl.get("pod", "")).strip()
        op_id = str(pl.get("gnb_id", "")).strip()
        role = str(pl.get("role", "")).strip().lower()
        if not pod:
            raise ValueError("each placement must include pod")
        if "port" not in pl:
            raise ValueError(f"placement for pod {pod} missing port")
        if op_id == "Ghost_UE" or role == "ghost":
            continue
        if op_id not in target_set:
            raise ValueError(f"placement for pod {pod} has unknown gnb_id {op_id}")
        rate = float(pl.get("initial_rate_mbps", initial_rate_mbps))
        per_op_initial_rate[op_id] += int(round(rate))

    for op_id in target_logical_ids:
        if op_id not in by_operator:
            raise ValueError(f"ue_control.by_operator missing operator {op_id}")

    gnb_states = {}
    for op_id, target in gnb_targets.items():
        cell_total_prbs = float(target.get("cell_total_prbs", 0.0))
        gnb_states[str(op_id)] = GnbState(
            gnb_id=str(op_id),
            cell_total_prbs=cell_total_prbs,
        )

    latest_rows = {}
    latest_ue_by_gnb = {}
    last_ue_rates = {op_id: int(per_op_initial_rate.get(op_id, initial_rate_mbps)) for op_id in target_logical_ids}

    configure_ue_settings({"placements": placements})
    sleep(1)
    init_k8s()

    for op_id in target_logical_ids:
        gnb_states[op_id].offered_mbps = float(last_ue_rates.get(op_id, initial_rate_mbps))

    slice_cfg = config.get("slice", {})
    sst = int(slice_cfg.get("sst", 1))
    sd_str = slice_cfg.get("sd_hex") or slice_cfg.get("sd") or "0"
    sd = int(str(sd_str), 16)

    mongo_client, gnb_col = init_gnb_state_collection(col_name=args.collection)
    decision_col_name = f"{args.collection}_broker_decision"
    _, decision_col = init_gnb_state_collection(client=mongo_client, col_name=decision_col_name)
    ghost_col_name = f"{args.collection}_ghost_events"
    _, ghost_event_col = init_gnb_state_collection(
        client=mongo_client,
        col_name=ghost_col_name,
    )
    from xapp_runtime.actuation_engine import set_ghost_event_col
    set_ghost_event_col(ghost_event_col)

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _send_indication(udp_sock)
    control_sck = open_control_socket(4200)

    total_reports = 0
    tick_id = 0
    valid_tick_count = 0
    tick_start_ts = time.time()
    tick_deadline = tick_start_ts + tick_period_s
    cap_step_deadline = tick_start_ts + cap_step_period_s
    cap_control_deadline = tick_start_ts + cap_control_period_s
    pending_cap_plan = {}
    cap_steps_generated = 0
    seen_gnbs = set()

    runtime_state = {
        "last_ue_rates": last_ue_rates,
        "ue_pods": {op_id: (by_operator[op_id]["pod"], int(by_operator[op_id]["port"])) for op_id in target_logical_ids},
        "gnb_states": gnb_states,
        "duration_s": duration_s,
        "min_delta_mbps": float(broker_cfg.get("min_rate_delta_mbps", 0.0)),
        "max_rate_step_mbps_per_tick": float(broker_cfg.get("max_rate_step_mbps_per_tick", 5.0)),
        "ghost_move_timeout_s": float(broker_cfg.get("ghost_move_timeout_s", 30)),
        "ghost_retry_wait_s": float(broker_cfg.get("ghost_retry_wait_s", 5)),
        "ue_debug": bool(args.ue_debug),
        "ue_profiles": {
            str(pl.get("logical_id") or pl.get("pod")): {
                "pod": str(pl.get("pod")),
                "gnb_id": str(pl.get("gnb_id")),
                "gnb_ip": str(pl.get("gnb_ip", "")),
                "conf": str(pl.get("conf", "")),
                "port": int(pl.get("port")),
                "role": str(pl.get("role", "ghost" if str(pl.get("gnb_id")) == "Ghost_UE" else "active")).lower(),
                "initial_rate_mbps": float(pl.get("initial_rate_mbps", 0.0)),
            }
            for pl in placements
        },
        "ghost_ue_id": next(
            (
                str(pl.get("logical_id") or pl.get("pod"))
                for pl in placements
                if str(pl.get("gnb_id")) == "Ghost_UE" or str(pl.get("role", "")).lower() == "ghost"
            ),
            None,
        ),
        "ue_role": {
            str(pl.get("logical_id") or pl.get("pod")): str(
                pl.get("role", "ghost" if str(pl.get("gnb_id")) == "Ghost_UE" else "active")
            ).lower()
            for pl in placements
        },
        "ue_rate": {
            str(pl.get("logical_id") or pl.get("pod")): float(pl.get("initial_rate_mbps", 0.0))
            for pl in placements
        },
        "ue_gnb_id": {
            str(pl.get("logical_id") or pl.get("pod")): str(pl.get("gnb_id"))
            for pl in placements
        },
        "gnb_ip_map": {
            str(pl.get("gnb_id")): str(pl.get("gnb_ip"))
            for pl in placements
            if str(pl.get("role", "")).lower() != "ghost"
            and str(pl.get("gnb_id")) not in ("Ghost_UE", "")
        },
        "rnti_to_ue": {},
        "ue_to_rnti": {},
        "current_rntis_by_gnb": {},
        "startup_complete": False,
        "startup_state": {
            "phase": "init",
            "ordered_ues": [],
            "pending_idx": 0,
            "rntis_before": set(),
        },
    }

    if static_mode:
        fixed = {op_id: int(last_ue_rates.get(op_id, initial_rate_mbps)) for op_id in target_logical_ids}
        runtime_state["fixed_rates"] = dict(fixed)
        policy_state = {"fixed_rates": dict(fixed)}
    else:
        policy_state = {"sla_ok_tick_streak": 0}

    runtime_state["ue_role"] = policy_state.get("ue_role", runtime_state.get("ue_role", {}))
    runtime_state["ue_rate"] = policy_state.get("ue_rate", runtime_state.get("ue_rate", {}))
    runtime_state["ue_gnb_id"] = policy_state.get("ue_gnb_id", runtime_state.get("ue_gnb_id", {}))

    ue_counter_state = {op_id: {} for op_id in target_logical_ids}
    agg_counter_state = {op_id: {"mac_total": 0.0, "pdcp_total": 0.0} for op_id in target_logical_ids}
    ue_demand_state = {
        op_id: {
            "rates": [
                float(pl.get("initial_rate_mbps", initial_rate_mbps))
                for pl in placements
                if str(pl.get("gnb_id")) == op_id and str(pl.get("gnb_id")) != "Ghost_UE" and str(pl.get("role", "")).lower() != "ghost"
            ],
            "next_idx": 0,
            "by_rnti": {},
        }
        for op_id in target_logical_ids
    }

    runtime_state["ue_demand_state"] = ue_demand_state

    try:
        while True:
            data = receive_from_socket(control_sck)
            if not data:
                continue

            resp = ran_messages_pb2.RAN_indication_response()
            resp.ParseFromString(data)

            ue_info_list = []
            raw_gnb_id = None
            for entry in resp.param_map:
                if entry.key == ran_messages_pb2.RAN_parameter.GNB_ID:
                    raw_gnb_id = extract_gnb_id(entry)
                    break

            logical_id, meid = normalize_gnb_id(raw_gnb_id, logical_to_meid, meid_to_logical)
            if logical_id is None:
                print("[LOOP] indication skipped: non-target gNB")
                _send_indication(udp_sock)
                continue

            now = time.time()
            for entry in resp.param_map:
                if entry.key == ran_messages_pb2.RAN_parameter.UE_LIST:
                    ue_info_list.extend(entry.ue_list.ue_info)

            raw_rntis = set()
            for ue_entry in ue_info_list:
                rnti_val = getattr(ue_entry, "rnti", None)
                if rnti_val:
                    raw_rntis.add(hex(int(rnti_val)))
            runtime_state["current_rntis_by_gnb"][logical_id] = raw_rntis
            _advance_startup_state(runtime_state, duration_s)

            if not ue_info_list:
                _send_indication(udp_sock)
                sleep(0.1)
                continue

            gnb_state = gnb_states[logical_id]
            gnb_state.meid = meid
            gnb_state.report_index += 1
            gnb_state.tick_id = int(tick_id)
            gnb_state.tick_ts = float(tick_deadline)
            aggregated_sample, ue_debug_rows = build_aggregated_ue_sample(
                op_id=logical_id,
                ue_info_list=ue_info_list,
                now=now,
                gnb_state=gnb_state,
                ue_counter_state=ue_counter_state,
                agg_counter_state=agg_counter_state,
                ue_demand_state=ue_demand_state,
                default_ue_rate=float(initial_rate_mbps),
                rnti_to_ue=runtime_state.get("rnti_to_ue", {}),
                ue_rate_map=runtime_state.get("ue_rate", {}),
            )
            latest_ue_by_gnb[logical_id] = ue_debug_rows
            gnb_state.update_sample(aggregated_sample, now)

            _apply_eff_override_to_states(gnb_states, args.eff)

            demand_for_this_gnb = float(last_ue_rates.get(logical_id, initial_rate_mbps))
            metrics_row = gnb_state.compute_metrics(demand_for_this_gnb)
            if args.eff is not None:
                metrics_row["gnb_eff_mbps_per_prb_override"] = float(args.eff)
            throughput_mbps = float(
                metrics_row.get("throughput_mbps", metrics_row.get("throughput", 0.0)) or 0.0
            )
            if clamp_throughput_enabled:
                throughput_mbps = min(clamp_throughput_mbps, throughput_mbps)
            metrics_row["throughput_mbps"] = throughput_mbps
            metrics_row["throughput"] = metrics_row["throughput_mbps"]
            metrics_row["goodput_mbps"] = float(
                metrics_row.get("goodput_mbps", metrics_row.get("goodput", 0.0)) or 0.0
            )
            metrics_row["goodput"] = metrics_row["goodput_mbps"]
            metrics_row["offered_mbps"] = float(getattr(gnb_state, "offered_mbps", demand_for_this_gnb))
            metrics_row["tick_id"] = tick_id
            metrics_row["tick_ts"] = float(tick_deadline)

            _apply_pricing_to_state(
                gnb_state=gnb_state,
                metrics_row=metrics_row,
                op_id=logical_id,
                bmin_by_op=bmin_by_op,
                price_model=price_model,
            )

            latest_rows[logical_id] = metrics_row

            total_reports += 1
            seen_gnbs.add(logical_id)

            if now >= tick_deadline:
                if not runtime_state.get("startup_complete", False):
                    print("[STARTUP] Waiting for UE attachment... skipping tick.")
                    tick_ts = tick_deadline
                    lag_s = max(0.0, now - tick_ts)
                    skipped = int(lag_s // tick_period_s)
                    tick_deadline = tick_ts + (skipped + 1) * tick_period_s
                    seen_gnbs = set()
                else:
                    if not static_mode:
                        policy_state["report_count"] = total_reports
                    tick_ts = tick_deadline
                    missing = sorted(target_set - seen_gnbs)
                    seen_ok = len(missing) == 0
                    snapshot = _build_tick_snapshot(
                        tick_id=tick_id,
                        tick_ts=tick_ts,
                        target_logical_ids=target_logical_ids,
                        gnb_states=gnb_states,
                        latest_rows=latest_rows,
                        slice_sla_mbps=float(broker_cfg.get("slice_sla_mbps", sum(last_ue_rates.values()))),
                        total_demand_mbps=float(sum(last_ue_rates.values())),
                    )

                    freshness_ok, freshness_by_gnb, valid_snapshot = evaluate_snapshot_freshness(
                        snapshot=snapshot,
                        target_logical_ids=target_logical_ids,
                        snapshot_require_all_gnbs=snapshot_require_all_gnbs,
                        max_freshness_ms=max_freshness_ms,
                        seen_ok=seen_ok,
                    )

                    if not static_mode:
                        print("COSTS", {k: (snapshot["per_gnb"][k]["cost"], snapshot["per_gnb"][k]["scarcity"]) for k in target_logical_ids})

                        if valid_snapshot and float(snapshot.get("total_throughput", 0.0)) >= float(snapshot.get("slice_sla_mbps", 0.0)):
                            policy_state["sla_ok_tick_streak"] = int(policy_state.get("sla_ok_tick_streak", 0)) + 1
                        else:
                            policy_state["sla_ok_tick_streak"] = 0

                    if static_mode:
                        system_healthy = True
                    else:
                        system_healthy = compute_system_healthy(runtime_state)

                    if static_mode:
                        action_plan, policy_state = static_broker_step(
                            snapshot=snapshot,
                            policy_state=policy_state,
                            valid_snapshot=valid_snapshot,
                        )
                    elif tick_id < warmup_ticks:
                        action_plan = {"reason": "hold", "desired_rates": {op: float(snapshot["per_gnb"][op].get("offered_mbps", 0.0) or 0.0) for op in snapshot.get("per_gnb", {})}, "slice_ctrl_updates": {}}
                    elif (tick_id % broker_decision_every_n_ticks) != 0:
                        action_plan = {
                            "reason": "hold_broker_interval",
                            "desired_rates": {
                                op: float(snapshot["per_gnb"][op].get("offered_mbps", 0.0) or 0.0)
                                for op in snapshot.get("per_gnb", {})
                            },
                            "slice_ctrl_updates": {},
                        }
                    else:
                        action_plan, policy_state = broker_step(
                            snapshot=snapshot,
                            policy_cfg=policy_cfg,
                            policy_state=policy_state,
                            valid_snapshot=valid_snapshot,
                            placements=placements,
                            system_healthy=system_healthy,
                        )
                        runtime_state["ue_state"] = policy_state.get("ue_state", {})
                        runtime_state["ue_role"] = policy_state.get("ue_role", runtime_state.get("ue_role", {}))
                        runtime_state["ue_rate"] = policy_state.get("ue_rate", runtime_state.get("ue_rate", {}))
                        runtime_state["ue_gnb_id"] = policy_state.get("ue_gnb_id", runtime_state.get("ue_gnb_id", {}))

                    if valid_snapshot:
                        for op_id in target_logical_ids:
                            if op_id not in latest_rows:
                                continue
                            row = dict(latest_rows[op_id])
                            row["tick_id"] = int(tick_id)
                            row["tick_ts"] = float(tick_ts)
                            st = gnb_states[op_id]
                            st.tick_id = int(tick_id)
                            st.tick_ts = float(tick_ts)
                            mongo_insert_one(gnb_col, build_gnb_state_doc(st, row))

                        cap_plan = plan_cap_for_tick(
                            target_logical_ids=target_logical_ids,
                            gnb_states=gnb_states,
                            cap_generators=cap_generators,
                            cap_scenarios=cap_scenarios,
                            min_prb_by_op=min_prb_by_op,
                        )
                        cap_steps_generated += 1
                        pending_cap_plan = cap_plan["slice_ctrl_updates"]
                        # Also advance cap_step_deadline if enough time has passed
                        if now >= cap_step_deadline:
                            cap_step_deadline = cap_step_deadline + cap_step_period_s
                    elif now >= cap_step_deadline:
                        # Step caps independently when no broker snapshot but cap deadline reached
                        cap_plan = plan_cap_for_tick(
                            target_logical_ids=target_logical_ids,
                            gnb_states=gnb_states,
                            cap_generators=cap_generators,
                            cap_scenarios=cap_scenarios,
                            min_prb_by_op=min_prb_by_op,
                        )
                        cap_steps_generated += 1
                        pending_cap_plan = cap_plan["slice_ctrl_updates"]
                        cap_step_deadline = cap_step_deadline + cap_step_period_s

                    if now >= cap_control_deadline and pending_cap_plan:
                        action_plan["slice_ctrl_updates"] = pending_cap_plan
                        pending_cap_plan = {}
                        lag_cap_s = max(0.0, now - cap_control_deadline)
                        cap_control_deadline = cap_control_deadline + cap_control_period_s

                    if static_mode:
                        runtime_state, actuation = apply_static_action_plan(
                            action_plan=action_plan,
                            runtime_state=runtime_state,
                            control_sck=control_sck,
                            sst=sst,
                            sd=sd,
                            logical_to_meid=logical_to_meid,
                        )
                    else:
                        runtime_state, actuation = apply_action_plan(
                            action_plan=action_plan,
                            runtime_state=runtime_state,
                            control_sck=control_sck,
                            sst=sst,
                            sd=sd,
                            logical_to_meid=logical_to_meid,
                        )

                    reason = action_plan["reason"]

                    deficit = max(0.0, float(snapshot["slice_sla_mbps"]) - float(snapshot["total_throughput"]))
                    print(
                        f"[TICK] id={tick_id} valid={valid_snapshot} reason={reason} "
                        f"deficit={deficit:.2f} rates_before={actuation['rates_before']} rates_after={actuation['rates_after']}"
                    )

                    if valid_snapshot:
                        report_rows = [latest_rows[op_id] for op_id in target_logical_ids if op_id in latest_rows]
                        if report_rows:
                            if args.ue_debug:
                                print_ue_report(
                                    latest_ue_by_gnb,
                                    target_logical_ids,
                                    rnti_to_ue=runtime_state.get("rnti_to_ue", {}),
                                )
                            else:
                                print_report(report_rows)
                            if not static_mode:
                                cost_items = [
                                    f"{op_id}={snapshot['per_gnb'][op_id].get('cost', 0.0):.4f}"
                                    for op_id in target_logical_ids
                                ]
                                print(f"[BROKER COST] {', '.join(cost_items)}")

                    action_plan_doc = dict(action_plan)
                    action_plan_doc["reason"] = reason
                    decision_doc = _build_decision_doc(
                        tick_id=tick_id,
                        valid_snapshot=valid_snapshot,
                        snapshot=snapshot,
                        action_plan=action_plan_doc,
                        actuation=actuation,
                        policy_state=policy_state,
                        system_healthy=system_healthy,
                    )
                    decision_doc["missing_gnbs"] = missing
                    decision_doc["max_freshness_ms"] = max_freshness_ms
                    decision_doc["freshness_ok"] = freshness_ok if snapshot_require_all_gnbs else True
                    decision_doc["freshness_by_gnb"] = freshness_by_gnb
                    mongo_insert_one(decision_col, decision_doc)

                    if valid_snapshot:
                        valid_tick_count += 1
                    tick_id += 1
                    tick_start_ts = tick_ts
                    for st in gnb_states.values():
                        st.tick_id = int(tick_id)
                        st.tick_ts = float(tick_ts)

                    lag_s = max(0.0, now - tick_ts)
                    skipped = int(lag_s // tick_period_s)
                    tick_deadline = tick_ts + (skipped + 1) * tick_period_s
                    seen_gnbs = set()

            _send_indication(udp_sock)

            if args.reports is not None and valid_tick_count >= args.reports:
                print(f"[INFO] Reached valid-tick limit ({args.reports}); stopping.")
                break

            if scenario_step_limit > 0 and cap_steps_generated >= scenario_step_limit:
                print(f"[INFO] Reached cap-generator step limit ({scenario_step_limit}); stopping.")
                break
    finally:
        cleanup_ues()
        try:
            udp_sock.close()
        except Exception:
            pass
        try:
            mongo_client.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
