# Known limitations

SIMULATED universe ("simulated arena"). All universe numbers come from the universe block in
`golden/golden.py` (single source of truth, every value TODO(FINAL)); none are repeated here as
fixed facts. Numbers below are measurements at that calibration and must be re-measured if it changes.

## L1. Fast-path reprice is not monotone for far-OTM calls near -jump_limit

**Status:** accepted. No RTL change. The FPGA reproduces it bit for bit because it matches `golden.reprice()`.

### What happens
As S rises one raw step (1/16 cent) at a time, a call's `theo_new` should never fall. For far-OTM calls it can drop by **1 LSB (1/256 tick = $0.0039 per contract)** at a few points inside the R04 window.

Measured with `JUMP_LIMIT_CENTS` (recomputed by `golden/error_curve.py`), sweeping every raw S from
S_ref - jump_limit up past dS saturation. "Pinned grid" = the strike grid of the default spot
(`golden.STRIKES`) held fixed while S_ref is moved below it:

| Grid | S_ref position | Dip points (all 1 LSB) | Contracts (delta_adj) |
| --- | --- | --- | --- |
| recentered (A22, every snapshot) | every spot tested (700, 730, 733.50, 735.49, 760, 800) | 0 | none |
| pinned | $42-$52 below the lowest strike (S_ref 680, 690) | 4, 114 | delta_adj about 0.000-0.001 |
| pinned | $20-$32 below the lowest strike (S_ref 700-712) | 273 to 1,649 | delta_adj about 0.005-0.06 |
| pinned | $16-$18 below the lowest strike (S_ref 714, 716) | 7, 13; dS about -367 to -342 c | delta_adj about 0.08-0.12 |
| pinned | $14 or less below the lowest strike (S_ref 718-733.50) | 0 | none |

With strike recentering (A22) on every snapshot, the strikes always bracket spot, every call keeps
delta_adj well above 0.12, and there are no dips. The dip needs a grid that is stale by more than
about $16, which a recentering snapshot builder never produces.

### Why
The repriced curve is `theo_0 + delta*dS + gamma/2*dS^2`. On the down side, its slope `delta + gamma*dS` approaches zero near dS = -delta/gamma. For a call with delta_adj below about 0.12, that point is inside the R04 window. There, the true change per raw step is well under 1 LSB. `p1` and `p2` are rounded separately (`rshift_round` by 12 and by 13), so their rounding errors can briefly push the sum one LSB backwards. Rounding causes this, not a sign error: the dip is never larger than 1 LSB.

### Impact
- The error is at most 1/256 tick, against the 1.5-tick (384 LSB) edge threshold and the 0.5-tick model margin. It cannot flip a fire decision unless the edge sits within 1 LSB of 384.
- It shows up only after a large down move close to -jump_limit, with a strike grid far above S_ref. R04 rejects anything beyond jump_limit.

### Where it is tested
`golden/test_golden.py`:
- `test_reprice_monotonic_calls`: strictly monotone on the recentered grid at several spots.
- `test_reprice_monotonic_calls_default_grid`: strictly monotone with the grid pinned, S_ref up to $14 below the lowest strike.
- `test_reprice_monotonic_otm_calls_near_jump_limit`: strict xfail with the grid pinned and S_ref = 714 (regenerated for the SPY calibration; replaces the legacy spot-140 case). It documents the dip and will fail loudly if the dip goes away.
- `test_reprice_otm_call_dip_is_at_most_one_lsb`: pinned grid, S_ref 700-716, dip <= 1 LSB.

### If it ever needs fixing
A fix would mean rounding fewer times: for example, add the p1 and p2 products at full precision and round once. That changes the golden formula in the project context, so it would need a spec change, new vectors, and its own monotonicity test (`g1` is itself rounded, so a single final rounding is not monotone by construction). Not planned.

## L2. R10 net-gamma limit is effectively looser on SPY (TODO(FINAL))

R10 stays at 0.03 delta per cent. Per-contract gamma_c at the SPY calibration is about 2.4x smaller
than in the legacy universe (measured 2.39x over the 8 snapshot contracts), so R10 binds about 2.4x
later in contract count. Not rescaled because it is not confirmed; see docs/PLACEHOLDERS.md.
