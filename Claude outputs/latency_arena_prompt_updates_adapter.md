# Prompt updates after the Nemotron core's ScenarioAdapter interface

These REPLACE P3 and PATCH P2 and P4 from latency_arena_claude_code_prompts.md. P0a, P0b, P1, P5 and P6 are unchanged.

New run order: P0a, P0b, P1, **P2, then P3** (P3 uses the twin), with P5 and P6 in parallel alongside them. P4 is probably unnecessary (see the end).

Before running P3, put the two files from the teammate into the repo:
- `interface.py` copied VERBATIM to `backend/app/interface.py` (never edit it)
- `mock_trading.py` to `backend/app/adapters/mock_trading.py` (reference only, never edit it)

---

## P2 patch (append to the P2 prompt)

```
ADDITION: also expose the twin as an importable in-process class, not only a TCP server:
  class TwinSession with start(seed), tick(n=1), status() -> dict decoded from the latest 0x20 frame,
  log_entries() -> list decoded from 0x21 frames, send_frame(bytes) -> result (same handling as TCP),
  inject(kind) for "gate_trip" (drives a REAL jump above the R04 limit through the engine so the latch is
  genuine, not a flag flip), "stale_feed" (delays the simulated feed), and "reset" (fresh seed).
The TCP server wraps this class. Every dict it returns is JSON-safe and carries "source": "software_twin".
Add tests for inject("gate_trip") latching the gate through the real gate_eval path.
```

---

## P3 (REPLACEMENT). TradingAdapter for the Nemotron agent core

```
[Paste SHARED RULES here]

TASK: Implement TradingAdapter, the real scenario adapter for the Nemotron agent core. The core is built by a teammate and
is already running against a mock. Your job is ONLY the adapter and what it needs. Do NOT build agents, a supervisor loop,
Nemotron tool schemas, a FakeLLM, or LLM client code; the core owns all of that and its tool names are frozen as the
adapter's 8 methods plus audit_log. Depends on P1 and P2.

You own: backend/app/adapters/trading.py (new, class TradingAdapter), backend/app/adapters/_trading_support/ (helpers),
tests/adapter/. Read-only: backend/app/interface.py (verbatim from the teammate, NEVER edit), backend/app/adapters/mock_trading.py
(a working reference for shapes and behavior; copy its dict keys and entry shapes, not its fake numbers or fake logic).

Contract (from interface.py; read it fully): observe, detect, actions, simulate, invariants, check, apply, report_template,
audit_log(limit=50), plus the demo hook inject(kind). Use Optional[X], not X | None. Every dict must be JSON-safe
(assert json.dumps(to_json(x)) works in tests). Plugging in: SCENARIO_ADAPTER=trading in .env, nothing else.

Data source: TwinSession from P2 by default. A flag (default OFF, TODO(FINAL)) can read the real board by decoding 0x20/0x21
with slowpath/frames.py. Sending frames to the real board is off by default.

Method requirements:

observe() -> dict. Use the same top-level keys as the mock so the core and UI keep working: source, clock_s,
  gate{armed, latched, trip_reason, killed}, token_bucket, bad_frames_1m, feed_age_ms,
  position{symbol, net_delta, open_orders}, pnl_today, config. Fill them from the decoded 0x20/0x21 frames and the engine.
  symbol is "SPY". "source" must say SIMULATED and twin. If the real status frame has no field for one of the mock's keys
  (read docs/FRAMES.md), return null for it and list it in the report; do NOT fabricate a value.

detect(state) -> list[Incident]. Incident has id (stable, e.g. "inc-gate"), kind, severity (1 low, 2 high, 3 critical),
  detail (JSON-safe), at (scenario clock). Emit: gate_trip (detail has the REAL rule id from gate_eval, e.g. R09 for net
  delta, and its value and limit; do NOT copy the mock's rule numbers or limits), stale_feed, bad_frames, and any others the
  real frames support. Sorted most severe first.

actions(incident) -> list[Action].
  - kill: Action("kill", ..., {"frame": "0x15"}, reduces_risk=True). It is the only action the supervisor runs without a human.
  - config changes: params {"frame": "0x12", <tunable>: <value>}, reduces_risk=False. Tunables come ONLY from the allowlist
    (derive it from GATE_SPEC.md and the config decoder, TODO(FINAL)): edge_threshold_ticks, min_move_cents, size_cap,
    hedge_band_shares. Do not use the mock's market-making knobs (quote width etc.); this strategy takes stale quotes.
  - Labels are plain words for a human.
  - One deliberately refused demo action is allowed (the mock offers "raise max delta" so the refusal shows on stage). Include
    it behind DEMO_REFUSAL_ACTION=true (default true, TODO(FINAL)), and label it as a demonstration of the authority limit.

simulate(state, action, seeds) -> Outcome. The core ALWAYS passes seeds 0 to 99. Apply this rule:
  - Run the real engine (P1) ONLY for seeds < 80, from a fresh seeded start with the current config plus the action's tunable
    overrides. State this limitation in the Outcome metrics ("fresh-start replay, not the live state").
  - Seeds >= 80 are NEVER run here. Return out_of_sample = {"evaluated": False, "note": "reserved for the one-shot final
    evaluation"}. The out-of-sample split is evaluated once by the final-eval command; never by simulate().
  - in_sample aggregates seeds 0-79, with "search" (0-59) and "confirm" (60-79) sub-aggregates inside the same dict. Keys:
    evaluated, seeds, mean_pnl, mean_max_drawdown, mean_gate_trips (same names as the mock), plus any extras.
  - source = "SIMULATED (golden engine, twin timing not FPGA latency)". No profit claims.
  - Performance: the core calls simulate for the baseline and every candidate over 80 seeds. Measure it. Cache results by
    (params hash, seed). If a full call takes more than about 10 seconds (TODO(FINAL)), speed it up (cache, multiprocessing)
    and report the measured time.

invariants() -> list[str]. Plain-language rules, accurate to this system: supervisor may request kill (0x15) without a human
  and it only reduces risk; config changes (0x12) need a named human approver; no action may arm the gate or clear a latch;
  no action may change a hardware limit (use the REAL limit names from the gate spec); no action may raise exposure.

check(action) -> (bool, str). Refuse with a reason string: any key in the hardware-limit set (names from the gate spec), arm,
  clear_latch, a size_cap increase (raises exposure), any param outside the tunable allowlist, any value outside its allowed
  range (search ranges from P1). Anything else that loosens risk beyond that is TODO(FINAL): decide and document.

apply(action, approved_by) -> ApplyResult(ok, reason, detail). Always call check() first. kill: send frame 0x15 to the twin and
  return detail {"frame": "0x15"}. Config: reduces_risk False and approved_by empty means refuse ("needs a named human
  approver"); otherwise build the 0x12 frame with the existing encoder and send it to the twin, return detail
  {"frame": "0x12"}. Never arm, never clear a latch. Every refusal and every application is written to the audit log.

report_template() -> dict. Same structure as the mock (proposal and post_mortem section lists).

audit_log(limit=50) -> list[dict], newest last. Keep the mock's entry shape: {t, event, data, prev, hash}. hash =
  SHA-256 of (prev + canonical JSON of the entry without its hash), full hex, genesis prev = 64 zeros. Persist as JSONL.
  Write boot, inject, refused, and applied events. Provide verify_chain() that detects tampering with any entry.

inject(kind) -> dict (demo hook, not part of the contract): "gate_trip", "stale_feed", "reset"; delegate to TwinSession.inject so
  the trip is real.

Tests (offline):
1. Contract test: run the sequence the agents run: observe, inject gate_trip, detect, actions, check each action, simulate
   baseline and each action over seeds 0..99, apply kill without approver (ok), apply config without approver (refused), apply
   config with approver (ok), apply a hardware-limit change (refused), arm and clear_latch (refused), then audit_log.
2. JSON-safety of every return value.
3. Seed guard: instrument the engine and assert it is NEVER called with a seed >= 80 across the whole test run.
4. Audit chain verifies, and detects a tampered entry.
5. Determinism: the same state, action and seeds give identical Outcome.
6. No label anywhere says FPGA latency for twin timing.
Report which tests ran and their exact results, and list every mock key you could not source from the real frames.
```

---

## P4 change

The disaster adapter is built by Haashim against the same ScenarioAdapter interface. Skip P4 unless he is not covering it. If he is, give him the spec below; it is the design we already agreed:

- 5,000 households (zone, age band, device dependence, mobility, language, hidden true need), 12 zones, 30 crews, a fixed share of silent high-need households that never report (share TODO(FINAL)).
- Baseline: first-come-first-served queue.
- Care rules, used as `check()`: C1 life-safety floor, C2 starvation timer, C3 no double assignment, C4 capacity, C5 heartbeat fallback to the rule queue, C6 one-way authority (the agent proposes, it never overrides a care rule), C7 hash-chained audit.
- Metrics: time to first contact for the top 10% by true need, count overlooked, crew utilization. An overlooked-household auditor finds the silent ones from zone patterns.
- Demo: shock button starts the storm, an AI downgrade is injected and refused by the care rules, kill falls back to the rule queue, the commander accepts the proposal.
- The same seed rule applies: `simulate()` runs seeds below 80 only and returns `out_of_sample = {"evaluated": False}`.
