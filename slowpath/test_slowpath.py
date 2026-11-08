"""Tests for slowpath snapshot and UART frames. Fixed seeds, no I/O."""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frames as fr  # noqa: E402
import snapshot as sn  # noqa: E402
from snapshot import g  # noqa: E402  (golden, imported by snapshot.py)

SEED = 20261107


def _snap(spot=g.SPOT, ref_seq=1234, sid=42):
    return sn.build_snapshot(spot, ref_seq, sid)


def _payload_for(ftype, rng):
    if ftype == fr.T_SNAPSHOT:
        return sn.to_payload(_snap())
    if ftype == fr.T_FEED_WORD:
        return g.build_word(2, 7, 1000, 3, 500, 502, 10, 20).to_bytes(16, "big")
    if ftype == fr.T_CONFIG:
        return fr.encode_config(fr.CONFIG_DEFAULTS)
    n = 2 + 2 * rng.randint(1, 126) if ftype == fr.T_LOAD_BLOCK else fr.FIXED_LEN[ftype]
    return bytes(rng.randrange(256) for _ in range(n))


def test_crc16_check_value():
    assert fr.crc16_ccitt(b"123456789") == 0x29B1


@pytest.mark.parametrize("ftype", sorted(fr.FRAME_TYPES))
def test_round_trip_every_type(ftype):
    rng = random.Random(SEED + ftype)
    payload = _payload_for(ftype, rng)
    frame = fr.encode(ftype, payload)
    assert fr.decode(frame) == (ftype, payload)
    dec = fr.FrameDecoder()
    assert dec.feed(frame) == [(ftype, payload)]


def test_round_trip_kill_empty_and_max_load_block():
    assert fr.decode(fr.encode(fr.T_KILL, b"")) == (fr.T_KILL, b"")
    for n in (4, 254):
        payload = bytes(range(n))
        assert fr.decode(fr.encode(fr.T_LOAD_BLOCK, payload)) == (fr.T_LOAD_BLOCK, payload)


def test_snapshot_payload_round_trip():
    for spot, rs, sid in ((g.SPOT, 0, 0), (140.0, 65535, 65535), (155.0, 17, 900)):
        s = sn.build_snapshot(spot, rs, sid)
        p = sn.to_payload(s)
        assert len(p) == 95
        assert sn.from_payload(p) == s


def test_snapshot_frame_is_100_bytes():
    frame = fr.encode(fr.T_SNAPSHOT, sn.to_payload(_snap()))
    assert len(frame) == 100
    assert frame[0] == 0x5A and frame[1] == 0x10 and frame[2] == 95


def test_negative_delta_adj_layout():
    s = _snap()
    put = s["contracts"][4]
    assert put["delta_adj"] < 0
    p = sn.to_payload(s)
    off = 7 + 4 * 11 + 3                                  # contract 4, delta_adj field
    assert int.from_bytes(p[off:off + 3], "big") == put["delta_adj"] & 0xFFFFFF
    assert p[off] == 0xFF                                 # sign-extended to 24 bits


def test_feed_word_helpers():
    w = g.build_word(1, 65535, 99, 8, 14713, 14714, 100, 200)
    assert fr.decode_feed_word(fr.decode(fr.encode_feed_word(w))[1]) == w


def test_corrupted_byte_rejected():
    rng = random.Random(SEED)
    frame = fr.encode(fr.T_SNAPSHOT, sn.to_payload(_snap()))
    for i in range(1, len(frame)):                        # every byte after sync
        bad = bytearray(frame)
        bad[i] ^= 1 << rng.randrange(8)
        with pytest.raises(ValueError):
            fr.decode(bytes(bad))
        assert fr.FrameDecoder().feed(bytes(bad)) == []


def test_garbage_before_frame_skipped():
    rng = random.Random(SEED + 1)
    frame = fr.encode(fr.T_HEARTBEAT, b"\x01\x02\x00\x2a")
    garbage = bytes(rng.randrange(256) for _ in range(300)) + b"\x5a\x5a\x10"
    dec = fr.FrameDecoder()
    got = dec.feed(garbage + frame)
    assert got[-1] == (fr.T_HEARTBEAT, b"\x01\x02\x00\x2a")
    assert dec.bytes_skipped >= len(garbage) - 3


def test_hunts_next_sync_after_bad_crc():
    good1 = fr.encode(fr.T_KILL, b"")
    bad = bytearray(fr.encode(fr.T_CONFIG, fr.encode_config(fr.CONFIG_DEFAULTS)))
    bad[-1] ^= 0xFF
    good2 = fr.encode(fr.T_CONTROL, b"\x01\x00")
    dec = fr.FrameDecoder()
    assert dec.feed(good1 + bytes(bad) + good2) == [(fr.T_KILL, b""), (fr.T_CONTROL, b"\x01\x00")]
    assert dec.bad_frames == 1


def test_frame_split_across_feeds():
    frame = fr.encode(fr.T_SNAPSHOT, sn.to_payload(_snap()))
    dec, got = fr.FrameDecoder(), []
    for i in range(len(frame)):
        got += dec.feed(frame[i:i + 1])
    assert got == [(fr.T_SNAPSHOT, frame[3:-2])]


def test_wrong_length_for_fixed_type_rejected():
    with pytest.raises(ValueError):
        fr.encode(fr.T_SNAPSHOT, bytes(94))
    body = bytes([fr.T_SNAPSHOT, 4]) + b"abcd"
    forged = b"\x5a" + body + fr.crc16_ccitt(body).to_bytes(2, "big")
    assert fr.FrameDecoder().feed(forged) == []


EXPECTED_LEN = {0x10: 95, 0x11: 4, 0x12: 10, 0x14: 8, 0x15: 0, 0x16: 2, 0x17: 16, 0x20: 33, 0x21: 20}


def _forged(ftype, payload):
    body = bytes([ftype, len(payload)]) + payload
    return b"Z" + body + fr.crc16_ccitt(body).to_bytes(2, "big")


def test_length_table_matches_frames_md():
    assert fr.FIXED_LEN == EXPECTED_LEN
    assert set(fr.FIXED_LEN) | {fr.T_LOAD_BLOCK} == set(fr.FRAME_TYPES)


@pytest.mark.parametrize("ftype", sorted(EXPECTED_LEN))
def test_fixed_length_enforced_per_type(ftype):
    n = EXPECTED_LEN[ftype]
    assert fr.decode(fr.encode(ftype, bytes(n))) == (ftype, bytes(n))     # framing only, not field ranges
    for bad_n in {n - 1, n + 1, 0, 40} - {n, -1}:
        with pytest.raises(ValueError, match=f"0x{ftype:02X}.*must be {n} bytes, got {bad_n}"):
            fr.encode(ftype, bytes(bad_n))
        forged = _forged(ftype, bytes(bad_n))             # good CRC, wrong length
        with pytest.raises(ValueError, match="bad type or length"):
            fr.decode(forged)
        dec = fr.FrameDecoder()
        assert dec.feed(forged) == [] and dec.bad_frames >= 1


def test_load_block_length_rule():
    for n in (4, 6, 100, 254):
        assert fr.decode(fr.encode(fr.T_LOAD_BLOCK, bytes(n)))[1] == bytes(n)
    for n in (0, 1, 2, 3, 5, 253, 255):
        with pytest.raises(ValueError, match=r"2 \+ 2N bytes"):
            fr.encode(fr.T_LOAD_BLOCK, bytes(n))
        assert fr.FrameDecoder().feed(_forged(fr.T_LOAD_BLOCK, bytes(n))) == []


def test_bad_length_frame_does_not_hide_next_frame():
    good = fr.encode(fr.T_HEARTBEAT, b"\x00\x01\x00\x00")
    dec = fr.FrameDecoder()
    assert dec.feed(_forged(fr.T_HEARTBEAT, b"\x00\x01") + good) == [(fr.T_HEARTBEAT, b"\x00\x01\x00\x00")]
    assert dec.bad_frames >= 1


def test_config_round_trip_and_ranges():
    assert fr.decode_config(fr.encode_config(fr.CONFIG_DEFAULTS)) == fr.CONFIG_DEFAULTS
    assert fr.CONFIG_DEFAULTS["edge_threshold"] == g.EDGE_THRESHOLD
    for name, _, _, lo, hi in fr.CONFIG_FIELDS:
        for v in (lo - 1, hi + 1):
            p = dict(fr.CONFIG_DEFAULTS, **{name: v})
            if name == "size_cap_base":
                p["size_cap_max"] = 10
            with pytest.raises(ValueError):
                fr.decode_config(fr.encode_config(p))
    with pytest.raises(ValueError, match="size_cap_max < size_cap_base"):
        fr.decode_config(fr.encode_config(dict(fr.CONFIG_DEFAULTS, size_cap_base=8, size_cap_max=6)))


def test_config_ignored_while_armed():
    reg = fr.ConfigRegister()
    new = dict(fr.CONFIG_DEFAULTS, edge_threshold=512, max_orders_per_event=1)
    assert reg.apply(fr.encode_config(new), armed=True) is False
    assert reg.params == fr.CONFIG_DEFAULTS and reg.config_rejects == 1
    assert reg.apply(fr.encode_config(new), armed=False) is True
    assert reg.params == new and reg.config_rejects == 1
    bad = fr.encode_config(dict(new, time_stop_us=999))
    assert reg.apply(bad, armed=False) is False                 # out of range: ignored in full
    assert reg.params == new and reg.config_rejects == 2


def test_random_stream_fuzz():
    rng = random.Random(SEED + 2)
    types = sorted(fr.FRAME_TYPES)
    stream, expect = bytearray(), []
    for _ in range(300):
        stream += bytes(rng.randrange(256) for _ in range(rng.randint(0, 5)))
        t = rng.choice(types)
        p = _payload_for(t, rng)
        f = bytearray(fr.encode(t, p))
        if rng.random() < 0.2:
            f[rng.randrange(1, len(f))] ^= 1 << rng.randrange(8)
        else:
            expect.append((t, p))
        stream += f
    got = fr.FrameDecoder().feed(bytes(stream))
    # every good frame is recovered in order; random garbage may rarely forge extras
    it = iter(got)
    assert all(any(x == e for x in it) for e in expect)


@pytest.mark.parametrize("spot", [140.0, g.SPOT, 155.0])
def test_quantization_error_within_one_step(spot):
    s = sn.build_snapshot(spot, 1, 1)
    ref = g.build_snapshot(S_ref_dollars=spot)
    assert s["S_ref"] == ref["S_ref"]
    assert abs(g.from_fixed(s["S_ref"], g.FMT_S) - spot * 100) <= 1 / 16
    for q, f in zip(s["contracts"], ref["contracts"]):
        for key, fmt, val in (("theo_0", g.FMT_THEO, f["price"] * 100),
                              ("delta_adj", g.FMT_DELTA_ADJ, f["delta_adj_f"]),
                              ("gamma_c", g.FMT_GAMMA_C, f["gamma"] / 100),
                              ("vega", g.FMT_VEGA, f["vega_f"] * 100)):
            step = 1 / (1 << fmt.frac)
            assert abs(g.from_fixed(q[key], fmt) - val) <= step, (key, q[key], val)


def test_snapshot_range_checks():
    with pytest.raises(ValueError):
        sn.build_snapshot(g.SPOT, 1 << 16, 0)
    with pytest.raises(ValueError):
        sn.from_payload(bytes(94))
