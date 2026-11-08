"""Tests for the tuning harness and the real evaluator (SIMULATED arena). Optuna-dependent tests skip
if optuna is missing. No test ever runs seeds >= 80: the final-eval test only checks the refusal path."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evaluator as ev  # noqa: E402
import tune  # noqa: E402

PARAMS = dict(tune.strategy_params(dict(tune.DEFAULT_PARAMS, hedge_band_shares=500)))


class FakeTrial:
    """Stands in for an Optuna trial: returns preset values and records the ranges asked for."""

    def __init__(self, values):
        self.values, self.asked = values, {}

    def suggest_float(self, name, lo, hi, log=False):
        self.asked[name] = ("float", lo, hi, log)
        return self.values[name]

    def suggest_int(self, name, lo, hi, log=False):
        self.asked[name] = ("int", lo, hi, log)
        return self.values[name]


def run(seed, pnl, dd=0.0, fills=10, trips=0):
    return {"seed": seed, "net_pnl": pnl, "max_drawdown": dd, "fills": fills, "hard_trips": trips}


# ---------------------------------------------------------------- search space
def test_search_space_ranges():
    vals = dict(tune.DEFAULT_PARAMS, hedge_band_shares=500)
    t = FakeTrial(vals)
    assert tune.suggest_params(t) == vals
    assert "hedge_band_shares" not in tune.DEFAULT_PARAMS                  # T10: no confirmed default
    assert t.asked == {"hedge_band_shares": ("int", 200, 1000, False),
        "edge_threshold_ticks": ("float", 1.0, 3.0, False), "min_move_cents": ("int", 1, 5, False),
        "max_orders_per_event": ("int", 1, 4, False), "size_cap_base": ("int", 2, 10, False),
        "size_cap_frac": ("float", 0.0, 1.0, False), "time_stop_us": ("int", 1000, 20000, True),
        "exit_edge_ticks": ("float", 0.25, 1.5, False)}


@pytest.mark.parametrize("base,frac,expected", [(5, 1.0, 10), (5, 0.0, 5), (10, 0.7, 10), (2, 0.5, 6),
                                                (2, 1.0, 10), (4, 0.25, 6),
                                                (3, 0.5, 7), (5, 0.5, 7)])   # T3: round(3.5)=4, round(2.5)=2
def test_size_cap_max(base, frac, expected):
    assert tune.size_cap_max(base, frac) == expected
    assert base <= tune.size_cap_max(base, frac) <= 10


def test_strategy_params_replaces_frac():
    sp = tune.strategy_params(tune.DEFAULT_PARAMS)
    assert "size_cap_frac" not in sp and sp["size_cap_max"] == 10 and sp["size_cap_base"] == 5


# ---------------------------------------------------------------- seed guard
def test_seed_guard():
    assert tune.seed_range(0, 59) == "0-59" and tune.seed_range(60, 79) == "60-79"
    for lo, hi in [(0, 80), (80, 99), (79, 80), (-1, 5), (10, 5)]:
        with pytest.raises(tune.SeedGuardError):
            tune.seed_range(lo, hi)


def test_evaluator_refuses_oos_before_running():
    ev = tune.Evaluator(["this-command-does-not-exist"])
    with pytest.raises(tune.SeedGuardError):
        ev(tune.strategy_params(tune.DEFAULT_PARAMS), 60, 99)
    assert ev.calls == 0


def test_configured_seed_sets_are_in_sample():
    assert tune.SEARCH_SEEDS[1] < tune.CONFIRM_SEEDS[0] and tune.CONFIRM_SEEDS[1] < tune.OOS_FIRST_SEED


# ---------------------------------------------------------------- objective
def test_score_math():
    res = {"normal": [run(0, 10.0, 4.0, 30), run(1, 20.0, 4.0, 30), run(2, 30.0, 4.0, 0), run(3, 40.0, 4.0, 0)],
           "quiet": [run(0, -8.0, 2.0), run(1, 0.0, 2.0), run(2, 0.0, 2.0), run(3, 8.0, 2.0)]}
    # P25 normal = 17.5, P25 quiet = -2.0 -> mean 7.75; mean dd = 3.0 -> 7.75 - 1.5
    assert tune.score(res) == pytest.approx(6.25)


def test_score_fails_on_few_normal_fills():
    res = {"normal": [run(0, 100.0, fills=24), run(1, 100.0, fills=25)], "quiet": [run(0, 1.0), run(1, 1.0)]}
    assert tune.score(res) == tune.FAIL_SCORE                       # 49 < 50
    res["normal"][1]["fills"] = 26
    assert tune.score(res) > tune.FAIL_SCORE                        # 50 is enough


def test_score_fails_on_any_hard_trip():
    res = {"normal": [run(0, 100.0, fills=60)], "rogue_burst": [run(0, 1.0, trips=1)]}
    assert tune.EXPECTED_TRIP_REGIMES == ("feed_fault", "rogue_burst")          # T7: designed trips
    assert tune.score(res) > tune.FAIL_SCORE
    assert tune.score(res, expected_trip_regimes=()) == tune.FAIL_SCORE
    res["normal"][0]["hard_trips"] = 1                                          # a trip anywhere else fails
    assert tune.score(res) == tune.FAIL_SCORE


def test_validate_result():
    good = {"schema": tune.SCHEMA, "simulated": True, "results": {"normal": [run(0, 1.0), run(1, 2.0)]}}
    assert tune.validate_result(good, [0, 1], ["normal"])
    bad = [
        {"schema": "other", "simulated": True, "results": good["results"]},
        {"schema": tune.SCHEMA, "results": good["results"]},                                 # not labeled simulated
        {"schema": tune.SCHEMA, "simulated": True, "results": {"normal": [run(0, 1.0)]}},    # missing seed
        {"schema": tune.SCHEMA, "simulated": True, "results": {"quiet": [run(0, 1.0), run(1, 1.0)]}},
        {"schema": tune.SCHEMA, "simulated": True, "results": {"normal": [run(0, 1.0), run(1, 1.0, dd=-1.0)]}},
        {"schema": tune.SCHEMA, "simulated": True, "results": {"normal": [run(0, 1.0), dict(run(1, 1.0), fills=1.5)]}},
        {"schema": tune.SCHEMA, "simulated": True,
         "results": {"normal": [run(0, 1.0), dict(run(1, 1.0), net_pnl=float("nan"))]}},
    ]
    for doc in bad:
        with pytest.raises(ValueError):
            tune.validate_result(doc, [0, 1], ["normal"])


# ---------------------------------------------------------------- OOS guard (seeds 60, 80, 99 in each mode)
@pytest.mark.parametrize("seed,search_ok,confirm_ok", [(60, False, True), (80, False, False), (99, False, False)])
def test_oos_guard_each_mode(seed, search_ok, confirm_ok):
    for mode, ok in (("search", search_ok), ("confirm", confirm_ok)):
        if ok:
            doc = ev.evaluate(mode, PARAMS, f"{seed}-{seed}", ["quiet"], workers=1)
            assert doc["seeds"] == [seed] and doc["split"] == "in_sample_confirm" and doc["simulated"] is True
        else:
            with pytest.raises(ev.GuardError):
                ev.evaluate(mode, PARAMS, f"{seed}-{seed}", ["quiet"], workers=1)
        assert ev.main(["--mode", mode, "--params", json.dumps(PARAMS), "--seeds", f"{seed}-{seed}",
                        "--regimes", "quiet", "--workers", "1"]) == (0 if ok else 2)
    # tune.py side: search/confirm routing, everything >= 80 refused before any call
    if seed == 60:
        assert tune.mode_for(60, 60) == "confirm"
    else:
        with pytest.raises(tune.SeedGuardError):
            tune.mode_for(seed, seed)


def test_guard_rejects_ranges_reaching_oos_and_bad_params():
    for mode, rng in (("search", "0-80"), ("confirm", "60-99"), ("search", "50-60"), ("confirm", "59-79")):
        with pytest.raises(ev.GuardError):
            ev.evaluate(mode, PARAMS, rng, ["quiet"], workers=1)
    with pytest.raises(ev.GuardError):
        ev.evaluate("search", {k: v for k, v in PARAMS.items() if k != "hedge_band_shares"}, "0-0", ["quiet"],
                    workers=1)


def test_final_eval_refuses_when_locked_and_takes_no_seeds(tmp_path):
    lock = tmp_path / "final_eval.lock"
    lock.write_text("already ran")
    with pytest.raises(ev.GuardError):
        ev.final_eval(PARAMS, str(lock), str(tmp_path / "out.json"))
    assert not (tmp_path / "out.json").exists() and lock.read_text() == "already ran"
    assert ev.main(["--final-eval", "--params", json.dumps(PARAMS), "--lock-file", str(lock)]) == 2
    assert ev.main(["--final-eval", "--seeds", "80-99", "--params", json.dumps(PARAMS), "--lock-file", str(lock)]) == 2
    assert ev.main(["--final-eval", "--mode", "search", "--params", json.dumps(PARAMS),
                    "--lock-file", str(lock)]) == 2
    assert not os.path.exists(ev.LOCK_PATH)                  # the real lock is never created by the tests


# ---------------------------------------------------------------- real evaluator
def test_real_evaluator_through_subprocess_is_deterministic():
    e1 = tune.Evaluator(tune.real_command(), regimes=("quiet", "normal"))
    res = e1(PARAMS, 60, 63)
    assert set(res) == {"quiet", "normal"} and all(len(v) == 4 for v in res.values())
    again = tune.Evaluator(tune.real_command(), regimes=("quiet", "normal"))(PARAMS, 60, 63)
    assert json.dumps(res, sort_keys=True) == json.dumps(again, sort_keys=True)
    e1(PARAMS, 60, 63)
    assert e1.calls == 1                                                          # cached
    with pytest.raises(tune.SeedGuardError):
        e1(PARAMS, 70, 80)
    assert e1.calls == 1


def test_evaluator_document_fields():
    doc = ev.evaluate("search", PARAMS, "0-1", ["normal", "rogue_burst"], workers=2)
    assert doc["simulated"] is True and doc["seeds"] == [0, 1] and doc["split"] == "in_sample_search"
    assert doc["params"] == PARAMS and len(doc["params_hash"]) == 64
    m = doc["metrics"]
    for k in ("net_pnl_total", "fills", "hard_trips", "max_drawdown", "triggers", "determinism_hash"):
        assert k in m
    assert ev.evaluate("search", PARAMS, "0-1", ["normal", "rogue_burst"], workers=1)["metrics"] == m


def test_regime_names_match_sim():
    import ast
    src = open(os.path.join(tune.HERE, "..", "..", "sim", "options_model.py")).read()
    found = [ast.literal_eval(n.value) for n in ast.parse(src).body
             if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "REGIMES" for t in n.targets)]
    assert found == [tune.REGIMES]


# ---------------------------------------------------------------- optuna end to end
def test_tiny_study_end_to_end(tmp_path):
    """Real evaluator, 2 regimes, a handful of trials: checks the pipeline, not the numbers."""
    pytest.importorskip("optuna")
    out = tmp_path / "result.json"
    db = str(tmp_path / "t.db").replace(os.sep, "/")
    tune.main(["--regimes", "quiet,normal", "--n-trials", "2", "--n-random", "1", "--top-k", "1", "--fresh",
               "--storage", "sqlite:///" + db, "--out", str(out)])
    rep = json.loads(out.read_text())
    assert rep["simulated"] is True and rep["provenance"] == "real evaluator, seeds 0-59 search, 60-79 confirm"
    assert rep["tpe"]["trials"] == 2 and rep["random"]["trials"] == 1 and len(rep["confirmed_top_k"]) == 1
    p0 = rep["default_trial0"]["params"]
    want = tune.strategy_params(dict(tune.DEFAULT_PARAMS, hedge_band_shares=p0["hedge_band_shares"]))
    assert p0 == want and 200 <= p0["hedge_band_shares"] <= 1000          # defaults + sampled hedge band (T10)


def test_resume_does_not_requeue_defaults(tmp_path):
    """Resume logic only: uses an in-process TEST DOUBLE for the evaluator (no P&L meaning)."""
    optuna = pytest.importorskip("optuna")
    storage = "sqlite:///" + str(tmp_path / "r.db").replace(os.sep, "/")

    def double(sp, lo, hi):                       # TEST DOUBLE: fixed feasible results
        return {"normal": [run(s, 1.0, fills=60) for s in range(lo, hi + 1)]}

    def sampler():
        return optuna.samplers.TPESampler(multivariate=True, seed=0)

    s1 = tune.run_study("resume", sampler(), 3, double, True, storage, fresh=True)
    assert len([t for t in s1.trials if t.state.is_finished()]) == 3
    s2 = tune.run_study("resume", sampler(), 6, double, True, storage)          # load_if_exists=True
    done = [t for t in s2.trials if t.state.is_finished()]
    assert len(done) == 6 and len(s2.trials) == 6                               # nothing left WAITING
    defaults = [t for t in s2.trials if all(t.params.get(k) == v for k, v in tune.DEFAULT_PARAMS.items())]
    assert len(defaults) == 1 and defaults[0].number == 0
    s3 = tune.run_study("resume", sampler(), 6, double, True, storage)          # already complete: no new trials
    assert len(s3.trials) == 6
