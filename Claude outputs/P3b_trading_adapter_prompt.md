# P3b: TradingAdapter wrapping the existing backend

Paste everything inside the code block into Claude Code (it includes the shared rules).
Before running: confirm `Claude outputs/interface.py` and `Claude outputs/mock_trading.py` are in the repo folder (they are).

```
SHARED RULES. Read before doing anything.
- Repo: C:\Users\varis\OneDrive\Documents\hackathon. There is NO git and NO backups.
  Before editing any existing file, copy it to _archive/<same relative path>.<YYYYMMDD-HHMM>.
  Never delete anything; move it to _archive/ instead.
- Do NOT modify verified files: fpga/io UART, status and top files, pin assignments, any existing
  Quartus project file, and everything in tools/.
- Only touch the paths listed under "You own". Another session may be working elsewhere.
- Every value that was not confirmed is a placeholder. Mark it TODO(FINAL) at the point of use and append
  a line to docs/PLACEHOLDERS.md (append only). Do NOT invent numbers, field layouts, or formats.
- All market data is SIMULATED. No network access, no paid APIs. Tests run offline.
- Any timing measured in Python or in the software twin is "twin timing". Never call it FPGA latency.
- No profit claims. Say "simulated arena".
- Do not claim a test passed unless you ran it and saw it pass. If something is missing, say so.
- Do not run git init, do not push anything.
- 80/20 split: search on seeds 0-59, confirm on 60-79, evaluate on 80-99 ONCE. Never run seeds 80+ yourself.
- Finish with a report: (1) files created/changed, (2) REAL: implemented and verified, (3) STUBBED or UNVERIFIED,
  (4) every test run with its exact result, (5) new TODO(FINAL) items.

TASK (P3b): add a TradingAdapter that WRAPS the existing backend. Do not rebuild anything that already exists in backend/.

You own: latency_arena_adapter/ (new top-level folder) and tests/adapter/.
Layout (mirrors the teammate's repo so the folder can be dropped in as backend/app/):
  latency_arena_adapter/app/__init__.py
  latency_arena_adapter/app/interface.py        copy of 'Claude outputs/interface.py', byte-for-byte, NEVER edit
  latency_arena_adapter/app/adapters/__init__.py
  latency_arena_adapter/app/adapters/trading.py class TradingAdapter
Read-only reference: 'Claude outputs/mock_trading.py' (reuse its dict keys, action ids and audit entry shape; NOT its fake
numbers or fake logic). Do NOT create backend/app/ (it would clash with the existing backend/app.py).

trading.py imports the existing backend (backend/runtime.py Backend, packs/options, authority.py, audit.py,
datasource.py TwinSource, configmap.py allowlist). Use backend/_paths.py (or an LA_REPO_ROOT env var) to put the repo root
on sys.path so `from backend...` works when the folder is copied elsewhere. Use `from ..interface import ...` like the mock.
Python 3.9 safe: Optional[X], no X | None. Every returned dict JSON-safe (assert json.dumps(to_json(x))).

Implement the contract (read interface.py fully): observe, detect, actions, simulate, invariants, check, apply,
report_template, audit_log(limit=50), plus the demo hook inject(kind).

- observe(): same top-level keys as the mock: source, clock_s, gate{armed, latched, trip_reason, killed}, token_bucket,
  bad_frames_1m, feed_age_ms, position{symbol, net_delta, open_orders}, pnl_today, config. Fill from the twin and decoded
  0x20/0x21 frames. symbol is "SPY". source says SIMULATED and twin. If the real frames have no field for a mock key, return
  null for it; NEVER invent a value; list those keys in the report.
- detect(state): Incident(id, kind, severity 1-3, detail, at). gate_trip must carry the REAL rule id and limit from the gate
  (net delta is R09 etc.), not the mock's numbers. Add stale_feed, bad_frames where the real data supports them. Most severe first.
- actions(incident):
  * kill: Action("kill", ..., {"frame": "0x15"}, reduces_risk=True). The only action run without a human.
  * config actions: {"frame": "0x12", <tunable>: value}, reduces_risk=False, using ONLY the allowlist in
    backend/packs/options/configmap.py (the 0x12 decoder fields). Keep the mock's action ids where a matching tunable exists
    (raise_edge -> edge_threshold_ticks, raise_min_move -> min_move_cents, cut_size_cap -> size_cap_base/size_cap_max).
    The mock's tighten_hedge has NO 0x12 field, so drop it (or map it to another real tunable and say so in the report).
    Never propose values outside the allowed range.
  * raise_delta demo: a deliberately REFUSED action showing the authority limit, only on gate_trip, behind
    DEMO_REFUSAL_ACTION (default "true"). Label it as a demonstration.
- simulate(state, action, seeds): the core ALWAYS passes seeds 0-99.
  * Run seeds < 80 ONLY, through the existing replay/evaluator path (fresh seeded start with the current config plus the
    action's overrides; state this limitation in metrics).
  * Seeds >= 80 are NEVER run. out_of_sample = {"evaluated": False, "note": "reserved for the one-shot final evaluation"}.
  * in_sample = seeds 0-79 with "search" (0-59) and "confirm" (60-79) sub-aggregates inside the dict. Keys evaluated, seeds,
    mean_pnl, mean_max_drawdown, mean_gate_trips (mock names) plus extras.
  * source = "SIMULATED (golden engine, twin timing not FPGA latency)".
  * Time one call (baseline + each action over 80 seeds). If one simulate() call takes more than ~10 s (TODO(FINAL)), cache by
    (params hash, seed) and/or parallelize; report the measured time.
- invariants(): plain-language rules accurate to this system, using the REAL hardware-limit names.
- check(action) -> (bool, reason): route through backend/authority.py. Refuse: arm, clear_latch, any hardware-limit key, size
  increases, unknown keys, out-of-range values. Reasons are plain sentences.
- apply(action, approved_by) -> ApplyResult(ok, reason, detail): check() first. kill goes to the twin as frame 0x15
  (detail {"frame": "0x15"}). A 0x12 config with reduces_risk False and no approved_by is refused ("needs a named human
  approver"); otherwise Backend.accept path builds the 0x12 frame (detail {"frame": "0x12"}). Never arm, never clear a latch.
  All refusals and applies are audited.
- audit_log(limit=50): existing audit.py entries as {t, event, data, prev, hash}, newest last.
- inject(kind): "gate_trip" (must cause a REAL latch through the engine, not a flag flip), "stale_feed", "reset".

Tests (offline) in tests/adapter/:
1. The agents' sequence: observe, inject gate_trip, detect, actions, check each, simulate baseline and each action over
   seeds 0..99, apply kill (no approver ok), apply config without approver (refused), with approver (ok), hardware-limit change
   (refused), arm and clear_latch (refused), audit_log.
2. JSON-safety of every return value.
3. Seed guard: assert the engine is NEVER called with a seed >= 80 across the whole run.
4. Audit chain verifies (backend verify_chain) and a tampered entry is detected.
5. Determinism: same state, action and seeds give identical Outcome.
6. No label says FPGA latency for twin timing.
Report what ran with exact results, which mock keys had no real source, the measured simulate() time, and exactly how the teammate
should plug it in (copy latency_arena_adapter/app into their backend/app/, and set LA_REPO_ROOT or PYTHONPATH to this repo).
```
