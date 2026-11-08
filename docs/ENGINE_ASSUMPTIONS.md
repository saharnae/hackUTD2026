# Engine assumptions (golden/engine.py, slowpath/tuning/evaluator.py)

SIMULATED arena. Every number the engine produces is simulated; timings are "twin timing"
(simulated microseconds in the software twin), never FPGA latency. Guessed values are marked
TODO(FINAL) here, at their point of use, and in docs/PLACEHOLDERS.md.

The engine only calls the bit-exact golden functions (reprice, microprice_q4, build_snapshot,
quote_sane, edge_buy/edge_sell, gate_eval); none of them were changed.

## Market view and snapshots
- **E1. S is the underlying microprice** (`microprice_q4` of the displayed quote, cents Q4). Insane underlying quotes (crossed / zero bid) are ignored for S; the last sane S stays.
- **E2. Strike grid** = `golden.strikes_for(session S_ref)`, the same grid the simulator quotes; the engine pins it for the session (the simulated session never drifts far enough for L1).
- **E3. Snapshots**: the first at book-ready (all 9 instruments seen), then every `snapshot_interval_us` (100,000, README; TODO(FINAL)) at fixed boundaries, priced at the last sane S before the boundary, `ref_seq` = last feed seq processed, committed instantly (commit delay 0, TODO(FINAL)).
- **Finding:** R02 blocks an order when the feed is more than 64 messages past `ref_seq`. The simulated feed carries about 6 messages per ms, so with a 100 ms interval R02 is open only for the first ~11 ms after each snapshot. This is the spec as written; it dominates the reject counts (see the evaluator output). Not changed here: it needs a decision (snapshot interval, or the R02 window).

## Orders and fills
- **E4. Order latency** (send -> arrival at the market) = `order_latency_us` = 10 us twin timing. TODO(FINAL).
- **E5. An option order fills only if the exact quote it targeted is still live at arrival**: the last simulator quote for that contract with t <= arrival must be the one the trigger saw (same quote time) at the same price. A market-maker re-quote in between = miss ("requoted"). Uses the simulator's own re-quote times (median 100 us lognormal delay, 20% stagger).
- **E6. Rivals take first**: if a simulator rival hit the same contract, side and quote before our arrival, it took the full displayed size (sim S6) and we miss ("rival").
- **E7. Size**: fill = min(order qty, displayed size minus what we already took from that same quote). Price = the quoted price. Orders are IOC: an unfilled order is gone.
- **E8. Hedge orders (underlying)** fill in full at their limit if the underlying touch at arrival is at or better than the limit. The simulator's underlying sizes (1-40) are not treated as depth. TODO(FINAL).
- **E9. Fees** 0.5 tick = $0.50 per option contract (golden FEE_TICKS_PER_CONTRACT, TODO(FINAL)); no fee on shares. TODO(FINAL).

## Strategy
- **E11. Event** = one feed message. Entries are evaluated on an underlying message when |S - S at the last evaluation| >= min_move (cents, compared in Q4). Order selection = golden `trigger()` with the soft parameters passed in (`engine.trigger_p`, identical at the defaults, tested). Edges with an order in flight are locked out. `orders_in_event` resets per message.
- **E10. Exit policy (default, TODO(FINAL))**: close a position when its entry-side edge (theo_new - ask for a long, bid - theo_new for a short) is no longer above `exit_edge_ticks`, or when it has been held `time_stop_us` (README 5,000 us; TODO(FINAL)). Engine default `exit_edge_ticks = 0.0` = "edge no longer positive" as specified. tune.py keeps its existing search range 0.25-1.5 and trial-0 value 0.5 (tune T1) - this mismatch is open. TODO(FINAL). Exits are reduce-only orders crossing at the displayed bid/ask, qty <= min(position, 10, displayed size), through the gate.
- **Hedging**: after every message, if |100 * sum(pos * delta_adj) + spy_shares| (the gate's R09 quantity, pending included) exceeds `hedge_band_shares` (200-1000, no default, TODO(FINAL)), one hedge order (qty <= 1,000, R07) is sent toward zero at the underlying touch. Only one strategy hedge is in flight at a time. R09 (20.00) is enforced by the gate on every order.
- **Rogue burst**: the simulator's 50 rogue hedge frames are sent through the gate as one event. They latch R12 only when they reach the 9th order of the event; if R02 rejects them first, nothing is consumed and no trip happens.

## Gate, latch, kill
- **E12. Flatten**: on any latch (R03, R12-R15) the engine liquidates every option and share position at the last sane displayed touch, in full, with fees, outside the gate (the operator / laptop path). Orders still in flight resolve normally and are flattened again. TODO(FINAL).
- **E13. Arming**: the operator raises ARM as soon as book-ready, a snapshot that passes R01/R02, and >= 2 heartbeats all hold. Orders are refused before that, and always while latched (G7).
- **E14. Heartbeats** arrive every 50,000 us starting at t = 0 (FRAMES.md 0x11), so R14 never trips in the evaluator. TODO(FINAL).
- **Latching**: R03 latches on the feed-integrity level (any gap or duplicate seen; the simulator's CRCs are always good). R12 latches on any gate result with fail bit 12 (G11). R13 latches on the equity condition, checked every message (G8). R15 latches on the kill input. Non-latching blocks (R01, R02, R04-R11) just reject.
- **Clearing**: only `Engine.local_rearm()` clears a latch (the explicit operator path: KEY0 with ARM low, then ARM rising). The engine never calls it. It refuses while the kill input is asserted and leaves the engine disarmed until the arming conditions hold again.
- **Kill**: `set_kill()` (or `run(..., kill_at_us=...)`) sets the kill input, flattens and latches R15. It can only make things safer.
- **Token bucket**: 20 burst, +1 every 5,000 us of session time, -1 per approved order.

## P&L
- Cash is integer cents. Realized P&L uses exact average-cost accounting (fractions) and includes fees. Unrealized = positions marked to model: options at golden `theo_new` (reprice of the active snapshot at the current S), shares at S. Identity checked on every result: cash + marked positions - starting cash == realized + unrealized (exact).
- Max drawdown = largest peak-to-trough drop of that marked equity, sampled after every message, in dollars.
- Open positions at the end of a session stay open and are marked to model (not liquidated).

## Evaluator (EV1-EV6 in evaluator.py)
- `--mode search` accepts only seeds 0-59 and `--mode confirm` only 60-79; any seed >= 80 is refused before any scenario is built. Seeds 80-99 run only via `--final-eval`, which creates `slowpath/tuning/final_eval.lock` (O_EXCL, holds the parameter hash) before doing anything and refuses a second run.
- Output: schema `latency-arena-eval/1` plus `"simulated": true`, seeds, split label, params and params hash, per-run results, and aggregate metrics (P&L, fills, trips, max drawdown, triggers, determinism hash).
- tune.py treats feed_fault (R03) and rogue_burst (R12) trips as expected (tune T7, TODO(FINAL)); a trip in any other regime fails the trial.
