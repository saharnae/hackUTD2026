"""Reference model of the 0x20 status frame in docs/FRAMES.md (33-byte payload).

Frame: 0x5A, 0x20, 33, payload, CRC16 (big-endian). CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF)
over type, len and payload (the sync byte is NOT included).
Payload offsets: 0 now_us u32 | 4 state | 5 trip_reason | 6 last_reject | 7 tokens | 8 snapshot_id u16 |
10 ref_seq u16 | 12 feed_seq u16 | 14 S u21 (3 bytes, cents Q4) | 17 equity_x6400 s48 | 23 orders_sent u16 |
25 fills u16 | 27 last_latency_ns u20 (3 bytes) | 30 frames_bad | 31 config_rejects | 32 log_drops.
State bits: 0 armed, 1 tripped, 2 book_ready, 3 snapshot_ok, 4 hb_ok, 5 shock_busy, 6 ARM switch, 7 kill pin.
"""
import struct

def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

FIELDS = ["now_us", "state", "trip_reason", "last_reject", "tokens", "snapshot_id", "ref_seq", "feed_seq",
          "S", "equity_x6400", "orders_sent", "fills", "last_latency_ns", "frames_bad", "config_rejects", "log_drops"]

def pack_payload(now_us=0, state=0, trip_reason=0, last_reject=0, tokens=0, snapshot_id=0, ref_seq=0, feed_seq=0,
                 S=0, equity_x6400=0, orders_sent=0, fills=0, last_latency_ns=0, frames_bad=0, config_rejects=0, log_drops=0):
    p = struct.pack(">IBBBBHHH", now_us & 0xFFFFFFFF, state, trip_reason, last_reject, tokens, snapshot_id, ref_seq, feed_seq)
    p += (S & 0x1FFFFF).to_bytes(3, "big") + (equity_x6400 & 0xFFFFFFFFFFFF).to_bytes(6, "big")
    p += struct.pack(">HH", orders_sent & 0xFFFF, fills & 0xFFFF) + (min(last_latency_ns, 999_999) & 0xFFFFF).to_bytes(3, "big")
    p += bytes([frames_bad & 0xFF, config_rejects & 0xFF, log_drops & 0xFF])
    assert len(p) == 33
    return p

def unpack_payload(p):
    assert len(p) == 33
    now_us, state, tr, lr, tok, sid, rs, fs = struct.unpack(">IBBBBHHH", p[:14])
    eq = int.from_bytes(p[17:23], "big", signed=True)
    return dict(now_us=now_us, state=state, trip_reason=tr, last_reject=lr, tokens=tok, snapshot_id=sid, ref_seq=rs, feed_seq=fs,
                S=int.from_bytes(p[14:17], "big"), equity_x6400=eq, orders_sent=int.from_bytes(p[23:25], "big"),
                fills=int.from_bytes(p[25:27], "big"), last_latency_ns=int.from_bytes(p[27:30], "big"),
                frames_bad=p[30], config_rejects=p[31], log_drops=p[32])

def status_frame(**kw):
    payload = pack_payload(**kw)
    body = bytes([0x20, len(payload)]) + payload
    return bytes([0x5A]) + body + struct.pack(">H", crc16(body))

if __name__ == "__main__":
    assert crc16(b"123456789") == 0x29B1, "CRC check value wrong"
    f = status_frame(now_us=1000, state=0b00011111, trip_reason=0, tokens=20, last_latency_ns=600)
    assert len(f) == 38
    assert unpack_payload(f[3:36])["last_latency_ns"] == 600
    print("CRC check value OK (0x29B1); example frame (38 bytes):", f.hex(" "))
