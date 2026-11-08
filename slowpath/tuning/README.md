# Strategy tuning (Optuna)

SIMULATED arena. `tune.py` runs a TPE study (`TPESampler(multivariate=True, seed=0)`, defaults enqueued as trial 0; 20 trials by default, TODO(FINAL)) and a random-search baseline (10 trials by default, TODO(FINAL)). Both are stored in `tuning.db` (SQLite). It confirms the top-K (K = 5, TODO(FINAL)) TPE candidates on seeds 60-79, picks the best confirmation score, and writes `tuning_result.json` with `"provenance": "real evaluator, seeds 0-59 search, 60-79 confirm"`.

| Seeds | Use |
| --- | --- |
| 0-59 | search objective (`evaluator.py --mode search`) |
| 60-79 | confirmation of the top-K only (`--mode confirm`) |
| 80-99 | out-of-sample: **never requested by tune.py**; reachable only via the one-shot `--final-eval` |

## Search space

| Parameter | Range | Default (trial 0) |
| --- | --- | --- |
| edge_threshold_ticks | float 1.0-3.0 | 1.5 |
| min_move_cents | int 1-5 (cents) | 2 |
| max_orders_per_event | int 1-4 | 2 |
| size_cap_base | int 2-10 | 5 |
| size_cap_frac | float 0-1 | 1.0 (gives size_cap_max 10) |
| time_stop_us | int 1,000-20,000, log scale | 5000 |
| exit_edge_ticks | float 0.25-1.5 | 0.5 (**assumed**, tune T1; the engine's stated default exit is 0.0, see ENGINE_ASSUMPTIONS E10) |
| hedge_band_shares | int 200-1000 | none confirmed: sampled in trial 0 (T10, TODO(FINAL)) |

The evaluator receives `size_cap_max = base + round(frac * (10 - base))` in place of `size_cap_frac`.

## Objective

`mean over regimes of P25(per-seed net_pnl) - 0.5 * mean(max_drawdown over all runs)`.
The score is -1e9 if the normal regime has fewer than 50 fills in total, or if any run outside `EXPECTED_TRIP_REGIMES` reports a hard trip. `feed_fault` (R03) and `rogue_burst` (R12) trip by design and are expected (T7, TODO(FINAL)). See T1-T11 in `tune.py`.

## Evaluator (real: `evaluator.py`; the stub is archived in `_archive/`)

`evaluator.py` runs `golden/engine.py` over `sim/options_model.py` scenarios. Assumptions EV1-EV6 are in `evaluator.py`; engine assumptions in `docs/ENGINE_ASSUMPTIONS.md`.

```
python evaluator.py --mode search  --params '<JSON>' --seeds 0-59  --regimes quiet,normal,...
python evaluator.py --mode confirm --params '<JSON>' --seeds 60-79 --regimes quiet,normal,...
```

- `--mode` is required (the original contract was silent; tune.py passes it). `search` accepts only seeds 0-59, `confirm` only 60-79. Any seed >= 80 exits with code 2 before anything runs.
- `--params` must have exactly: `edge_threshold_ticks`, `min_move_cents`, `max_orders_per_event`, `size_cap_base`, `size_cap_max`, `time_stop_us`, `exit_edge_ticks`, `hedge_band_shares`.
- stdout is one JSON document, exit code 0: schema `latency-arena-eval/1`, `"simulated": true`, `mode`, `split`, `seeds`, `regimes`, `params`, `params_hash`, `results` (per regime, one entry per seed: `seed`, `net_pnl` $, `max_drawdown` $ >= 0, `fills`, `hard_trips` = latching trips R03/R12-R15, plus extras), and `metrics` (P&L, fills, trips, max drawdown, triggers, `determinism_hash`).
- `validate_result()` in tune.py also requires `"simulated": true`.

### Out-of-sample: one shot only

Seeds 80-99 run only through `--final-eval`. It takes no `--seeds`/`--mode`, creates `final_eval.lock` (holding the parameter hash) before doing anything, writes `final_eval_result.json`, and refuses to run a second time. Run it yourself, once, after the tuning result is final:

```
python slowpath/tuning/evaluator.py --final-eval --from-result slowpath/tuning/tuning_result.json
```

## Commands

```
cd C:\Users\varis\OneDrive\Documents\hackathon
python -m pytest slowpath/tuning golden -q
python slowpath/tuning/tune.py --fresh          # real evaluator, small default trial counts (TODO(FINAL))
```
