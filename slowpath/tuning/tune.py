"""Strategy-parameter tuning with Optuna (TPE) plus a random-search baseline.

SIMULATED arena: every P&L number comes from golden/engine.py over sim/options_model.py
scenarios, through evaluator.py (the real evaluator; the old stub is archived).

Seed discipline (80/20):
  search objective   in-sample seeds 0-59
  confirmation       in-sample seeds 60-79 (top-K candidates only)
  out-of-sample      seeds 80-99: never requested by this script (hard guard below)

Evaluator contract: see README.md in this folder.

ASSUMPTIONS (flagged; change here if wrong):
  T1. exit_edge_ticks has no CLAUDE.md default; trial 0 uses 0.5 (= model_margin_ticks).
  T2. size_cap_frac default 1.0, so size_cap_max = 5 + round(1.0 * 5) = 10 (CLAUDE.md 5/10).
  T3. round() is Python round (half to even) on frac * (10 - base).
  T4. "25th percentile" = numpy linear interpolation (np.percentile default).
  T5. "mean max drawdown" = mean over every (regime, seed) run; drawdown is a positive number.
  T6. "fewer than 50 fills" = total fills over all seeds of the normal regime.
  T7. "any hard trip" = hard_trips > 0 in any regime and seed. rogue_burst latches R12 and
      feed_fault latches R03 in every run by design (the real engine shows this), so both are in
      EXPECTED_TRIP_REGIMES; a trip anywhere else still fails the trial. TODO(FINAL)
  T8. Top-K = 5 (TODO(FINAL)) distinct completed TPE trials by in-sample objective; the winner is
      the best confirmation score. Defaults are kept small so a run finishes in minutes:
      20 TPE + 10 random trials (TODO(FINAL)).
  T9. The random study does not enqueue the defaults (pure baseline).
  T10. hedge_band_shares is searched over 200-1000 (golden HEDGE_BAND_SHARES_RANGE). It has no
      confirmed default, so trial 0 enqueues DEFAULT_PARAMS without it and Optuna samples it.
  T11. The evaluator is called with --mode search for 0-59 and --mode confirm for 60-79; it
      refuses any seed >= 80 itself as well (evaluator.py EV1).
"""

import argparse
import hashlib
import json
import math
import os
import shlex
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "tuning.db")
STORAGE = "sqlite:///" + DB_PATH.replace("\\", "/")

# regime names copied from sim/options_model.py REGIMES (not imported: no coupling to sim)
REGIMES = ("quiet", "normal", "single_jump", "flash_crash", "quote_flicker", "feed_fault", "rogue_burst")
EXPECTED_TRIP_REGIMES = ("feed_fault", "rogue_burst")   # T7  TODO(FINAL)

SEARCH_SEEDS = (0, 59)
CONFIRM_SEEDS = (60, 79)
OOS_FIRST_SEED = 80                 # seeds >= this are out-of-sample: forbidden here

MIN_NORMAL_FILLS = 50
DRAWDOWN_WEIGHT = 0.5
FAIL_SCORE = -1e9
TOP_K = 5                           # T8  TODO(FINAL)
N_TPE_TRIALS = 20                   # T8  TODO(FINAL): small so a run finishes in minutes
N_RANDOM_TRIALS = 10                # T8  TODO(FINAL)
PROVENANCE = "real evaluator, seeds 0-59 search, 60-79 confirm"

DEFAULT_PARAMS = {                  # trial 0 (CLAUDE.md defaults; T1, T2)
    "edge_threshold_ticks": 1.5,
    "min_move_cents": 2,
    "max_orders_per_event": 2,
    "size_cap_base": 5,
    "size_cap_frac": 1.0,
    "time_stop_us": 5000,
    "exit_edge_ticks": 0.5,
}
SCHEMA = "latency-arena-eval/1"


class SeedGuardError(ValueError):
    pass


# ---------------------------------------------------------------- search space
def size_cap_max(base, frac):
    return base + round(frac * (10 - base))                          # T3


def suggest_params(trial):
    """Search space exactly as specified. Works with an Optuna trial or any object
    with suggest_float / suggest_int."""
    return {
        "edge_threshold_ticks": trial.suggest_float("edge_threshold_ticks", 1.0, 3.0),
        "min_move_cents": trial.suggest_int("min_move_cents", 1, 5),
        "max_orders_per_event": trial.suggest_int("max_orders_per_event", 1, 4),
        "size_cap_base": trial.suggest_int("size_cap_base", 2, 10),
        "size_cap_frac": trial.suggest_float("size_cap_frac", 0.0, 1.0),
        "time_stop_us": trial.suggest_int("time_stop_us", 1000, 20000, log=True),
        "exit_edge_ticks": trial.suggest_float("exit_edge_ticks", 0.25, 1.5),
        "hedge_band_shares": trial.suggest_int("hedge_band_shares", 200, 1000),     # T10
    }


def strategy_params(p):
    """Search parameters -> what the evaluator receives (size_cap_frac becomes size_cap_max)."""
    out = {k: v for k, v in p.items() if k != "size_cap_frac"}
    out["size_cap_max"] = size_cap_max(p["size_cap_base"], p["size_cap_frac"])
    return out


# ---------------------------------------------------------------- seeds
def seed_range(lo, hi):
    """'lo-hi' string for the evaluator; refuses any out-of-sample seed."""
    if not (0 <= lo <= hi < OOS_FIRST_SEED):
        raise SeedGuardError(f"seed range {lo}-{hi} touches out-of-sample seeds (>= {OOS_FIRST_SEED}) or is invalid")
    return f"{lo}-{hi}"


def parse_seed_range(s):
    lo, hi = (int(x) for x in s.split("-"))
    return list(range(lo, hi + 1))


# ---------------------------------------------------------------- evaluator
def real_command():
    return [sys.executable, os.path.join(HERE, "evaluator.py")]


def resolve_command(evaluator=None):
    if evaluator:
        return shlex.split(evaluator, posix=(os.name != "nt"))
    if os.environ.get("LATENCY_EVALUATOR"):
        return shlex.split(os.environ["LATENCY_EVALUATOR"], posix=(os.name != "nt"))
    return real_command()


def mode_for(lo, hi):
    """T11: search for 0-59, confirm for 60-79; anything else is refused."""
    seed_range(lo, hi)
    if SEARCH_SEEDS[0] <= lo and hi <= SEARCH_SEEDS[1]:
        return "search"
    if CONFIRM_SEEDS[0] <= lo and hi <= CONFIRM_SEEDS[1]:
        return "confirm"
    raise SeedGuardError(f"seed range {lo}-{hi} is neither inside search {SEARCH_SEEDS} nor confirm {CONFIRM_SEEDS}")


def validate_result(doc, seeds, regimes):
    """Check the evaluator JSON against the contract; returns {regime: [run dicts]}."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"evaluator output must be an object with schema={SCHEMA!r}")
    if doc.get("simulated") is not True:
        raise ValueError("evaluator output must carry \"simulated\": true")
    res = doc.get("results")
    if not isinstance(res, dict) or set(res) != set(regimes):
        raise ValueError(f"results must have exactly the regimes {sorted(regimes)}")
    for reg, runs in res.items():
        got = sorted(r.get("seed") for r in runs)
        if got != sorted(seeds):
            raise ValueError(f"{reg}: seeds {got[:5]}... do not match the request")
        for r in runs:
            for k, typ in (("net_pnl", (int, float)), ("max_drawdown", (int, float)),
                           ("fills", int), ("hard_trips", int)):
                if not isinstance(r.get(k), typ) or isinstance(r.get(k), bool):
                    raise ValueError(f"{reg} seed {r.get('seed')}: {k} missing or wrong type")
            if not math.isfinite(r["net_pnl"]) or r["max_drawdown"] < 0 or r["fills"] < 0 or r["hard_trips"] < 0:
                raise ValueError(f"{reg} seed {r['seed']}: out-of-range value")
    return res


class Evaluator:
    """Runs the external command; caches by (params, seeds, regimes)."""

    def __init__(self, command, regimes=REGIMES, timeout_s=3600):
        self.command, self.regimes, self.timeout_s = list(command), tuple(regimes), timeout_s
        self.cache, self.calls = {}, 0

    def __call__(self, sparams, lo, hi):
        rng = seed_range(lo, hi)                                     # guard first
        mode = mode_for(lo, hi)
        pjson = json.dumps(sparams, sort_keys=True)
        key = hashlib.sha256(f"{pjson}|{rng}|{self.regimes}".encode()).hexdigest()
        if key not in self.cache:
            cmd = self.command + ["--mode", mode, "--params", pjson, "--seeds", rng, "--regimes", ",".join(self.regimes)]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s, check=True)
            self.calls += 1
            self.cache[key] = validate_result(json.loads(out.stdout), parse_seed_range(rng), self.regimes)
        return self.cache[key]


# ---------------------------------------------------------------- objective
def score(results, expected_trip_regimes=EXPECTED_TRIP_REGIMES):
    """mean_regimes(p25(net_pnl)) - 0.5 * mean(max_drawdown); FAIL_SCORE on the hard rules."""
    if "normal" in results and sum(r["fills"] for r in results["normal"]) < MIN_NORMAL_FILLS:   # T6
        return FAIL_SCORE
    for reg, runs in results.items():                                         # T7
        if reg not in expected_trip_regimes and any(r["hard_trips"] > 0 for r in runs):
            return FAIL_SCORE
    p25 = [float(np.percentile([r["net_pnl"] for r in runs], 25)) for runs in results.values()]   # T4
    dd = [r["max_drawdown"] for runs in results.values() for r in runs]                          # T5
    return float(np.mean(p25) - DRAWDOWN_WEIGHT * np.mean(dd))


def make_objective(evaluate):
    def objective(trial):
        p = suggest_params(trial)
        sp = strategy_params(p)
        trial.set_user_attr("strategy_params", sp)
        return score(evaluate(sp, *SEARCH_SEEDS))
    return objective


# ---------------------------------------------------------------- studies
def _optuna():
    try:
        import optuna
    except ImportError as e:
        raise SystemExit("optuna is not installed: python -m pip install optuna") from e
    return optuna


def run_study(name, sampler, n_trials, evaluate, enqueue_defaults, storage=STORAGE, fresh=False):
    optuna = _optuna()
    if fresh:
        try:
            optuna.delete_study(study_name=name, storage=storage)
        except KeyError:
            pass
    study = optuna.create_study(study_name=name, storage=storage, sampler=sampler,
                                direction="maximize", load_if_exists=True)
    if enqueue_defaults and not study.trials:            # fresh study only; never re-queued on resume
        study.enqueue_trial(DEFAULT_PARAMS)
    remaining = n_trials - len([t for t in study.trials if t.state.is_finished()])
    if remaining > 0:
        study.optimize(make_objective(evaluate), n_trials=remaining)
    return study


def top_k(study, k=TOP_K):
    seen, out = set(), []
    done = [t for t in study.trials if t.value is not None and t.state.name == "COMPLETE"]
    for t in sorted(done, key=lambda t: (-t.value, t.number)):
        key = json.dumps(t.user_attrs["strategy_params"], sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(t)
        if len(out) == k:
            break
    return out


def confirm(trials, evaluate):
    rows = []
    for t in trials:
        sp = t.user_attrs["strategy_params"]
        rows.append({"trial": t.number, "search": t.value, "confirm": score(evaluate(sp, *CONFIRM_SEEDS)),
                     "params": sp})
    return sorted(rows, key=lambda r: (-r["confirm"], r["trial"]))


def summarize(study):
    vals = [t.value for t in study.trials if t.value is not None]
    ok = [v for v in vals if v > FAIL_SCORE]
    best_at = next((i for i, v in enumerate(np.maximum.accumulate(vals)) if v == max(vals)), None) if vals else None
    return {"trials": len(vals), "valid": len(ok), "best": max(vals) if vals else None,
            "median_valid": float(np.median(ok)) if ok else None, "best_found_at_trial": best_at}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--evaluator", help="evaluator command line (default: python evaluator.py in this folder)")
    ap.add_argument("--regimes", default=",".join(REGIMES), help="subset for quick checks (the full run uses all 7)")
    ap.add_argument("--n-trials", type=int, default=N_TPE_TRIALS)
    ap.add_argument("--n-random", type=int, default=N_RANDOM_TRIALS)
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--storage", default=STORAGE)
    ap.add_argument("--study-prefix", default="arena")
    ap.add_argument("--fresh", action="store_true", help="delete existing studies with these names first")
    ap.add_argument("--out", default=os.path.join(HERE, "tuning_result.json"))
    a = ap.parse_args(argv)

    optuna = _optuna()
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    regimes = tuple(a.regimes.split(","))
    evaluate = Evaluator(resolve_command(a.evaluator), regimes)
    tag = "real"
    tpe = run_study(f"{a.study_prefix}-{tag}-tpe", optuna.samplers.TPESampler(multivariate=True, seed=0),
                    a.n_trials, evaluate, True, a.storage, a.fresh)
    rnd = run_study(f"{a.study_prefix}-{tag}-random", optuna.samplers.RandomSampler(seed=0),
                    a.n_random, evaluate, False, a.storage, a.fresh)
    confirmed = confirm(top_k(tpe, a.top_k), evaluate)
    default_trial = tpe.trials[0]

    report = {"simulated": True, "provenance": PROVENANCE,
              "label": "SIMULATED arena: simulated market data and fills; not real trading results",
              "regimes": list(regimes), "top_k": a.top_k,
              "evaluator": " ".join(evaluate.command),
              "search_seeds": "%d-%d" % SEARCH_SEEDS, "confirm_seeds": "%d-%d" % CONFIRM_SEEDS,
              "out_of_sample": "never evaluated by this script",
              "tpe": summarize(tpe), "random": summarize(rnd),
              "default_trial0": {"search": default_trial.value, "params": default_trial.user_attrs["strategy_params"]},
              "confirmed_top_k": confirmed, "recommended": confirmed[0] if confirmed else None}
    with open(a.out, "w", newline="\n") as f:
        json.dump(report, f, indent=2, sort_keys=True)

    print(f"evaluator: {report['evaluator']}   evaluator calls: {evaluate.calls}")
    print(f"{'study':8s} {'trials':>6s} {'valid':>6s} {'best':>12s} {'median ok':>12s} {'best@':>6s}")
    for nm in ("tpe", "random"):
        s = report[nm]
        fmt = lambda v: f"{v:12.2f}" if v is not None else f"{'-':>12s}"
        print(f"{nm:8s} {s['trials']:6d} {s['valid']:6d} {fmt(s['best'])} {fmt(s['median_valid'])} {str(s['best_found_at_trial']):>6s}")
    print(f"defaults (trial 0) search score: {default_trial.value:.2f}")
    print(f"top-{a.top_k} confirmed on seeds {report['confirm_seeds']}:")
    for r in confirmed:
        print(f"  trial {r['trial']:4d}  search {r['search']:10.2f}  confirm {r['confirm']:10.2f}  {r['params']}")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
