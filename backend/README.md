# backend/ (SIMULATED arena)

FastAPI backend: pack interface, domain-free engine, options pack, hash-chained audit log, one authority
choke point, Nemotron tool schemas, and an agent skeleton driven by a FakeLLM. Nothing here talks to a
network or the board unless you turn it on (serial is off by default; the Nemotron transport is TODO(FINAL)).

## Run

```
cd C:\Users\varis\OneDrive\Documents\hackathon
python -m pytest backend/tests -q                 # offline, FakeLLM only (~30 s)
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Env flags (none required): `LA_AUDIT_LOG` (default `backend/audit_log.jsonl`), `LA_USE_SERIAL=1` + `LA_SERIAL_PORT=COM3`
(read-only serial source), `LA_LLM_BACKEND=nemotron` (raises until the CS teammate wires the transport).
`NEMOTRON_BASE_URL`, `NEMOTRON_MODEL`, `NVIDIA_API_KEY` are read only by `agents/llm.py` at call time, and are redacted everywhere.

## Layout

| Path | What |
| --- | --- |
| `packs/base.py` | `Pack` protocol and the domain-free `Engine` (forecast, what_if, plan_actions). Conventions PK1-PK4. |
| `packs/options/` | Options pack: `pack.py` (OP1-OP6), `twin.py` (prefix replay of golden/engine.py, TW1-TW6), `replay.py` (seed guard + real evaluator, RP1-RP3), `configmap.py` (0x12 allowlist + hardware-limit names, CM1-CM3), `frames_io.py` (0x20 / 0x21 codecs). |
| `audit.py` | Append-only JSONL, `hash = SHA-256(prev_hash + canonical JSON without hash)`, genesis 64 zeros, `verify_chain()` (AU1-AU4). |
| `authority.py` | `authorize(actor, action, payload)`: the one choke point. Refusals are audited, then raised (AT1-AT2). |
| `runtime.py` | `Backend`: every API route and every agent tool goes through it. |
| `datasource.py` | `TwinSource` (default) / `SerialSource` (off; writes disabled). |
| `agents/` | `tools.py` (6 JSON-schema tools), `llm.py` (FakeLLM, NemotronClient seam), `orchestrator.py`, `monitor.py`, `stress.py`, `proposal.py`, `postmortem.py`, `policy.py` (deterministic FakeLLM responses). |
| `app.py` | FastAPI routes. |

## Endpoints (proposed, TODO(FINAL))

`GET /status`, `GET /log?limit=`, `POST /sim/step {dt_s, new_session?, seed?, regime?, operator}`,
`POST /supervisor/run {dt_s, max_steps, operator}`, `GET /proposals`, `POST /proposals/{id}/accept {approver}`,
`POST /proposals/{id}/deny {approver, reason}`, `POST /kill {operator, reason}`, `GET /audit/verify`.
Extras: `GET /audit?limit=`, `GET /reports`. Refusals return 403, unknown ids 404, wrong state 409.
There is no HTTP authentication (AT1): bind to 127.0.0.1.

## Authority

Supervisor (any agent): read status / log / audit, simulate, forecast, what_if, replay, plan, propose a 0x12
draft (allowlisted tunables, in range), request a kill (0x15). Refused for everyone in software: arm, clear a latch,
change a hardware limit. Supervisor-refused: accept / deny, sending a config frame, anything unlisted.
`Backend.accept()` (a named human) is the only code that builds a 0x12 frame; deny sends nothing.

Tunable allowlist (TODO(FINAL)) = the 0x12 decoder fields in `slowpath/frames.py`, in engine units:
`edge_threshold_ticks` 1.0-3.0, `exit_edge_ticks` 0.25-1.5, `min_move_cents` 1-5, `max_orders_per_event` 1-4,
`size_cap_base` 2-10, `size_cap_max` 2-10 (>= base), `time_stop_us` 1000-20000. Hardware-limit names (R01-R15
constants in GATE_SPEC.md / golden.py) are refused with their rule id.

## Seeds

Live twin 0-59; held-out checks 60-79 only; 80+ refused before any scenario is built (`replay.guard_seeds`, plus
the evaluator's own guard). The out-of-sample split stays reachable only by `evaluator.py --final-eval`.

## Findings / open decisions (TODO(FINAL))

1. No Pack/Engine interface file existed; `packs/base.py` implements the signatures from the task verbatim.
2. 0x12 has no `hedge_band_shares` field, so it is not tunable by proposal (CM1).
3. Seeds 60-79 are the tuning's confirm set, which already picked the current ("recommended") config. At the full
   size (20 seeds x 4 regimes) no other top-K trial beats it there (baseline -7.55, best candidate -11.61), so the
   default demo run ends with `no_proposal`. Those seeds are not independent of the tuning selection.
4. `engine.orders_sent` counts every gate evaluation, rejected ones included; the twin's 0x20 `orders_sent` reports
   approved orders, as FRAMES.md defines it.
5. Twin limits: prefix replay (TW1-TW2), forecast is perfect foresight inside the twin (OP6), unmodeled 0x20 fields
   are sent as 0 and listed (TW4), rejected decisions are not in 0x21 (TW5). The engine arms ~100 ms into a session
   (2 heartbeats + snapshot).
6. Speed: a full supervisor run is ~20-30 s with 4 evaluator workers (`BACKEND_WORKERS`), ~68 s with 1.
7. `tools/fake_fpga.py` (a teammate's TCP twin speaking the same frames) appeared during this work; it is not wired
   in. A TCP data source would reuse the same decoding path.
