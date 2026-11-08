"""SIMULATED disaster world: synthetic households, zones and crews. Deterministic per seed.

Everything here is synthetic. Every number is a placeholder (TODO(FINAL)) listed in
docs/PLACEHOLDERS.md and in DW1-DW9 below.

Two views:
  registry(seed) -> what agents may see: per household zone, age band, device dependence,
                    mobility, language. Static, known before the storm (e.g. a utility's
                    medical-baseline list and census-style records).
  _truth(seed)   -> HIDDEN: true need tier, silent flag, dark zones, report schedule, on-site
                    durations. Only pack.py (for the metrics and the report stream) reads it.
                    Agents never receive it (tested).

ASSUMPTIONS (DW, all TODO(FINAL)):
  DW1. 5,000 households, 12 zones on a 3 x 4 grid (4-neighbour adjacency), 30 crews, depot in zone 5.
  DW2. Attributes: age band 0 (<65) / 1 (65-79) / 2 (80+) with p = .65/.22/.13; device dependence
       p = .04 + .06*(80+); limited mobility p = .08 + .20*(80+); language 0 / 1 / 2 with p = .75/.18/.07.
  DW3. Hidden need score = .40*zone severity + .25*device + .15*(80+) + .08*(65-79) + .12*mobility
       + N(0, .08); zone severity ~ U(.2, 1). Tiers by rank: top 10% = 3 (life safety, the "top 10%
       by true need"), next 20% = 2, next 30% = 1, rest = 0.
  DW4. Two hidden "dark" zones (communications outage): every report probability there x .30.
  DW5. Silent share: 15% of tier-3 households never report; they are drawn with weight 4 in dark
       zones and weight 2 for language 2, so the silence has a zone pattern the auditor can find.
  DW6. Report probability by tier .95 / .80 / .35 / .10 (tier 3..0), x .80 for language 2,
       x DW4 in dark zones. Report delay after the storm starts ~ exponential, mean 60 min (tier 3)
       or 120 min (others).
  DW7. A report states a severity: the true tier with p = .85, else one tier off (clipped 0..3).
  DW8. Travel = 6 + 4 * grid distance from the depot (minutes); on-site = 15 / 20 / 25 / 30 min for
       tier 0..3.
  DW9. All randomness: random.Random(seed) consumed in a fixed order; no wall clock.
"""

import functools
import random

N_HOUSEHOLDS = 5000          # TODO(FINAL)
N_ZONES = 12                 # TODO(FINAL)
N_CREWS = 30                 # TODO(FINAL)
GRID = (3, 4)                # rows x cols  TODO(FINAL)
DEPOT_ZONE = 5               # TODO(FINAL)
SILENT_SHARE = 0.15          # of tier-3 households  TODO(FINAL)
N_DARK_ZONES = 2             # TODO(FINAL)
TOP_FRAC = 0.10              # "top 10% by true need" = tier 3


def zone_xy(z):
    return divmod(z, GRID[1])


def neighbours(z):
    r, c = zone_xy(z)
    out = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        rr, cc = r + dr, c + dc
        if 0 <= rr < GRID[0] and 0 <= cc < GRID[1]:
            out.append(rr * GRID[1] + cc)
    return out


def travel_s(z):
    (r0, c0), (r, c) = zone_xy(DEPOT_ZONE), zone_xy(z)
    return 60 * (6 + 4 * (abs(r - r0) + abs(c - c0)))                      # DW8


def _pick(rng, probs):
    x, acc = rng.random(), 0.0
    for i, p in enumerate(probs):
        acc += p
        if x < acc:
            return i
    return len(probs) - 1


@functools.lru_cache(maxsize=16)
def _generate(seed):
    rng = random.Random(seed)
    severity = [rng.uniform(0.2, 1.0) for _ in range(N_ZONES)]
    dark = sorted(rng.sample(range(N_ZONES), N_DARK_ZONES))
    zone, age, dev, mob, lang, score = [], [], [], [], [], []
    for _ in range(N_HOUSEHOLDS):
        z = rng.randrange(N_ZONES)
        a = _pick(rng, (0.65, 0.22, 0.13))
        d = int(rng.random() < 0.04 + 0.06 * (a == 2))
        m = int(rng.random() < 0.08 + 0.20 * (a == 2))
        lg = _pick(rng, (0.75, 0.18, 0.07))
        s = (0.40 * severity[z] + 0.25 * d + 0.15 * (a == 2) + 0.08 * (a == 1) + 0.12 * m + rng.gauss(0, 0.08))
        zone.append(z); age.append(a); dev.append(d); mob.append(m); lang.append(lg); score.append(s)
    order = sorted(range(N_HOUSEHOLDS), key=lambda i: (-score[i], i))
    tier = [0] * N_HOUSEHOLDS
    cut = [int(N_HOUSEHOLDS * f) for f in (0.10, 0.30, 0.60)]
    for rank, i in enumerate(order):
        tier[i] = 3 if rank < cut[0] else 2 if rank < cut[1] else 1 if rank < cut[2] else 0
    # silent tier-3 households (DW5): weighted draw without replacement
    t3 = [i for i in range(N_HOUSEHOLDS) if tier[i] == 3]
    n_silent = round(SILENT_SHARE * len(t3))
    w = {i: (4.0 if zone[i] in dark else 1.0) * (2.0 if lang[i] == 2 else 1.0) for i in t3}
    silent, pool = set(), list(t3)
    for _ in range(n_silent):
        tot = sum(w[i] for i in pool)
        x, acc = rng.random() * tot, 0.0
        for k, i in enumerate(pool):
            acc += w[i]
            if x < acc:
                silent.add(i)
                pool.pop(k)
                break
    report_s, stated = [], []
    for i in range(N_HOUSEHOLDS):
        p = (0.10, 0.35, 0.80, 0.95)[tier[i]] * (0.80 if lang[i] == 2 else 1.0) * (0.30 if zone[i] in dark else 1.0)
        u, delay = rng.random(), rng.expovariate(1.0 / (3600.0 if tier[i] == 3 else 7200.0))
        st = tier[i] if rng.random() < 0.85 else min(3, max(0, tier[i] + (1 if rng.random() < 0.5 else -1)))
        reports = (i not in silent) and u < p
        report_s.append(int(delay // 60) * 60 + 60 if reports else None)       # whole minutes after the storm
        stated.append(st if reports else None)
    onsite = [60 * (15, 20, 25, 30)[t] for t in tier]
    registry = {"zone": zone, "age_band": age, "device_dependent": dev, "mobility_limited": mob, "language": lang}
    truth = {"tier": tier, "silent": sorted(silent), "dark_zones": dark, "severity": severity,
             "report_after_s": report_s, "stated_tier": stated, "onsite_s": onsite,
             "top": sorted(i for i in range(N_HOUSEHOLDS) if tier[i] == 3)}
    return registry, truth


def registry(seed):
    """Agent-visible static attributes (copies)."""
    reg, _ = _generate(seed)
    return {k: list(v) for k, v in reg.items()}


def _truth(seed):
    """HIDDEN ground truth. Only pack.py may call this."""
    return _generate(seed)[1]
