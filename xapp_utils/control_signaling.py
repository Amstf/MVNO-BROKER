import importlib

from xapp_utils.xapp_control import send_socket

ran_messages_pb2 = importlib.import_module("oai-oran-protolib.builds.ran_messages_pb2")


def trigger_indication():
    msg = ran_messages_pb2.RAN_message()
    msg.msg_type = ran_messages_pb2.RAN_message_type.INDICATION_REQUEST
    inner = ran_messages_pb2.RAN_indication_request()
    inner.target_params.extend([
        ran_messages_pb2.RAN_parameter.GNB_ID,
        ran_messages_pb2.RAN_parameter.UE_LIST,
    ])
    msg.ran_indication_request.CopyFrom(inner)
    return msg.SerializeToString()


def trigger_slicing_control(sst=1, sd=2, min_ratio=20, max_ratio=80):
    slicing = ran_messages_pb2.slicing_control_m()
    slicing.sst = sst
    if sd:
        slicing.sd = sd
    slicing.min_ratio = min_ratio
    slicing.max_ratio = max_ratio

    ctrl = ran_messages_pb2.RAN_param_map_entry()
    ctrl.key = ran_messages_pb2.RAN_parameter.SLICING_CONTROL
    ctrl.slicing_ctrl.CopyFrom(slicing)

    inner = ran_messages_pb2.RAN_control_request()
    inner.target_param_map.append(ctrl)

    msg = ran_messages_pb2.RAN_message()
    msg.msg_type = ran_messages_pb2.RAN_message_type.CONTROL
    msg.ran_control_request.CopyFrom(inner)

    return msg.SerializeToString()


def wrap_control_with_meid(meid: str, payload: bytes) -> bytes:
    """
    Prepend a tiny routing header so the connector can set MEID without
    decoding the protobuf payload.
    Format: [1 byte MEID length][MEID bytes][2 byte payload length][payload bytes]
    """
    if not meid:
        raise ValueError("MEID is required to route control payloads")
    meid_bytes = meid.encode("utf-8")
    if len(meid_bytes) > 255:
        raise ValueError("MEID length must fit in one byte")
    payload_len = len(payload)
    if payload_len > 65535:
        raise ValueError("Payload length must fit in two bytes")
    return bytes([len(meid_bytes)]) + meid_bytes + payload_len.to_bytes(2, "big") + payload


def send_slice_ctrl(sock, meid: str, sst=1, sd=2, min_ratio=20, max_ratio=80):
    """
    Send a slicing control message with explicit min/max ratios.
    """
    meid_bytes = meid.encode("utf-8") if meid else b""
    payload = trigger_slicing_control(sst=sst, sd=sd, min_ratio=min_ratio, max_ratio=max_ratio)
    buf = wrap_control_with_meid(meid, payload)
    header_len = 1 + len(meid_bytes) + 2
    if (not buf or buf[0] != len(meid_bytes)
            or buf[1:1 + len(meid_bytes)] != meid_bytes
            or buf[1 + len(meid_bytes):header_len] != len(payload).to_bytes(2, "big")):
        raise RuntimeError("Control buffer MEID header mismatch")
    send_socket(sock, buf)
    print(f"📢 Sent slice control: sst={sst}, sd={sd}, min={min_ratio}, max={max_ratio}")
    return min_ratio, max_ratio


