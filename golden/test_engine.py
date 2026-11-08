"""Tests for golden/engine.py (SIMULATED arena). In-sample seeds only (0-79); fixed seeds."""

import ast
import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "sim"))
import engine as E  # noqa: E402
import golden as g  # noqa: E402
import options_model as om  # noqa: E402

P = {"hedge_band_shares": 500}


def scen(seed, regime="normal"):
    assert seed < 80, "tests never touch out-of-sample seeds"
    return om.OptionsMarketScenario(seed, regime)


class CheckedEngine(E.Engine):
    """Asserts the accounting identity and the R09 bound after every message."""

    def _apply(self, m):
        super()._apply(m)
        lhs, rhs = self.accounting_identity()
        assert lhs == rhs, (m["t"], lhs, rhs)
        if self.snap is not None:
            assert abs(self.net_delta_shares()) / 100 <= g.NET_DELTA_LIMIT, m["t"]
        self.checked = getattr(self, "checked", 0) + 1


# ---------------------------------------------------------------- determinism
@pytest.mark.parametrize("regime", om.REGIMES)
def test_determinism(regime):
    a = E.run_session(scen(2, regime), P)
    b = E.run_session(scen(2, regime), P)
    assert a == b
    assert E.run_session(scen(3, regime), P)["determinism_hash"] != a["determinism_hash"] or regime == "feed_fault"


def test_trigger_p_matches_golden_trigger():
    rng = random.Random(7)
    for _ in range(3000):
        mids = [rng.randint(30, 800) for _ in range(8)]
        bid = [m - rng.randint(1, 2) for m in mids]
        ask = [max(b + 1, m + rng.randint(0, 2)) for m, b in zip(mids, bid)]
        theo = [(m << 8) + rng.randint(-6 * 256, 6 * 256) for m in mids]
        bsz = [rng.randint(0, 30) for _ in range(8)]
        asz = [rng.randint(0, 30) for _ in range(8)]
        ok, lock = rng.randint(0, 255), rng.randint(0, 0xFFFF) & rng.randint(0, 0xFFFF)
        pos = [rng.randint(-22, 22) for _ in range(8)]
        want = g.trigger(theo, bid, ask, bsz, asz, ok, lock, pos)["orders"]
        got = E.trigger_p(theo, bid, ask, bsz, asz, ok, lock, pos, g.EDGE_THRESHOLD,
                          g.SIZE_CAP_BASE, g.SIZE_CAP_MAX, g.MAX_ORDERS_PER_EVENT)
        assert got == want


# ---------------------------------------------------------------- accounting
@pytest.mark.parametrize("seed,regime", [(0, "normal"), (1, "normal"), (5, "flash_crash"),
                                         (2, "rogue_burst"), (4, "quote_flicker"), (6, "feed_fault")])
def test_accounting_identity_every_message(seed, regime):
    e = CheckedEngine(P)
    r = e.run(scen(seed, regime))
    assert e.checked == len(e.sc.messages)
    lhs, rhs = e.accounting_identity()
    assert lhs == rhs
    assert r["net_pnl"] == pytest.approx(float(lhs) / 100, abs=1e-6)
    assert r["net_pnl"] == pytest.approx(r["realized_pnl"] + r["unrealized_pnl"], abs=1e-5)
    assert r["simulated"] is True


def test_fees_charged_per_contract():
    e = E.Engine(P)
    r = e.run(scen(0, "normal"))
    n = sum(abs(f["qty"]) for f in e.fills if f["kind"] == "option")
    assert n > 0
    # with every position closed at the end, realized P&L = trade P&L - fees exactly
    gross = -sum(f["qty"] * f["price"] * 100 for f in e.fills if f["kind"] == "option") \
        - sum(f["qty"] * f["price"] for f in e.fills if f["kind"] == "hedge")
    assert r["final_position"] == [0] * 8 and r["final_shares"] == 0
    assert gross - n * 50 == e.cash


# ---------------------------------------------------------------- latch and kill
def _sends_after(e, t):
    return [x for x in e.log if x[0] == "send" and x[1] > t]


def test_latch_persists_and_blocks_orders():
    e = E.Engine(P)
    r = e.run(scen(4, "feed_fault"))
    assert r["trip_reasons"] == [3] and e.latched
    t_latch = e.latch_reasons[0]["t_us"]
    assert not _sends_after(e, t_latch)
    assert r["final_position"] == [0] * 8 and r["final_shares"] == 0
    assert e.latched                                         # still latched at the end of the session


def test_only_local_rearm_clears_the_latch():
    src = open(os.path.join(HERE, "engine.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", getattr(n.func, "id", "")) == "local_rearm"]
    assert calls == []                                       # the engine never calls it
    def latched_values(n):                                   # every value assigned to *.latched
        for t in n.targets:
            if isinstance(t, ast.Attribute) and t.attr == "latched":
                yield n.value
            elif isinstance(t, ast.Tuple) and isinstance(n.value, ast.Tuple):
                for tt, vv in zip(t.elts, n.value.elts):
                    if isinstance(tt, ast.Attribute) and tt.attr == "latched":
                        yield vv
    clears = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and any(not (isinstance(v, ast.Constant) and v.value is True) for v in latched_values(n))]
    fns = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
           for s in clears if f.lineno <= s.lineno <= f.end_lineno}
    assert fns == {"__init__", "local_rearm"}                # reset at construction, operator re-arm only
    e = E.Engine(P)
    e.run(scen(4, "feed_fault"))
    e.local_rearm()
    assert not e.latched and not e.armed


def test_kill_flattens_and_latches():
    base = E.Engine(P)
    base.run(scen(0, "normal"))
    entry = next(f for f in base.fills if f["purpose"] == "entry")
    t_kill = entry["t_us"] + 1.0                             # a position is open right now
    e = E.Engine(P)
    r = e.run(scen(0, "normal"), kill_at_us=t_kill)
    assert r["trip_reasons"] == [15] and e.latched and e.kill
    assert r["final_position"] == [0] * 8 and r["final_shares"] == 0
    assert any(x[0] == "flatten" for x in e.log)
    assert not _sends_after(e, t_kill)
    with pytest.raises(RuntimeError):
        e.local_rearm()                                      # refused while the kill input is up
    assert e.latched


def test_kill_never_increases_exposure():
    e = E.Engine(P)
    e.run(scen(1, "normal"), kill_at_us=0.0)
    assert e.latched and not [x for x in e.log if x[0] in ("send", "fill")]


# ---------------------------------------------------------------- hedging and R09
@pytest.mark.parametrize("band", [200, 1000])
def test_hedging_respects_r09(band):
    total_hedges = 0
    for seed in range(0, 6):
        e = CheckedEngine({"hedge_band_shares": band})
        r = e.run(scen(seed, "normal"))
        assert r["max_abs_net_delta"] <= g.NET_DELTA_LIMIT
        total_hedges += r["hedge_fills"]
    assert total_hedges > 0


def test_hedge_band_has_no_default():
    with pytest.raises(ValueError):
        E.Engine({})
    for bad in (199, 1001):
        with pytest.raises(ValueError):
            E.Engine({"hedge_band_shares": bad})
