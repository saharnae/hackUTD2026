"""Tests for the golden model. All randomness uses fixed seeds."""

import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golden as g  # noqa: E402

SEED = 20261107


def _random_bs_params(rng, n, max_abs_d1=3.0):
    """Random (S, K, T, r, q, sigma) with |d1| bounded so greeks are not vanishing."""
    out = []
    while len(out) < n:
        S = rng.uniform(20.0, 400.0)
        K = S * math.exp(rng.uniform(-0.3, 0.3))
        T = rng.uniform(2.0 / 365.0, 2.0)
        r = rng.uniform(0.0, 0.08)
        q = rng.uniform(0.0, 0.05)
        sigma = rng.uniform(0.08, 1.0)
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        if abs(d1) <= max_abs_d1:
            out.append((S, K, T, r, q, sigma))
    return out


# ---------------------------------------------------------------- Black-Scholes
def test_put_call_parity():
    rng = random.Random(SEED)
    cases = _random_bs_params(rng, 2000, max_abs_d1=10.0)
    cases += [(g.SPOT, K, g.T_YEARS, g.RATE, g.DIV, g.smile_sigma(math.log(K / g.SPOT)))
              for K in g.STRIKES]
    for S, K, T, r, q, sigma in cases:
        c = g.bs(S, K, T, r, q, sigma, True)
        p = g.bs(S, K, T, r, q, sigma, False)
        assert abs(c["price"] - p["price"] - (S * math.exp(-q * T) - K * math.exp(-r * T))) < 1e-9
        assert abs(c["delta"] - p["delta"] - math.exp(-q * T)) < 1e-12
        assert c["gamma"] == p["gamma"] and c["vega"] == p["vega"]


def test_finite_difference_delta_gamma():
    rng = random.Random(SEED + 1)
    for S, K, T, r, q, sigma in _random_bs_params(rng, 500):
        for is_call in (True, False):
            base = g.bs(S, K, T, r, q, sigma, is_call)
            h = S * 1e-5
            up = g.bs(S + h, K, T, r, q, sigma, is_call)
            dn = g.bs(S - h, K, T, r, q, sigma, is_call)
            fd_delta = (up["price"] - dn["price"]) / (2 * h)
            fd_gamma = (up["delta"] - dn["delta"]) / (2 * h)
            assert fd_delta == pytest.approx(base["delta"], rel=1e-6)
            assert fd_gamma == pytest.approx(base["gamma"], rel=1e-6)


def test_vega_and_theta_units():
    S, K, T, r, q, sigma = g.SPOT, g.STRIKES[1], g.T_YEARS, g.RATE, g.DIV, g.SMILE_A
    base = g.bs(S, K, T, r, q, sigma, True)
    h = 1e-4
    fd_vega = (g.bs(S, K, T, r, q, sigma + h, True)["price"]
               - g.bs(S, K, T, r, q, sigma - h, True)["price"]) / (2 * h) / 100.0
    assert fd_vega == pytest.approx(base["vega"], rel=1e-6)          # $ per vol point
    dt = 1e-6
    fd_theta = -(g.bs(S, K, T + dt, r, q, sigma, True)["price"]
                 - g.bs(S, K, T - dt, r, q, sigma, True)["price"]) / (2 * dt) / 365.0
    assert fd_theta == pytest.approx(base["theta"], rel=1e-6)        # $ per day


# ---------------------------------------------------------------- formats
@pytest.mark.parametrize("fmt", list(g.FORMATS.values()), ids=lambda f: f.name)
def test_fixed_roundtrip_and_saturation(fmt):
    step = 1.0 / (1 << fmt.frac)
    rng = random.Random(SEED + 2)
    for _ in range(2000):
        raw = rng.randint(fmt.lo, fmt.hi)
        assert g.to_fixed(g.from_fixed(raw, fmt), fmt) == raw
    assert g.to_fixed(fmt.hi * step * 4 + 1, fmt) == fmt.hi
    assert g.to_fixed(-abs(fmt.lo * step) * 4 - 1, fmt) == fmt.lo
    assert g.to_fixed(0.5 * step, fmt) == 1                          # round half up
    assert g.to_fixed(-0.5 * step, fmt) == (0 if fmt.lo <= 0 else fmt.lo)


def test_ds_saturation_from_spot():
    ref = g.to_fixed(g.SPOT * 100, g.FMT_S)
    assert g.ds_raw(ref, ref) == 0
    assert g.ds_raw(ref + 16383, ref) == 16383
    assert g.ds_raw(ref + 16384, ref) == g.DS_LIMIT
    assert g.ds_raw(ref - 50000, ref) == -g.DS_LIMIT
    assert g.from_fixed(g.DS_LIMIT, g.FMT_DS) == 1023.9375


# ---------------------------------------------------------------- reprice
def test_reprice_vs_float_random():
    rng = random.Random(SEED + 3)
    max_ds = 100 * 16                                   # |dS| <= 100 cents
    worst = 0.0
    for _ in range(100_000):
        theo0 = rng.randint(0, g.FMT_THEO.hi)
        d_adj = rng.randint(g.FMT_DELTA_ADJ.lo, g.FMT_DELTA_ADJ.hi)
        gam = rng.randint(0, (1 << 17) - 1)             # keeps g1 inside its saturation band
        dS = rng.randint(-max_ds, max_ds)
        got = g.reprice(theo0, d_adj, gam, dS) / 256.0
        ref = g.reprice_float(g.from_fixed(theo0, g.FMT_THEO), g.from_fixed(d_adj, g.FMT_DELTA_ADJ),
                              g.from_fixed(gam, g.FMT_GAMMA_C), g.from_fixed(dS, g.FMT_DS))
        worst = max(worst, abs(got - ref))
    assert worst <= 0.05, f"worst error {worst} ticks"


def test_reprice_ds_zero():
    rng = random.Random(SEED + 4)
    for _ in range(1000):
        theo0 = rng.randint(0, g.FMT_THEO.hi)
        assert g.reprice(theo0, rng.randint(-(1 << 17), (1 << 17) - 1),
                         rng.randint(0, (1 << 18) - 1), 0) == theo0


def test_reprice_at_ds_limits():
    theo0, d_adj, gam = 100 << 8, 1 << 15, (1 << 18) - 1   # 100 ticks, delta 0.5, max gamma
    for dS in (g.DS_LIMIT, -g.DS_LIMIT):
        v = g.reprice(theo0, d_adj, gam, dS)
        assert 0 <= v <= g.FMT_THEO.hi
    # gamma term saturates: g1 = +/-(2^17 - 1 or 2^17)
    assert g.sat(g.rshift_round(gam * g.DS_LIMIT, 11), -(1 << 17), (1 << 17) - 1) == (1 << 17) - 1
    # output clamps at both ends
    assert g.reprice(g.FMT_THEO.hi, (1 << 17) - 1, 0, g.DS_LIMIT) == g.FMT_THEO.hi
    assert g.reprice(0, (1 << 17) - 1, 0, -g.DS_LIMIT) == 0
    # a saturated dS reprices identically to any S further out
    ref = g.to_fixed(g.SPOT * 100, g.FMT_S)
    a = g.reprice(theo0, d_adj, 5000, g.ds_raw(ref + 20000, ref))
    b = g.reprice(theo0, d_adj, 5000, g.ds_raw(ref + 16383, ref))
    assert a == b


def test_reprice_theo_near_zero():
    # deep OTM put-like: tiny theo, positive move pushes below zero -> clamps at 0
    assert g.reprice(3, -(1 << 12), 0, 160) == 0
    # tiny theo, small positive contribution stays exact
    assert g.reprice(1, 1 << 12, 0, 16) == 1 + 16                    # 0.0625 * 1 cent = 16/256 tick
    # theo 0 and dS 0
    assert g.reprice(0, 0, 0, 0) == 0


def test_reprice_negative_delta():
    # -0.5 delta, +1 cent -> exactly -0.5 tick = -128
    assert g.reprice(1000, -(1 << 15), 0, 16) == 1000 - 128
    # -0.5 delta, -1 cent -> +128
    assert g.reprice(1000, -(1 << 15), 0, -16) == 1000 + 128
    # rounding half up on negatives: product -2048 -> (-2048 + 2048) >> 12 = 0
    assert g.rshift_round(-2048, 12) == 0
    assert g.rshift_round(-2049, 12) == -1
    assert g.reprice(1000, -2048, 0, 1) == 1000
    # snapshot puts: price falls as spot rises
    snap = g.build_snapshot()
    for c in snap["contracts"][4:]:
        assert c["delta_adj"] < 0
        assert g.reprice(c["theo_0"], c["delta_adj"], c["gamma_c"], 160) < c["theo_0"]


def _call_max_drop(spot, strikes=None):
    """Largest step-down (raw LSB) of any call's reprice as S rises one raw step at a time,
    from -jump_limit (R04 blocks larger down moves) through dS saturation and beyond.
    strikes=None recenters on spot (A22); pass g.STRIKES to pin the default-spot grid."""
    snap = g.build_snapshot(S_ref_dollars=spot, strikes=strikes)
    ref = snap["S_ref"]
    worst = 0
    for c in snap["contracts"][:4]:
        prev = None
        for S in range(ref - g.JUMP_LIMIT_CENTS * 16, ref + g.DS_LIMIT + 2000):
            v = g.reprice(c["theo_0"], c["delta_adj"], c["gamma_c"], g.ds_raw(S, ref))
            if prev is not None:
                worst = max(worst, prev - v)
            prev = v
    return worst


@pytest.mark.parametrize("spot", [700.0, 720.0, g.SPOT, 735.49, 760.0, 800.0])
def test_reprice_monotonic_calls(spot):
    """Recentered grid (A22): strikes always bracket spot, so every call is strictly monotone."""
    assert _call_max_drop(spot) == 0


@pytest.mark.parametrize("spot", [718.0, 722.0, 726.0, 730.0, g.SPOT])
def test_reprice_monotonic_calls_default_grid(spot):
    """Default-spot grid 732-735 pinned, S_ref from 718 up: strictly monotone."""
    assert _call_max_drop(spot, g.STRIKES) == 0


@pytest.mark.xfail(strict=True, reason="KNOWN LIMITATION L1 (docs/KNOWN_LIMITATIONS.md): with the 732-735 grid "
                   "pinned and S_ref = 714 (about $18 below the strikes), OTM calls with delta_adj <~ 0.12 dip by "
                   "1 LSB between about -364 and -342 cents: slope there is below 1 LSB per dS step, so p1/p2 "
                   "rounding can step back. Regenerated for the SPY calibration (was spot 140 on the legacy grid).")
def test_reprice_monotonic_otm_calls_near_jump_limit():
    assert _call_max_drop(714.0, g.STRIKES) == 0


@pytest.mark.parametrize("spot", [700.0, 705.0, 710.0, 714.0, 716.0])
def test_reprice_otm_call_dip_is_at_most_one_lsb(spot):
    assert _call_max_drop(spot, g.STRIKES) <= 1


def test_jump_limit_is_tighter_error_curve_proposal():
    """JUMP_LIMIT_CENTS is the recorded output of error_curve.adopted_jump_limit() (370 c at the
    SPY calibration, TODO(FINAL)); the gate raw value is cents x 16 and gate.sv must carry it."""
    import error_curve
    assert g.JUMP_LIMIT_CENTS == error_curve.adopted_jump_limit() == 370
    assert g.JUMP_LIMIT_RAW == g.JUMP_LIMIT_CENTS * 16
    sv = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fpga", "gate", "gate.sv")).read()
    assert f"JUMP_LIMIT_RAW = {g.JUMP_LIMIT_CENTS} * 16" in sv
    assert f"44'sd{g.NET_VEGA_LIMIT * 256:_}" in sv                    # R11 mirror


# ---------------------------------------------------------------- format limits at this calibration
def test_format_limits_at_calibration():
    """Step-3 assertions: the SPY universe fits the existing fixed-point formats (no format change)."""
    lim = g.check_format_limits()
    assert lim["S_max_dollars"] == pytest.approx(1310.71, abs=0.01)
    assert lim["theo_cap_dollars"] == pytest.approx(10.24, abs=0.01)
    assert lim["ds_cap_cents"] == pytest.approx(1023.9, abs=0.05)
    assert lim["max_theo_dollars"] == pytest.approx(6.77, abs=0.05)      # deepest ITM 5-day contract
    assert 2000 < lim["max_gamma_c"] < 2600                             # gamma code about 2,300
    assert lim["max_theo_dollars"] < lim["theo_cap_dollars"]
    assert g.SPOT * 100 + g.JUMP_LIMIT_CENTS < lim["S_max_dollars"] * 100
    assert g.JUMP_LIMIT_CENTS < lim["ds_cap_cents"]


def test_strikes_recenter_on_nearest():
    assert g.STRIKES == (732.0, 733.0, 734.0, 735.0)
    assert g.build_snapshot()["strikes"] == g.STRIKES
    assert g.strikes_for(733.49) == (731.0, 732.0, 733.0, 734.0)
    assert g.strikes_for(740.2) == (738.0, 739.0, 740.0, 741.0)
    snap = g.build_snapshot(S_ref_dollars=700.0)
    assert [c["K"] for c in snap["contracts"]] == [698.0, 699.0, 700.0, 701.0] * 2


# ---------------------------------------------------------------- snapshot
def test_snapshot_fits_formats_and_matches_floats():
    snap = g.build_snapshot()
    assert snap["S_ref"] == 1173600                                  # 733.50 * 100 * 16 (TODO(FINAL) spot)
    assert len(snap["contracts"]) == 8
    for i, c in enumerate(snap["contracts"]):
        assert c["is_put"] == (i >= 4) and c["K"] == g.STRIKES[i % 4]
        for key, fmt in (("theo_0", g.FMT_THEO), ("delta_adj", g.FMT_DELTA_ADJ),
                         ("gamma_c", g.FMT_GAMMA_C), ("vega", g.FMT_VEGA)):
            assert fmt.lo < c[key] < fmt.hi, (i, key)                # no saturation
        assert abs(g.from_fixed(c["theo_0"], g.FMT_THEO) - c["price"] * 100) <= 1 / 512
        assert abs(g.from_fixed(c["delta_adj"], g.FMT_DELTA_ADJ) - c["delta_adj_f"]) <= 2 ** -17
        assert abs(g.from_fixed(c["gamma_c"], g.FMT_GAMMA_C) - c["gamma"] / 100) <= 2 ** -24
        assert abs(g.from_fixed(c["vega"], g.FMT_VEGA) - c["vega_f"] * 100) <= 1 / 512
    for k in range(4):
        call, put = snap["contracts"][k], snap["contracts"][k + 4]
        assert call["gamma_c"] == put["gamma_c"] and call["vega"] == put["vega"]
        assert call["delta_adj"] > 0


# ---------------------------------------------------------------- edges
def test_edges_and_fire():
    assert g.edge_buy(1000 << 8, 998) == 2 << 8
    assert g.edge_sell(1000 << 8, 1002) == 2 << 8
    theo = (100 << 8) + 385                                          # ask 100 -> edge 385
    assert g.fire_buy(theo, 99, 100, 5, 5, False)
    assert not g.fire_buy(theo - 1, 99, 100, 5, 5, False)            # edge 384 is not > 384
    assert not g.fire_buy(theo, 99, 100, 5, 5, True)                 # locked out
    assert not g.fire_buy(theo, 99, 100, 5, 0, False)                # no ask size
    assert not g.fire_buy(theo, 0, 100, 5, 5, False)                 # bid 0 -> insane
    assert not g.fire_buy(theo + (30 << 8), 70, 100, 5, 5, False)    # spread 30 > 20
    theo_s = (100 << 8) - 385
    assert g.fire_sell(theo_s, 100, 101, 5, 5, False)
    assert not g.fire_sell(theo_s, 100, 100, 5, 5, False)            # locked market


# ---------------------------------------------------------------- microprice
def test_recip_table():
    assert len(g.RECIP) == 8192 and g.RECIP[0] == 0
    assert g.RECIP[1] == (1 << 20) - 1                               # saturated (A10b)
    assert g.RECIP[8191] == 128 and max(g.RECIP) < (1 << 20)
    for t in range(2, 8192):
        assert g.RECIP[t] == round((1 << 20) / t)


MP_TARGET_CENTS = 0.06


def _mp_np(spread, bsz, asz):
    """Vectorized copy of golden microprice_q4 (bid 0), cross-checked against it below."""
    np = pytest.importorskip("numpy")
    rec = np.array(g.RECIP, dtype=np.int64)
    return (spread * bsz * rec[bsz + asz] + (1 << 15)) >> 16


def _mp_exhaustive_worst(spread):
    """Worst |microprice - exact| in cents over every (bsz, asz) in 0..4095, total >= 1."""
    np = pytest.importorskip("numpy")
    worst = 0.0
    asz = np.arange(4096, dtype=np.int64)
    for bsz in range(4096):
        a = asz if bsz else asz[1:]
        err = np.abs(_mp_np(spread, bsz, a) / 16.0 - spread * bsz / (bsz + a))
        worst = max(worst, float(err.max()))
    return worst


def test_microprice_vectorized_matches_golden():
    np = pytest.importorskip("numpy")
    rng = random.Random(SEED + 5)
    for _ in range(20_000):
        spread, bsz, asz = rng.randint(0, 20), rng.randint(0, 4095), rng.randint(0, 4095)
        if bsz + asz == 0:
            continue
        bid = rng.randint(1, 50000)
        assert (g.microprice_q4(bid, bid + spread, bsz, asz, g.RECIP) - (bid << 4)
                == int(_mp_np(spread, np.int64(bsz), np.int64(asz))))


@pytest.mark.parametrize("spread", [1, 2])
def test_microprice_vs_float(spread):
    """Every total size up to 8,190 (both 12-bit sizes), no halving: within the 0.06-cent target."""
    assert _mp_exhaustive_worst(spread) <= MP_TARGET_CENTS


def test_microprice_wide_spread_bound():
    """Measured finding (A10b): 0.06 holds up to spread 14; spread 20 (R05 max) reaches 0.0699."""
    assert _mp_exhaustive_worst(14) <= MP_TARGET_CENTS
    assert MP_TARGET_CENTS < _mp_exhaustive_worst(20) <= 0.07


def test_microprice_exact_points():
    U = g.SPOT_CENTS
    assert g.microprice_q4(U, U + 1, 1, 1, g.RECIP) == (U << 4) + 8           # midpoint
    assert g.microprice_q4(U, U + 1, 0, 5, g.RECIP) == U << 4                 # all at bid
    assert g.microprice_q4(U, U, 7, 9, g.RECIP) == U << 4                     # zero spread
    # largest total (8,190, no halving), still the midpoint
    assert g.microprice_q4(100, 102, 4095, 4095, g.RECIP) == (100 << 4) + 16
    assert g.microprice_q4(100, 101, 1, 0, g.RECIP) == 101 << 4             # tot 1, saturated recip
    assert g.microprice_q4(100, 120, 1, 0, g.RECIP) == 120 << 4


def test_crc8_check_value():
    assert g.crc8(b"123456789") == 0xF4                              # CRC-8/SMBUS (poly 0x07, init 0)


def test_vectors_regenerate_identically(tmp_path):
    """docs/vectors matches a fresh run (directed-case asserts run inside the generator)."""
    import make_vectors
    make_vectors.main(str(tmp_path))
    committed = os.path.join(make_vectors.ROOT, "docs", "vectors")
    for name in ("parser.csv", "microprice.csv", "reprice.csv", "trigger.csv", "gate.csv", "README.md"):
        fresh = (tmp_path / name).read_bytes()
        assert open(os.path.join(committed, name), "rb").read() == fresh, f"{name} is stale: rerun make_vectors.py"



def test_microprice_old_worst_case_now_within_target():
    # was 0.090 cent with the 16-bit table (sizes halved to 1951/34)
    assert abs(g.microprice_q4(1000, 1002, 3903, 69, g.RECIP) / 16.0
               - g.microprice_float(1000, 1002, 3903, 69)) <= 0.06
