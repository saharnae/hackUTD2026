"""0x20 status / 0x21 log payload codecs (docs/FRAMES.md layouts, PROPOSED) and frame helpers.

0x20 payload: reuses tools/gen_status_frame.py pack_payload / unpack_payload (33 bytes).
0x21 payload: 20 bytes, encoded and decoded here (no codec existed in the repo).
Envelope: slowpath/frames.py encode / FrameDecoder (sync, type, length, CRC-16).
"""

from ... import _paths  # noqa: F401
import frames as F
import gen_status_frame as GS

STATE_BITS = ("armed", "tripped", "book_ready", "snapshot_ok", "hb_ok", "shock_busy", "arm_switch", "kill_pin")
FLAG_BITS = ("approved", "reduce_only", "blocked", "source_0x14")


def status_frame(fields):
    return F.encode(F.T_STATUS, GS.pack_payload(**fields))


def decode_status(payload):
    d = GS.unpack_payload(bytes(payload))
    d["state_bits"] = {n: (d["state"] >> i) & 1 for i, n in enumerate(STATE_BITS)}
    d["equity_dollars"] = d["equity_x6400"] / 6400.0
    d["S_cents"] = d["S"] / 16.0
    return d


def encode_log_record(r):
    p = bytes([r["rec_type"] & 0xFF]) + (int(r["ts_us"]) & 0xFFFFFFFF).to_bytes(4, "big")
    instr = ((r["is_hedge"] & 1) << 7) | ((r["side"] & 1) << 4) | (r["contract"] & 7)
    p += bytes([instr]) + (r["qty"] & 0xFFFF).to_bytes(2, "big") + (r["price"] & 0x3FFFF).to_bytes(3, "big")
    p += bytes([r["reason"] & 0x0F, r["flags"] & 0xFF]) + (r["fail_mask"] & 0x7FFF).to_bytes(2, "big")
    p += (r["client_id"] & 0xFFFF).to_bytes(2, "big") + bytes([r["tokens"] & 0xFF, r["orders_in_event"] & 0xFF, 0])
    assert len(p) == F.FIXED_LEN[F.T_LOG]
    return p


def decode_log_record(p):
    p = bytes(p)
    if len(p) != F.FIXED_LEN[F.T_LOG]:
        raise ValueError("0x21 payload is 20 bytes")
    instr = p[5]
    flags = p[12]
    return {"rec_type": p[0], "kind": {1: "decision", 2: "fill"}.get(p[0], "unknown"),
            "ts_us": int.from_bytes(p[1:5], "big"), "is_hedge": instr >> 7, "side": (instr >> 4) & 1,
            "contract": instr & 7, "qty": int.from_bytes(p[6:8], "big"), "price": int.from_bytes(p[8:11], "big"),
            "reason": p[11], "flags": flags, "flag_bits": {n: (flags >> i) & 1 for i, n in enumerate(FLAG_BITS)},
            "fail_mask": int.from_bytes(p[13:15], "big"), "client_id": int.from_bytes(p[15:17], "big"),
            "tokens": p[17], "orders_in_event": p[18]}


def log_frame(rec):
    return F.encode(F.T_LOG, encode_log_record(rec))


def decode_stream(decoder, data):
    """-> (list of decoded status dicts, list of decoded log records, list of other (type, payload))."""
    st, logs, other = [], [], []
    for t, p in decoder.feed(data):
        if t == F.T_STATUS:
            st.append(decode_status(p))
        elif t == F.T_LOG:
            logs.append(decode_log_record(p))
        else:
            other.append((t, p))
    return st, logs, other
