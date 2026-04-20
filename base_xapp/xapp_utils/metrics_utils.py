import time


def _format_identifier(m: dict) -> str:
    gnb_id = m.get("gnb_id")
    if gnb_id is not None:
        return str(gnb_id)
    rnti = m.get("rnti")
    if rnti is None:
        return "unknown"
    return hex(rnti)


def print_report(metrics_list):
    if not metrics_list:
        return
    print(f"\n[QoS & RESOURCE REPORT] {time.strftime('%H:%M:%S')}")
    print("-" * 154)
    header = (
        f"{'GNB':<10} | {'THROUGHPUT':<12} | {'PRBs':<6} | {'CAP %':<6} | "
        f"{'TARGET':<8} | {'GAP':<8} | {'EFF':<8} | {'SLA OK':<7} | {'STEER':<12}"
    )
    print(header)
    print("-" * 154)
    for m in metrics_list:
        cap_ratio = m.get("cap_ratio")
        cap_display = f"{cap_ratio:.0f}" if cap_ratio is not None else "n/a"
        target = m.get("steering_target_mbps")
        target_display = f"{target:.1f}" if target is not None else "n/a"
        gap = m.get("steering_gap_mbps")
        gap_display = f"{gap:.1f}" if gap is not None else "n/a"
        eff = m.get("gnb_eff_mbps_per_prb")
        eff_display = f"{eff:.3f}" if eff is not None else "n/a"
        role = m.get("steering_role") or "none"
        sla_ok = "no" if m.get("steering_sla_violated") else "yes"
        print(
            f"{_format_identifier(m):<10} | {m['throughput']:>8.2f} Mbps | {m['prbs']:>6.1f} | "
            f"{cap_display:>6} | {target_display:>8} | {gap_display:>8} | {eff_display:>8} | "
            f"{sla_ok:>7} | {role:>12}"
        )


def print_ue_report(ue_by_gnb: dict, target_logical_ids: tuple, rnti_to_ue=None):
    if not ue_by_gnb:
        return
    if rnti_to_ue is None:
        rnti_to_ue = {}

    print(f"\n[UE DEBUG REPORT] {time.strftime('%H:%M:%S')}")
    print("-" * 212)
    header = (
        f"{'GNB':<8} | {'UE':<25} | {'RNTI':<8} | {'THROUGHPUT':<12} | {'DEMAND':<10} | {'GAP':<10} | "
        f"{'PRBs':<6} | {'TBS/PRB':<8} | {'DL_BLER':<8} | {'DL_MCS':<7}"
    )
    print(header)
    print("-" * 212)

    for op_id in target_logical_ids:
        rows = ue_by_gnb.get(op_id, [])
        for row in rows:
            gnb_id = str(row.get("gnb_id", op_id))
            rnti_val = row.get("rnti")
            rnti_hex = hex(int(rnti_val)) if rnti_val else "0x?"
            logical_id = rnti_to_ue.get((gnb_id, rnti_hex), "unknown")

            is_zombie = (
                float(row.get("throughput_mbps", 0.0) or 0.0) == 0.0
                and float(row.get("prbs", 0.0) or 0.0) == 0.0
            )

            if is_zombie:
                display_name = f"{logical_id} ({rnti_hex}) [zombie]"
                display_demand = 0.0
            else:
                display_name = f"{logical_id} ({rnti_hex})"
                display_demand = float(row.get("demand_mbps", row.get("demand", 0.0)) or 0.0)

            print(
                f"{gnb_id:<8} | {display_name:<25} | {rnti_hex:<8} | "
                f"{float(row.get('throughput_mbps', 0.0)):>8.2f} Mbps | {display_demand:>8.2f} | "
                f"{float(row.get('gap_mbps', row.get('gap', 0.0)) or 0.0):>8.2f} | {float(row.get('prbs', 0.0)):>6.2f} | "
                f"{float(row.get('tbs_per_prb', 0.0)):>8.3f} | {float(row.get('dl_bler', 0.0)):>8.3f} | {float(row.get('dl_mcs', 0.0)):>7.2f}"
            )
