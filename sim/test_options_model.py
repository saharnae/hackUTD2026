"""Tests for the SIMULATED options feed model. Fixed seeds throughout."""

import json
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import options_model as om  # noqa: E402

g = om.g


@pytest.fixture(scope="module")
def long_run():
    return om.OptionsMarketScenario(0, "normal", {"duration_us": 100_000_000})


def test_mean_tick_gap(long_run):
    mean = np.diff(long_run.tick_times).mean()
    assert abs(mean / om.CONFIG["tick_gap_mean_us"] - 1) < 0.02, mean


def test_jump_frequency(long_run):
    n_random = sum(not j["scheduled"] for j in long_run.jumps)
    freq = n_random / long_run.tick_times.size
    assert abs(freq / om.CONFIG["jump_prob"] - 1) < 0.10, freq


def test_stale_window_median(long_run):
    med = np.median([r["W_us"] for r in long_run.refreshes])
    assert abs(med / om.CONFIG["stale_median_us"] - 1) < 0.05, med


def test_refresh_stagger_and_fake_jumps(long_run):
    cfg = om.CONFIG
    for r in long_run.refreshes[:500]:
        for t in r["refresh_t_us"]:
            frac = (t - r["trigger_t_us"]) / r["W_us"]
            assert 1 - cfg["refresh_stagger"] - 1e-9 <= frac <= 1 + 1e-9
    for j in long_run.jumps:
        if j["fake"] and j["revert_t_us"] is not None:
            assert 0 <= j["revert_t_us"] - j["t_us"] <= cfg["fake_revert_max_us"]


@pytest.mark.parametrize("regime", ["normal", "quote_flicker", "feed_fault", "flash_crash"])
def test_byte_identical_for_same_seed(tmp_path, regime):
    a = om.write_scenario(7, regime, str(tmp_path / "a"))
    b = om.write_scenario(7, regime, str(tmp_path / "b"))
    for pa, pb in zip(a, b):
        assert open(pa, "rb").read() == open(pb, "rb").read()
    c = om.write_scenario(8, regime, str(tmp_path / "c"))
    assert open(a[0], "rb").read() != open(c[0], "rb").read()


@pytest.mark.parametrize("regime", om.REGIMES)
def test_no_crossed_quotes_except_injected(regime):
    sc = om.OptionsMarketScenario(5, regime)
    injected = {f["index"] for f in sc.faults}
    for i, w in enumerate(sc.words()):
        f = g.parse_word(w)
        bad = not (f["bid"] > 0 and f["ask"] > f["bid"])
        assert bad == (i in injected), (i, f)
    p = om.CONFIG["flicker_fault_prob"] if regime == "quote_flicker" else om.CONFIG["fault_prob"]
    rate = len(injected) / len(sc.messages)
    assert abs(rate - p) < 4 * np.sqrt(p * (1 - p) / len(sc.messages)) + 1e-3


def test_pack_unpack_round_trip():
    rng = np.random.default_rng(11)
    for _ in range(2000):
        vals = {name: int(rng.integers(0, 1 << width)) for name, _, width in g.WORD_FIELDS if name != "crc8"}
        w = g.build_word(**vals)
        f = g.parse_word(w)
        assert f["crc_ok"] == 1 and all(f[k] == v for k, v in vals.items())
        # bit positions straight from the CLAUDE.md table
        assert (w >> 120) & 0xFF == vals["type"] and (w >> 104) & 0xFFFF == vals["seq"]
        assert (w >> 72) & 0xFFFFFFFF == vals["ts_us"] and (w >> 68) & 0xF == vals["id"]
        assert (w >> 50) & 0x3FFFF == vals["bid"] and (w >> 32) & 0x3FFFF == vals["ask"]
        assert (w >> 20) & 0xFFF == vals["bid_size"] and (w >> 8) & 0xFFF == vals["ask_size"]
        assert w & 0xFF == g.crc8(w.to_bytes(16, "big")[:15])


def test_written_files_match_model(tmp_path):
    sc = om.OptionsMarketScenario(2, "feed_fault")
    p_bin, p_hex, p_json = om.write_scenario(2, "feed_fault", str(tmp_path))
    data = open(p_bin, "rb").read()
    assert len(data) == 16 * len(sc.messages)
    words = [int.from_bytes(data[i:i + 16], "big") for i in range(0, len(data), 16)]
    for w, m in zip(words, sc.messages):
        f = g.parse_word(w)
        assert f["crc_ok"] == 1
        assert all(f[k] == m[k] for k in ("type", "seq", "ts_us", "id", "bid", "ask", "bid_size", "ask_size"))
    lines = open(p_hex).read().split("\n")[:-1]
    side = json.load(open(p_json))
    assert len(lines) == 4096 and all(len(x) == 32 for x in lines)
    assert [int(x, 16) for x in lines[:side["hex_valid_words"]]] == words[:side["hex_valid_words"]]
    assert all(int(x, 16) == 0 for x in lines[side["hex_valid_words"]:])
    assert side["label"].startswith("SIMULATED") and side["split"] == "in_sample"


def test_injected_gaps_and_dups_seen_by_golden_tracker():
    sc = om.OptionsMarketScenario(4, "feed_fault")
    t = g.SeqTracker()
    flags = [t.step(w) for w in sc.words()]
    assert sum(f["seq_gap"] for f in flags) == om.CONFIG["n_gaps"]
    assert sum(f["seq_dup"] for f in flags) == om.CONFIG["n_dups"]
    clean = om.OptionsMarketScenario(4, "normal")
    t2 = g.SeqTracker()
    assert not any(f["seq_gap"] or f["seq_dup"] for f in (t2.step(w) for w in clean.words()))


def test_regime_specifics():
    assert om.OptionsMarketScenario(1, "quiet").jumps == []
    sj = om.OptionsMarketScenario(1, "single_jump").jumps
    assert len(sj) == 1 and sj[0]["t_us"] == om.CONFIG["single_jump_t_us"]
    fc = om.OptionsMarketScenario(1, "flash_crash")
    ts = [j["t_us"] for j in fc.jumps]
    assert len(ts) == 5 and ts[-1] - ts[0] <= om.CONFIG["flash_span_us"]
    drop = fc.und[-1]["S_true"] - fc.S_ref
    assert drop < -7.0                                      # 5 x -$1.50 plus diffusion
    rb = om.OptionsMarketScenario(1, "rogue_burst").sidecar()["rogue_burst"]
    assert rb["count"] == 50 and len(rb["frames"]) == 50


def test_mm_ignores_fake_jumps_and_rivals_are_sidecar_only():
    sc = om.OptionsMarketScenario(0, "normal", {"duration_us": 20_000_000})
    by_t = {u["t"]: u for u in sc.und}
    last = sc.S_ref
    for r in sc.refreshes:
        # every trigger sits on an underlying event and is justified by the MM's fake-free spot
        u = by_t[r["trigger_t_us"]]
        assert r["mm_spot"] == u["mm_spot"]
        assert abs(u["mm_spot"] - last) * 100 >= om.CONFIG["mm_move_cents"] - 1e-9
        last = u["mm_spot"]
    fakes = [j for j in sc.jumps if j["fake"]]
    assert fakes and all(abs(by_t[j["t_us"]]["mm_spot"] - by_t[j["t_us"]]["S_true"]) > 0 for j in fakes)
    for rh in sc.rival_hits:
        assert rh["t_arrive_us"] > rh["t_jump_us"]
        edges = [h["edge_ticks"] for h in rh["hits"]]
        assert edges == sorted(edges, reverse=True) and all(e > 0 for e in edges)
        assert len(rh["hits"]) <= om.CONFIG["rival_max_hits"]


def test_seed_split_guard(tmp_path):
    assert om.split_of(0) == om.split_of(79) == "in_sample"
    assert om.split_of(80) == om.split_of(99) == "out_of_sample"
    with pytest.raises(ValueError):
        om.OptionsMarketScenario(100, "normal")
    p = om.write_scenario(85, "quiet", str(tmp_path))[0]
    assert os.sep + "out_of_sample" + os.sep in p


def test_scenario_interface_is_pure():
    sc = om.OptionsMarketScenario(3, "normal")
    s0 = sc.initial_state()
    s1, m1 = sc.step(s0)
    s1b, m1b = sc.step(s0)
    assert s0["index"] == 0 and m1 == m1b and s1 == s1b and m1["word"] == sc.words()[0]
    assert sc.cost([{"qty": 4}]) == 2.0 and sc.limits()["max_position"] == 20


# ---------------------------------------------------------------- SPY re-parameterization (P0b)
SNAP_US = 100_000          # snapshot interval from README.md.txt (S_ref refresh), used for R04 windows


def _stream(sc):
    return [(m["t"], m["type"], m["id"], m["seq"], m["bid"], m["ask"], m["bid_size"], m["ask_size"], m["fault"])
            for m in sc.messages]


@pytest.mark.parametrize("regime", om.REGIMES)
def test_determinism_per_seed(regime):
    a, b = om.OptionsMarketScenario(3, regime), om.OptionsMarketScenario(3, regime)
    assert _stream(a) == _stream(b) and a.words() == b.words()
    assert json.dumps(a.sidecar(), sort_keys=True) == json.dumps(b.sidecar(), sort_keys=True)
    assert _stream(om.OptionsMarketScenario(4, regime)) != _stream(a)


def test_universe_comes_from_golden():
    c = om.CONFIG
    assert c["spot"] == g.SPOT and c["mm_half_spread_ticks"] == g.MM_HALF_SPREAD_TICKS == 1
    assert (c["mm_size_min"], c["mm_size_max"]) == (g.QUOTE_SIZE_MIN, g.QUOTE_SIZE_MAX) == (20, 200)
    assert c["single_jump_cents"] == g.STRESS_SINGLE_JUMP_CENTS == 40
    assert (c["flash_n"], c["flash_cents"]) == (g.STRESS_FLASH_STEPS, g.STRESS_FLASH_STEP_CENTS) == (5, -150)
    assert c["stale_median_us"] == 100.0 and c["fake_frac"] == 0.20 and c["rival_prob"] == 0.3
    sc = om.OptionsMarketScenario(0, "normal")
    assert [k for k, _ in sc._contracts[:4]] == list(g.STRIKES)
    for q in sc.opt:
        assert q["ask"] - q["bid"] <= 2 and 20 <= q["bsz"] <= 200 and 20 <= q["asz"] <= 200


def test_step_in_cents():
    assert om.DERIVED["step_cents"] == pytest.approx(0.44, abs=0.005)
    sc = om.OptionsMarketScenario(0, "quiet", {"duration_us": 20_000_000})
    S = np.array([u["S_true"] for u in sc.und])
    assert np.std(np.diff(S)) * 100 == pytest.approx(om.DERIVED["step_cents"], rel=0.05)


def test_jump_range(long_run):
    lo, hi = om.CONFIG["jump_bps_min"], om.CONFIG["jump_bps_max"]
    assert (lo, hi) == (1.0, 12.0)
    assert om.DERIVED["jump_cents_min"] == pytest.approx(7.3, abs=0.1)
    assert om.DERIVED["jump_cents_max"] == pytest.approx(88.0, abs=0.1)
    rnd = [j for j in long_run.jumps if not j["scheduled"]]
    mags = np.array([abs(j["bps"]) for j in rnd])
    assert len(rnd) > 1000 and mags.min() >= lo and mags.max() <= hi
    assert mags.min() < lo + 0.1 and mags.max() > hi - 0.1                 # uniform shape over the range
    assert om.bps_to_cents(mags.max()) < g.JUMP_LIMIT_CENTS
    with pytest.raises(AssertionError):                                     # a range that reaches R04 is refused
        om.check_config(dict(om.CONFIG, jump_bps_max=g.JUMP_LIMIT_CENTS / om.bps_to_cents(1.0)))


def _gate_fail_mask_at(S_cents_q4, S_ref_q4):
    import make_vectors as mv                                    # golden/ is on sys.path via options_model
    st, od = mv.gate_baseline()
    st.update(S=S_cents_q4, S_ref=S_ref_q4)
    return g.gate_eval(st, od)["fail_mask"]


def _worst_window_move(sc):
    """Max |bid - bid at the last snapshot boundary| (cents) and the bid where it happens."""
    ref, nxt, worst, at = round(sc.S_ref * 100), SNAP_US, 0, None
    for u in sc.und:
        while u["t"] >= nxt:
            ref, nxt = u["bid"], nxt + SNAP_US
        if abs(u["bid"] - ref) > worst:
            worst, at = abs(u["bid"] - ref), (u["bid"], ref)
    return worst, at


@pytest.mark.parametrize("regime", om.REGIMES)
def test_scheduled_stress_trips_only_r04(regime):
    """In-sample seeds only. Only flash_crash moves S past R04 within a snapshot window, and at that
    point the gate (clean baseline otherwise) fails R04 and nothing else."""
    snap = g.build_snapshot()
    for seed in range(0, 10):
        worst, (bid, ref) = _worst_window_move(om.OptionsMarketScenario(seed, regime))
        mask = _gate_fail_mask_at(bid * 16, ref * 16)
        assert snap["S_ref"] == round(g.SPOT * 100) * 16
        if regime == "flash_crash":
            assert worst > g.JUMP_LIMIT_CENTS and mask == 1 << 3, (seed, worst, mask)
        else:
            assert worst <= g.JUMP_LIMIT_CENTS and mask == 0, (seed, worst, mask)
