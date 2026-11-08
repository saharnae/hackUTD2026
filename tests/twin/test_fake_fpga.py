"""Tests for tools/fake_fpga.py: SOFTWARE TWIN, NOT THE FPGA. SIMULATED arena; seeds 0-79 only."""

import io
import json
import os
import socket
import sys
import threading
import time
from contextlib import redirect_stdout

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in ("tools", "slowpath", "golden", "sim"):
    sys.path.insert(0, os.path.join(ROOT, p))

import fake_fpga as ff  # noqa: E402
import frames as fr  # noqa: E402
import gen_status_frame as gsf  # noqa: E402
import golden as g  # noqa: E402
import read_status as rs  # noqa: E402  (imported, not edited)
import snapshot as sn  # noqa: E402

HB = 500
ARM_T = 100_500.0            # just after the 100 ms snapshot: R02 holds (the board needs snapshot_ok at the edge)


def run_twin(seed=3, regime="normal", switch=(), frames_in=(), **kw):
    buf = bytearray()
    tw = ff.Twin(seed, HB, regime, emit=buf.extend, quiet=True, **kw)
    tw.scripted_local = sorted(switch)
    tw.scripted_frames = sorted(frames_in, key=lambda x: x[0])
    res = tw.run()
    return tw, res, bytes(buf)


def decode_all(b):
    d = fr.FrameDecoder()
    out = d.feed(b)
    return out, d


def statuses(b):
    return [gsf.unpack_payload(p) for t, p in decode_all(b)[0] if t == fr.T_STATUS]


def logs(b):
    return [ff.unpack_log(p) for t, p in decode_all(b)[0] if t == fr.T_LOG]


def raw_frame(ftype, payload, length=None, crc_ok=True):
    """Hand-built frame for malformed cases; CRC from slowpath.frames (not re-implemented)."""
    body = bytes([ftype, len(payload) if length is None else length]) + bytes(payload)
    crc = fr.crc16_ccitt(body) ^ (0 if crc_ok else 0x0101)
    return bytes([fr.SYNC]) + body + crc.to_bytes(2, "big")


def every_frame_type():
    """Every frame type (valid) plus malformed ones. Kill is excluded (it would latch, see its own test)."""
    snap = sn.to_payload(sn.build_snapshot(g.SPOT, 1, 1))
    word = g.build_word(1, 5, 100, 8, g.SPOT_CENTS, g.SPOT_CENTS + 1, 10, 10)
    good = [
        fr.encode(fr.T_SNAPSHOT, snap),
        fr.encode(fr.T_HEARTBEAT, b"\x00\x01\x00\x01"),
        fr.encode(fr.T_CONFIG, fr.encode_config(fr.CONFIG_DEFAULTS)),
        fr.encode(fr.T_LOAD_BLOCK, b"\x20\x00\x12\x34"),
        fr.encode(fr.T_HEDGE_ORDER, b"\x00\x07\x00\x00\x64" + (g.SPOT_CENTS + 1).to_bytes(3, "big")),
        fr.encode(fr.T_CONTROL, b"\x01\x00"), fr.encode(fr.T_CONTROL, b"\x02\x0a"),
        fr.encode(fr.T_CONTROL, b"\x03\x01"), fr.encode(fr.T_CONTROL, b"\x7f\xff"),   # unknown op
        fr.encode(fr.T_CONTROL, b"\xff\x01"),
        fr.encode(fr.T_FEED_WORD, word.to_bytes(16, "big")),
        fr.encode(fr.T_STATUS, gsf.pack_payload(state=0xFF)),                          # "armed" bits from host
        fr.encode(fr.T_LOG, bytes(20)),
    ]
    bad = [
        raw_frame(fr.T_CONFIG, fr.encode_config(fr.CONFIG_DEFAULTS), crc_ok=False),     # bad CRC
        raw_frame(fr.T_CONFIG, fr.encode_config(fr.CONFIG_DEFAULTS)[:9]),              # bad length
        raw_frame(fr.T_KILL, b"\x01"),                                                 # kill with a payload
        raw_frame(0x33, b"\x01\x02"), raw_frame(0x00, b""), raw_frame(0xFF, bytes(5)),  # unknown types
        raw_frame(fr.T_CONFIG, b"\xff" * 10),                                          # good CRC, out of range
        raw_frame(fr.T_STATUS, bytes(32)), raw_frame(fr.T_HEARTBEAT, bytes(4), length=200),
        fr.encode(fr.T_HEARTBEAT, b"\x00\x02\x00\x00")[:5],                            # truncated
        b"\x5a\x5a\x5a\x00\x5a\x12", bytes(range(256)),                                # garbage
    ]
    return good, bad


# ---------------------------------------------------------------- round trip
def test_frames_round_trip_both_decoders():
    tw, res, b = run_twin(switch=[(ARM_T, "a")])
    out, d = decode_all(b)
    assert d.bad_frames == 0 and d.bytes_skipped == 0 and out
    buf, got = bytearray(b), []
    assert rs.parse_stream(buf, got) == 0                      # tools/read_status.py decoder, valid CRC
    assert got == out
    assert b == b"".join(fr.encode(t, p) for t, p in out)      # byte-identical re-encode
    st = [p for t, p in out if t == fr.T_STATUS]
    assert st and all(len(fr.encode(fr.T_STATUS, p)) == 38 for p in st)
    for p in st:
        assert rs.show(fr.T_STATUS, p).startswith("now=")
    assert {t for t, _ in out} == {fr.T_STATUS, fr.T_LOG}
    assert res["source"] == "software_twin" and res["timing"] == "twin timing, not FPGA latency"


def test_log_records_match_engine():
    tw, res, b = run_twin(seed=0, switch=[(ARM_T, "a")])
    recs = logs(b)
    dec = [r for r in recs if r["rec_type"] == 1]
    assert dec and all(r["flags"] & ff.F_APPROVED == (r["reason"] == 0 and not r["flags"] & ff.F_BLOCKED) for r in dec)
    fills = [r for r in recs if r["rec_type"] == 2 and r["qty"] > 0]
    assert len(fills) == len(tw.eng.fills)


# ---------------------------------------------------------------- counters ("sequence")
def test_status_counters_increment_like_the_rtl():
    tw, res, b = run_twin(seed=0, switch=[(ARM_T, "a")])
    sts = statuses(b)
    periodic = [s["now_us"] for s in sts if s["now_us"] % ff.STATUS_PERIOD_US == 0]
    assert periodic == list(range(0, periodic[-1] + 1, ff.STATUS_PERIOD_US))       # one per period, in order
    assert all(a <= b_ for a, b_ in zip([s["now_us"] for s in sts], [s["now_us"] for s in sts][1:]))
    recs = logs(b)
    assert sts[-1]["orders_sent"] == sum(1 for r in recs if r["rec_type"] == 1 and r["flags"] & ff.F_APPROVED)
    assert sts[-1]["fills"] == sum(1 for r in recs if r["rec_type"] == 2 and r["qty"] > 0)
    assert sts[-1]["feed_seq"] == tw.eng.last_seq & 0xFFFF and sts[-1]["snapshot_id"] == 6
    for a, b_ in zip(sts, sts[1:]):
        assert b_["orders_sent"] >= a["orders_sent"] and b_["fills"] >= a["fills"]
    tw.approved, tw.fill_reports, tw.frames_bad_extra = 65536 + 3, 65535 + 2, 1000
    f = tw.status_fields()
    assert f["orders_sent"] == 3 and f["fills"] == 1 and f["frames_bad"] == 255    # u16 wrap, u8 saturate


# ---------------------------------------------------------------- determinism
def test_deterministic_per_seed():
    _, r1, b1 = run_twin(seed=5, switch=[(ARM_T, "a")])
    _, r2, b2 = run_twin(seed=5, switch=[(ARM_T, "a")])
    _, _, b3 = run_twin(seed=6, switch=[(ARM_T, "a")])
    assert b1 == b2 and r1["determinism_hash"] == r2["determinism_hash"]
    assert b1 != b3


def test_seed_guard():
    for s in (80, 99, -1):
        with pytest.raises(ff.SeedRefused):
            ff.Twin(s, HB)
    with redirect_stdout(io.StringIO()):
        assert ff.main(["--seed", "80", "--hedge-band", "500", "--out-file", os.devnull]) == 2


# ---------------------------------------------------------------- arming
def test_never_arms_from_any_frame():
    good, bad = every_frame_type()
    burst = b"".join(good + bad)
    times = [0.0, 50_000.0, ARM_T, 100_600.0, 101_000.0, 150_000.0, 200_400.0, 300_300.0, 450_000.0]
    tw, res, b = run_twin(seed=0, frames_in=[(t, burst) for t in times])
    assert not res["ever_armed"] and not tw.eng.armed
    assert all(not s["state"] & 1 for s in statuses(b))
    assert not any(r["flags"] & ff.F_APPROVED for r in logs(b))
    assert tw.decoder.bad_frames > 0 and statuses(b)[-1]["frames_bad"] > 0
    assert tw.cfg.config_rejects > 0                          # out-of-range config counted
    assert tw.host_hb["valid"] >= 1 and tw.ignored          # valid frames were seen and handled


def test_arms_only_on_local_rising_edge_while_ready():
    _, res, b = run_twin(seed=0, switch=[(ARM_T, "a")])
    assert res["ever_armed"] and any(s["state"] & 1 for s in statuses(b))
    _, res, _ = run_twin(seed=0, switch=[(0.0, "a")])        # ARM already up before ready: no edge later
    assert not res["ever_armed"]
    _, res, _ = run_twin(seed=0, switch=[(0.0, "a"), (150_000.0, "d"), (200_500.0, "a")])
    assert res["ever_armed"]                                  # low then high again while ready


def test_frames_cannot_reach_the_switch_or_rearm():
    src = open(os.path.join(ROOT, "tools", "fake_fpga.py"), encoding="utf-8").read()
    frame_fn = src[src.index("def _apply_frame"):src.index("def _drain_inputs")]
    for forbidden in ("local_rearm", "_edge_now", "arm_switch", "local_key", "armed = True"):
        assert forbidden not in frame_fn
    assert src.count("local_rearm()") == 1                    # only the local KEY0 path calls it


# ---------------------------------------------------------------- kill
def test_kill_frame_latches_and_flattens():
    kill = fr.encode(fr.T_KILL)
    tw, res, b = run_twin(seed=0, switch=[(ARM_T, "a"), (300_000.0, "d"), (300_100.0, "c")],
                          frames_in=[(150_000.0, kill)])
    e = tw.eng
    assert e.latched and e.kill and res["trip_reasons"] == [15]
    after = [s for s in statuses(b) if s["now_us"] >= 150_000]
    assert after and all(s["state"] & 0b10 and s["state"] & 0x80 and s["trip_reason"] == 15 for s in after)
    assert not any(r["flags"] & ff.F_APPROVED for r in logs(b) if r["rec_type"] == 1 and r["ts_us"] >= 150_000)
    assert res["final_position"] == [0] * 8 and res["final_shares"] == 0
    assert e.latched                                          # KEY0 with ARM low refused while kill is set (TW7)


def test_kill_while_disarmed_still_latches():
    tw, res, b = run_twin(seed=1, frames_in=[(10_000.0, fr.encode(fr.T_KILL))])
    assert tw.eng.latched and res["trip_reasons"] == [15] and not res["ever_armed"]


# ---------------------------------------------------------------- config and bad frames
def test_config_applies_only_while_disarmed_and_in_range():
    cfg = dict(fr.CONFIG_DEFAULTS, edge_threshold=512, min_move=4)
    f = fr.encode(fr.T_CONFIG, fr.encode_config(cfg))
    tw, _, _ = run_twin(seed=0, frames_in=[(1_000.0, f)])
    assert tw.eng.p["edge_threshold_ticks"] == 2.0 and tw.eng.p["min_move_cents"] == 4 and tw.eng.thr == 512
    assert tw.cfg.config_rejects == 0
    tw, _, _ = run_twin(seed=0, switch=[(ARM_T, "a")], frames_in=[(ARM_T + 5_000, f)])
    assert tw.cfg.config_rejects == 1 and tw.eng.p["edge_threshold_ticks"] == 1.5     # armed: ignored
    assert tw.eng.p["hedge_band_shares"] == HB


def test_bad_length_frames_are_dropped_and_decoder_recovers():
    bad_len = raw_frame(fr.T_CONFIG, fr.encode_config(fr.CONFIG_DEFAULTS)[:9])     # good CRC, wrong length
    hb = fr.encode(fr.T_HEARTBEAT, b"\x00\x09\x00\x00")
    tw, _, b = run_twin(seed=0, frames_in=[(1_000.0, bad_len + hb + raw_frame(fr.T_KILL, b"\x00") + hb[:4]),
                                           (2_000.0, hb[4:])])
    assert tw.decoder.bad_frames >= 2 and tw.host_hb["valid"] == 1 and not tw.eng.kill
    assert statuses(b)[-1]["frames_bad"] >= 2 and tw.cfg.config_rejects == 0


# ---------------------------------------------------------------- labels and transports
def test_console_labels_and_banner(tmp_path):
    out = io.StringIO()
    with redirect_stdout(out):
        assert ff.main(["--seed", "2", "--hedge-band", "500", "--out-file", str(tmp_path / "t.bin"),
                        "--switch", "a@100500"]) == 0
    lines = out.getvalue().splitlines()
    assert lines and all(x.startswith("[TWIN] ") for x in lines)
    assert "SOFTWARE TWIN, NOT THE FPGA" in lines[0]
    docs = [json.loads(x[7:]) for x in lines if x[7:].startswith("{")]
    assert docs and all(d["source"] == "software_twin" and d["timing"] == "twin timing, not FPGA latency" for d in docs)
    assert all("twin_ns_last" in d for d in docs if d["kind"] == "status")
    b = (tmp_path / "t.bin").read_bytes()
    assert decode_all(b)[1].bad_frames == 0 and statuses(b)


def test_tcp_loopback_server():
    tw = ff.Twin(4, HB, quiet=True, out=io.StringIO())
    port = {}
    ready = threading.Event()
    th = threading.Thread(target=ff.serve, args=(tw, 0, 0.0),
                          kwargs=dict(stdin=False, ready=lambda p: (port.setdefault("p", p), ready.set()), idle_s=0.5),
                          daemon=True)
    with redirect_stdout(io.StringIO()):
        th.start()
        assert ready.wait(10)
        c = socket.create_connection(("127.0.0.1", port["p"]), timeout=10)
        c.sendall(fr.encode(fr.T_KILL))
        data = bytearray()
        t_end = time.time() + 20
        while time.time() < t_end:
            try:
                d = c.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not d:
                break
            data += d
        c.close()
        th.join(10)
    sts = statuses(bytes(data))
    assert sts and decode_all(bytes(data))[1].bad_frames == 0
    assert any(s["trip_reason"] == 15 for s in sts) and not any(s["state"] & 1 for s in sts)
