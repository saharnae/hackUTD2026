"""Real evaluator (SIMULATED arena): runs golden/engine.py over sim/options_model.py scenarios.

Replaces the archived stub_evaluator.py. Contract: README.md in this folder (schema
latency-arena-eval/1), plus what the README was silent on (EV1-EV6 below).

    python evaluator.py --mode search  --params JSON --seeds 0-59  --regimes a,b,...
    python evaluator.py --mode confirm --params JSON --seeds 60-79 --regimes a,b,...
    python evaluator.py --final-eval (--params JSON | --from-result tuning_result.json)

ASSUMPTIONS (EV):
  EV1. --mode is required for normal runs. search accepts only seeds 0-59, confirm only 60-79.
       Any seed >= 80 is refused in both, before any scenario is built (exit code 2).
  EV2. Seeds 80-99 are reachable only through --final-eval, which takes no --seeds (always
       80-99, all 7 regimes), first creates final_eval.lock exclusively (O_EXCL) holding the
       parameter hash, and refuses if the lock already exists. --lock-file exists only so the
       test suite can point the refusal check at a temp path; the real command uses the default.
  EV3. --params must contain exactly PARAM_KEYS (hedge_band_shares included; it has no default).
  EV4. Runs are independent and pure; they are spread over a process pool and returned in
       (regime, seed) order, so the output is identical for any worker count.
  EV5. metrics.determinism_hash = sha256 over (regime, seed, run determinism_hash) in order;
       each run hash covers that session's full send/fill/miss/latch log.
  EV6. All numbers are SIMULATED. No timing measured here is FPGA latency.
"""

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "golden"))
sys.path.insert(0, os.path.join(ROOT, "sim"))

SCHEMA = "latency-arena-eval/1"
REGIMES = ("quiet", "normal", "single_jump", "flash_crash", "quote_flicker", "feed_fault", "rogue_burst")
MODES = {"search": (0, 59), "confirm": (60, 79)}
FINAL_SEEDS = (80, 99)
OOS_FIRST_SEED = 80
LOCK_PATH = os.path.join(HERE, "final_eval.lock")
FINAL_OUT = os.path.join(HERE, "final_eval_result.json")
PARAM_KEYS = ("edge_threshold_ticks", "min_move_cents", "max_orders_per_event", "size_cap_base",
              "size_cap_max", "time_stop_us", "exit_edge_ticks", "hedge_band_shares")
SPLIT_LABEL = {"search": "in_sample_search", "confirm": "in_sample_confirm", "final": "out_of_sample_final"}


class GuardError(ValueError):
    pass


def parse_seeds(s):
    try:
        lo, hi = (int(x) for x in s.split("-"))
    except ValueError as e:
        raise GuardError(f"--seeds must be LO-HI, got {s!r}") from e
    if lo > hi:
        raise GuardError(f"empty seed range {s}")
    return lo, hi


def check_seeds(mode, lo, hi):
    """EV1: refuse before anything is built."""
    if hi >= OOS_FIRST_SEED or lo < 0:
        raise GuardError(f"seeds {lo}-{hi}: seeds >= {OOS_FIRST_SEED} are out-of-sample; use --final-eval once")
    a, b = MODES[mode]
    if not (a <= lo and hi <= b):
        raise GuardError(f"mode {mode} accepts only seeds {a}-{b}, got {lo}-{hi}")
    return list(range(lo, hi + 1))


def check_params(p):
    if not isinstance(p, dict) or set(p) != set(PARAM_KEYS):
        raise GuardError(f"params must have exactly {sorted(PARAM_KEYS)}; got {sorted(p) if isinstance(p, dict) else p}")
    return p


def params_hash(p):
    return hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _one(args):
    seed, regime, params = args
    import engine as E
    import options_model as om
    r = E.run_session(om.OptionsMarketScenario(seed, regime), params)
    r["seed"] = seed
    return regime, seed, r


def run(params, seeds, regimes, workers=None):
    jobs = [(s, r, params) for r in regimes for s in seeds]
    workers = workers or min(os.cpu_count() or 1, 12)
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            out = list(ex.map(_one, jobs, chunksize=max(1, len(jobs) // (4 * workers))))
    else:
        out = [_one(j) for j in jobs]
    results = {r: [] for r in regimes}
    for regime, _, res in out:
        results[regime].append(res)
    return results


def document(mode, params, seeds, regimes, results):
    runs = [x for r in regimes for x in results[r]]
    h = hashlib.sha256()
    for r in regimes:
        for x in results[r]:
            h.update(f"{r}|{x['seed']}|{x['determinism_hash']};".encode())
    return {
        "schema": SCHEMA,
        "simulated": True,
        "label": "SIMULATED arena: simulated market data, simulated fills; not real trading results",
        "mode": mode, "split": SPLIT_LABEL[mode], "seeds": seeds, "regimes": list(regimes),
        "params": params, "params_hash": params_hash(params),
        "results": results,
        "metrics": {
            "net_pnl_total": round(sum(x["net_pnl"] for x in runs), 6),
            "net_pnl_mean": round(sum(x["net_pnl"] for x in runs) / len(runs), 6),
            "fills": sum(x["fills"] for x in runs),
            "hedge_fills": sum(x["hedge_fills"] for x in runs),
            "hard_trips": sum(x["hard_trips"] for x in runs),
            "trips_by_regime": {r: sum(x["hard_trips"] for x in results[r]) for r in regimes},
            "max_drawdown": max(x["max_drawdown"] for x in runs),
            "mean_max_drawdown": round(sum(x["max_drawdown"] for x in runs) / len(runs), 6),
            "triggers": sum(x["triggers"] for x in runs),
            "orders_sent": sum(x["orders_sent"] for x in runs),
            "determinism_hash": h.hexdigest(),
        },
    }


def evaluate(mode, params, seed_str, regimes, workers=None):
    """Guarded in-sample evaluation (search / confirm). Raises GuardError before any work."""
    if mode not in MODES:
        raise GuardError(f"--mode must be one of {sorted(MODES)}")
    seeds = check_seeds(mode, *parse_seeds(seed_str))
    params = check_params(params)
    bad = [r for r in regimes if r not in REGIMES]
    if bad or not regimes:
        raise GuardError(f"unknown regimes {bad}")
    return document(mode, params, seeds, regimes, run(params, seeds, regimes, workers))


def final_eval(params, lock_path=LOCK_PATH, out_path=FINAL_OUT, workers=None):
    """EV2: one shot over seeds 80-99. The lock is taken BEFORE any scenario is built."""
    params = check_params(params)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise GuardError(f"final evaluation already ran (lock {lock_path} exists); refusing a second run")
    with os.fdopen(fd, "w", newline="\n") as f:
        json.dump({"params_hash": params_hash(params), "params": params,
                   "seeds": "%d-%d" % FINAL_SEEDS, "simulated": True}, f, indent=1, sort_keys=True)
    seeds = list(range(FINAL_SEEDS[0], FINAL_SEEDS[1] + 1))
    doc = document("final", params, seeds, REGIMES, run(params, seeds, REGIMES, workers))
    with open(out_path, "w", newline="\n") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description="SIMULATED arena evaluator (real engine)")
    ap.add_argument("--mode", choices=sorted(MODES))
    ap.add_argument("--params")
    ap.add_argument("--from-result", help="final-eval only: take params from tuning_result.json 'recommended'")
    ap.add_argument("--seeds")
    ap.add_argument("--regimes", default=",".join(REGIMES))
    ap.add_argument("--workers", type=int)
    ap.add_argument("--final-eval", action="store_true")
    ap.add_argument("--lock-file", default=LOCK_PATH, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        if a.final_eval:
            if a.mode or a.seeds:
                raise GuardError("--final-eval takes no --mode/--seeds (always seeds 80-99, all regimes)")
            if a.from_result:
                params = json.load(open(a.from_result))["recommended"]["params"]
            else:
                params = json.loads(a.params or "null")
            doc = final_eval(params, a.lock_file)
        else:
            if not a.mode or not a.seeds or a.params is None:
                raise GuardError("--mode, --seeds and --params are required (or --final-eval)")
            doc = evaluate(a.mode, json.loads(a.params), a.seeds, a.regimes.split(","), a.workers)
    except GuardError as e:
        print(json.dumps({"error": str(e), "simulated": True}), file=sys.stderr)
        return 2
    json.dump(doc, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
