"""Error curve: fixed-point reprice() vs a full Black-Scholes reprice under the vol rule.

SIMULATED universe (placeholder SPY parameters from golden.py, all TODO(FINAL)).

Full reprice at S = S_ref + dS, same T (instantaneous move):
    sigma_new(K) = sigma(m_ref) + lambda * (smile(ln(K/S)) - smile(ln(K/S_ref))) + beta * dS_dollars
Its first derivative at dS = 0 is exactly the CLAUDE.md vol rule
-lambda*(b + 2cm)/S_ref + beta, so delta_adj matches the full model's slope.
lambda = 0 is sticky strike (each contract keeps its snapshot vol);
lambda = 1 moves along the smile (sticky moneyness).

The snapshot (theo_0, delta_adj, gamma_c) is rebuilt with the same lambda.

Usage: python golden/error_curve.py
"""

import math
import os

import numpy as np

import golden as g

MODEL_MARGIN_TICKS = 0.5
DS_CENTS = np.arange(-500, 501, 5)
HERE = os.path.dirname(os.path.abspath(__file__))


def full_reprice_ticks(c, S_ref, dS_cents, lam, beta=g.VOL_BETA,
                       a=g.SMILE_A, b=g.SMILE_B, cc=g.SMILE_C):
    S = S_ref + dS_cents / 100.0
    m_ref, m_new = math.log(c["K"] / S_ref), math.log(c["K"] / S)
    sigma = (g.smile_sigma(m_ref, a, b, cc)
             + lam * (g.smile_sigma(m_new, a, b, cc) - g.smile_sigma(m_ref, a, b, cc))
             + beta * dS_cents / 100.0)
    return g.bs(S, c["K"], g.T_YEARS, g.RATE, g.DIV, sigma, not c["is_put"])["price"] * 100.0


def error_curve(lam):
    """Returns (errors[n_ds, 8] in ticks, snapshot)."""
    snap = g.build_snapshot(lam=lam)
    S_ref = g.from_fixed(snap["S_ref"], g.FMT_S) / 100.0
    err = np.zeros((len(DS_CENTS), 8))
    for j, dS in enumerate(DS_CENTS):
        ds_raw = g.ds_raw(snap["S_ref"] + int(dS) * 16, snap["S_ref"])
        for i, c in enumerate(snap["contracts"]):
            fx = g.reprice(c["theo_0"], c["delta_adj"], c["gamma_c"], ds_raw) / 256.0
            err[j, i] = abs(fx - full_reprice_ticks(c, S_ref, dS, lam))
    return err, snap


def first_exceed(ds, worst, limit=MODEL_MARGIN_TICKS):
    """(first dS > 0 with worst > limit, first dS < 0 with worst > limit); None if never."""
    up = next((int(d) for d, e in zip(ds, worst) if d > 0 and e > limit), None)
    dn = next((int(d) for d, e in zip(ds[::-1], worst[::-1]) if d < 0 and e > limit), None)
    return up, dn


def proposed_jump_limit(up, dn, step=5):
    """Largest symmetric |dS| (cents) on the grid strictly before either side exceeds."""
    cands = [abs(x) - step for x in (up, dn) if x is not None]
    return min(cands) if cands else None


def adopted_jump_limit():
    """One jump_limit for both lambda modes: the tighter of the two proposals (cents)."""
    out = []
    for lam in (0.0, 1.0):
        err, _ = error_curve(lam)
        out.append(proposed_jump_limit(*first_exceed(DS_CENTS, err.max(axis=1))))
    return min(x for x in out if x is not None)


def hand_check(step_cents=1, max_cents=500):
    """Nearest-strike call (734 at the default spot), pure float delta-gamma Taylor vs full BS,
    vol held fixed. Two flat-vol readings: (i) truly flat at the smile level a (no smile),
    (ii) the smile vol at K held fixed (sticky strike). Returns {label: (up, dn)} in cents.
    """
    S = g.SPOT
    K = g.strikes_for(S)[-g.STRIKE_OFFSET]                       # nearest strike (A22)
    out = {}
    for label, sigma in ((f"flat {g.SMILE_A:.0%}", g.SMILE_A),
                         ("smile vol held", g.smile_sigma(math.log(K / S)))):
        gr = g.bs(S, K, g.T_YEARS, g.RATE, g.DIV, sigma, True)
        ds = np.arange(-max_cents, max_cents + 1, step_cents)
        err = []
        for d in ds:
            dd = d / 100.0
            taylor = gr["price"] + gr["delta"] * dd + 0.5 * gr["gamma"] * dd * dd
            full = g.bs(S + dd, K, g.T_YEARS, g.RATE, g.DIV, sigma, True)["price"]
            err.append(abs(taylor - full) * 100.0)
        out[label] = first_exceed(ds, np.array(err))
    return out


def main():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [f"{'P' if i >= 4 else 'C'}{int(g.STRIKES[i % 4])}" for i in range(8)]   # default-spot grid
    results = {}
    for lam in (0.0, 1.0):
        err, _ = error_curve(lam)
        worst = err.max(axis=1)
        up, dn = first_exceed(DS_CENTS, worst)
        results[lam] = (err, worst, up, dn, proposed_jump_limit(up, dn))

    print(f"SIMULATED {g.UNDERLYING_SYMBOL} universe (spot {g.SPOT}). Worst |reprice - full BS| across 8 contracts, ticks")
    print(f"{'dS (c)':>7} | {'lam=0':>7} {'worst':>5} | {'lam=1':>7} {'worst':>5}")
    for j, dS in enumerate(DS_CENTS):
        if dS % 50 == 0:
            row = f"{int(dS):>7} |"
            for lam in (0.0, 1.0):
                err = results[lam][0]
                row += f" {err[j].max():7.3f} {names[int(err[j].argmax())]:>5} |"
            print(row.rstrip("|"))
    print()
    print(f"First dS with worst error > {MODEL_MARGIN_TICKS} tick (5-cent grid):")
    for lam in (0.0, 1.0):
        _, _, up, dn, jl = results[lam]
        print(f"  lambda={lam:g}: up side {up} c, down side {dn} c -> proposed jump_limit "
              f"{jl} c (${jl / 100:.2f})" if jl is not None else f"  lambda={lam:g}: never exceeds")
    print(f"  adopted (one constant, both modes): golden JUMP_LIMIT_CENTS = {g.JUMP_LIMIT_CENTS} c "
          f"= min of the two proposals ({adopted_jump_limit()} c)")
    print()
    print(f"Hand check, {g.strikes_for(g.SPOT)[-g.STRIKE_OFFSET]:g} call, float delta-gamma vs full BS, "
          "vol fixed (1-cent grid):")
    for label, (up, dn) in hand_check().items():
        print(f"  {label:>15}: exceeds 0.5 tick at +{up} c / {dn} c")

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for lam, style in ((0.0, "-"), (1.0, "--")):
        ax.plot(DS_CENTS, results[lam][1], style, label=f"worst of 8, lambda={lam:g}")
    ax.axhline(MODEL_MARGIN_TICKS, color="red", lw=1, label="model_margin 0.5 tick")
    for x in (-g.JUMP_LIMIT_CENTS, g.JUMP_LIMIT_CENTS):
        ax.axvline(x, color="gray", lw=1, ls=":",
                   label=f"jump_limit +/-{g.JUMP_LIMIT_CENTS} c" if x > 0 else None)
    ax.set_xlabel("dS (cents)")
    ax.set_ylabel("|reprice - full BS| (ticks)")
    ax.set_ylim(0, 3)
    ax.set_title(f"Fast-path repricing error vs full Black-Scholes (SIMULATED {g.UNDERLYING_SYMBOL})")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    path = os.path.join(HERE, "error_curve.png")
    fig.savefig(path, dpi=120)
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
