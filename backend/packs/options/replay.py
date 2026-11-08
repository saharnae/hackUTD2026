"""Seed-guarded replay through the REAL evaluator (slowpath/tuning/evaluator.py) and golden engine.

Seed rules (hard):
  - any seed >= 80 is refused before any scenario is built (SeedGuardError), everywhere;
  - held-out checks (held_out=True) accept only seeds 60-79;
  - the evaluator's own guard (check_seeds, mode search 0-59 / confirm 60-79) runs as a second layer.

ASSUMPTIONS:
  RP1. Results are cached in-process by (params hash, regime, seed, scenario override). Runs are pure
       (evaluator EV4), so the cache cannot change any number.
  RP2. A scenario override (e.g. a jump size other than golden STRESS_SINGLE_JUMP_CENTS) cannot go
       through evaluator.run (it builds scenarios with the default config), so those runs call
       engine.run_session on OptionsMarketScenario(seed, regime, override) directly, serially.
  RP3. Score = tune.score (the tuning objective) with tune.EXPECTED_TRIP_REGIMES. "unexpected_trips"
       counts hard trips outside those regimes.
"""

import hashlib
import json

import numpy as np

from ... import _paths  # noqa: F401
from ...errors import SeedGuardError
import engine as EN
import evaluator as EV
import options_model as om
import tune as TU

AGENT_SEED_LIMIT = 80
HELD_OUT = (60, 79)
LABEL = "SIMULATED (golden engine replay; twin timing, not FPGA latency; no profit claim)"


def guard_seeds(seeds, held_out=False):
    """Returns a sorted list of ints or raises SeedGuardError. Called before anything is built."""
    try:
        out = sorted({int(s) for s in seeds})
    except (TypeError, ValueError):
        raise SeedGuardError("seeds must be integers", {"seeds": str(seeds)[:200]})
    if not out:
        raise SeedGuardError("no seeds requested")
    bad = [s for s in out if s >= AGENT_SEED_LIMIT or s < 0]
    if bad:
        raise SeedGuardError(f"seeds {bad[:5]} are not available to agents (seeds >= {AGENT_SEED_LIMIT} are the "
                             "out-of-sample split, reachable only by the one-shot final evaluation)",
                             {"refused_seeds": bad[:20]})
    if held_out and not all(HELD_OUT[0] <= s <= HELD_OUT[1] for s in out):
        raise SeedGuardError(f"held-out checks use seeds {HELD_OUT[0]}-{HELD_OUT[1]} only", {"seeds": out})
    for lo, hi, mode in ((0, 59, "search"), (60, 79, "confirm")):              # evaluator EV1 as a second layer
        part = [s for s in out if lo <= s <= hi]
        if part:
            try:
                EV.check_seeds(mode, part[0], part[-1])
            except EV.GuardError as e:
                raise SeedGuardError(str(e))
    return out


def seeds_from_range(lo, hi):
    return list(range(int(lo), int(hi) + 1))


def params_hash(p):
    return EV.params_hash(p)


class Replayer:
    def __init__(self, workers=1):
        self.workers = workers
        self.cache = {}
        self.engine_calls = 0                 # sessions actually run (tests)

    def _key(self, params, regime, seed, override):
        return (params_hash(params), regime, seed, json.dumps(override, sort_keys=True) if override else None)

    def runs(self, params, seeds, regimes, scenario_config=None, held_out=False):
        """-> {regime: [per-seed result dicts]} in seed order. Seed guard first."""
        seeds = guard_seeds(seeds, held_out)
        params = EV.check_params(dict(params))
        bad = [r for r in regimes if r not in EV.REGIMES]
        if bad or not regimes:
            raise ValueError(f"unknown regimes {bad}; choose from {list(EV.REGIMES)}")
        missing = {r: [s for s in seeds if self._key(params, r, s, scenario_config) not in self.cache]
                   for r in regimes}
        if scenario_config:
            for r, ss in missing.items():
                for s in ss:                                                   # RP2
                    res = EN.run_session(om.OptionsMarketScenario(s, r, scenario_config), params)
                    res["seed"] = s
                    self.cache[self._key(params, r, s, scenario_config)] = res
        else:
            # one evaluator call per seed set (one process pool, not one per regime)
            groups = {}
            for r, ss in missing.items():
                if ss:
                    groups.setdefault(tuple(ss), []).append(r)
            for ss, rs in groups.items():
                out = EV.run(params, list(ss), rs, workers=self.workers)
                for r in rs:
                    for res in out[r]:
                        self.cache[self._key(params, r, res["seed"], None)] = res
        self.engine_calls += sum(len(ss) for ss in missing.values())
        return {r: [self.cache[self._key(params, r, s, scenario_config)] for s in seeds] for r in regimes}

    def summary(self, params, seeds, regimes, scenario_config=None, held_out=False):
        results = self.runs(params, seeds, regimes, scenario_config, held_out)
        seeds = guard_seeds(seeds, held_out)
        return summarize(params, seeds, regimes, results, scenario_config)


def summarize(params, seeds, regimes, results, scenario_config=None):
    per = {}
    unexpected = 0
    for r in regimes:
        runs = results[r]
        trips = [x["hard_trips"] for x in runs]
        reasons = {}
        for x in runs:
            for c in x["trip_reasons"]:
                reasons[f"R{c:02d}"] = reasons.get(f"R{c:02d}", 0) + 1
        if r not in TU.EXPECTED_TRIP_REGIMES:
            unexpected += sum(trips)
        pnl = [x["net_pnl"] for x in runs]
        per[r] = {"mean_pnl": round(float(np.mean(pnl)), 6), "p25_pnl": round(float(np.percentile(pnl, 25)), 6),
                  "mean_max_drawdown": round(float(np.mean([x["max_drawdown"] for x in runs])), 6),
                  "fills": sum(x["fills"] for x in runs), "hard_trips": sum(trips), "trip_reasons": reasons,
                  "expected_trip_regime": r in TU.EXPECTED_TRIP_REGIMES}
    h = hashlib.sha256()
    for r in regimes:
        for x in results[r]:
            h.update(f"{r}|{x['seed']}|{x['determinism_hash']};".encode())
    score = float(TU.score(results))
    split = ("held_out_60_79" if all(60 <= s <= 79 for s in seeds) else
             "in_sample_search_0_59" if all(s <= 59 for s in seeds) else "in_sample_mixed_0_79")
    return {"label": LABEL, "simulated": True, "split": split, "seeds": [seeds[0], seeds[-1]], "n_seeds": len(seeds),
            "regimes": list(regimes), "params": params, "params_hash": params_hash(params),
            "scenario_override": scenario_config, "per_regime": per, "unexpected_trips": unexpected,
            "score": score, "score_failed": score <= TU.FAIL_SCORE,
            "score_rule": "tune.score: mean over regimes of P25(net_pnl) - 0.5 * mean(max_drawdown); "
                          "-1e9 on a trip outside feed_fault/rogue_burst or < 50 normal fills",
            "determinism_hash": h.hexdigest()}
