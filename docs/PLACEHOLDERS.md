# Placeholders (append only, never rewrite)

Every line is an unconfirmed value marked TODO(FINAL) at its point of use. SIMULATED arena.
Format: date | item | value | where used | note.

2026-11-08 | UNDERLYING_SYMBOL | "SPY" | golden/golden.py, backend/config.py | universe switch from the legacy symbol
2026-11-08 | SPOT | 733.50 $ | golden/golden.py SPOT, SPOT_CENTS 73350 | confirm against a live quote later
2026-11-08 | vol (smile a) | 0.17 | golden/golden.py VOL_BASE / SMILE_A |
2026-11-08 | dividend yield | 0.011 | golden/golden.py DIV |
2026-11-08 | rate | 0.039 | golden/golden.py RATE |
2026-11-08 | smile b, c | -0.3, 2.0 | golden/golden.py SMILE_B, SMILE_C |
2026-11-08 | vol lambda | 1.0 | golden/golden.py VOL_LAMBDA | unchanged
2026-11-08 | expiry | 5 days, T = 5/365 | golden/golden.py EXPIRY_DAYS, T_YEARS |
2026-11-08 | contracts | 4 strikes, $1 apart, calls + puts = 8 | golden/golden.py N_STRIKES, STRIKE_STEP |
2026-11-08 | strike recentering (A22) | nearest strike (round half up) - 2 .. +1; 732-735 at 733.50 | golden/golden.py strikes_for, build_snapshot | tie rule and offset are an assumption
2026-11-08 | MM half-spread | 1 tick | golden/golden.py MM_HALF_SPREAD_TICKS | sim/options_model.py still uses its own 2 (not owned here)
2026-11-08 | quote sizes | 20-200 | golden/golden.py QUOTE_SIZE_MIN/MAX | sim/options_model.py still uses 5-50 (not owned here)
2026-11-08 | edge threshold | 1.5 ticks (384), search 1.0-3.0 | golden/golden.py EDGE_THRESHOLD_TICKS, EDGE_THRESHOLD_RANGE |
2026-11-08 | min_move | 2 cents, search 1-5 | golden/golden.py MIN_MOVE_CENTS, MIN_MOVE_RANGE | stays in cents
2026-11-08 | size cap | base 5, max 10 | golden/golden.py SIZE_CAP_BASE/MAX | unchanged
2026-11-08 | max orders per event | 2 | golden/golden.py MAX_ORDERS_PER_EVENT | unchanged
2026-11-08 | hedge band | tunable 200-1000 shares, default unset (None) | golden/golden.py HEDGE_BAND_SHARES(_RANGE) | no default given
2026-11-08 | spot-vol beta | 0 | golden/golden.py VOL_BETA |
2026-11-08 | fees | 0.5 tick per contract | golden/golden.py FEE_TICKS_PER_CONTRACT |
2026-11-08 | R04 jump limit | 370 cents (raw 5920) | golden/golden.py JUMP_LIMIT_CENTS, fpga/gate/gate.sv JUMP_LIMIT_RAW | recomputed by error_curve.py: lambda 0 -> 415 c, lambda 1 -> 370 c, tighter adopted
2026-11-08 | R11 net vega | $1,500 per vol point (raw 384,000) | golden/golden.py NET_VEGA_LIMIT, fpga/gate/gate.sv R11 | scaled from $300; per-contract vega rose 5.06x
2026-11-08 | R09 net delta | 20.00 (hedge qty spy_shares) | golden/golden.py NET_DELTA_LIMIT | unchanged
2026-11-08 | R10 net gamma | 0.03 | golden/golden.py NET_GAMMA_LIMIT_X100 | unchanged; binds ~2.4x later on SPY (measured 2.39x), effectively looser
2026-11-08 | scheduled stress | +40 c single jump; 5 steps of -150 c | golden/golden.py STRESS_* | unchanged in dollars
2026-11-08 | buzzer pin, polarity | IO5, active-low | backend/config.py PINS | proposed only, not in any pin file
2026-11-08 | relay pin, polarity | IO6, active-low | backend/config.py PINS | proposed only
2026-11-08 | shock button pin, polarity | IO3, active-low | backend/config.py PINS | proposed only
2026-11-08 | kill button pin, polarity | IO4, active-low | backend/config.py PINS | proposed only
2026-11-08 | ARM_SWITCH | SW9 rising edge after boot | backend/config.py ARM_SWITCH |
2026-11-08 | tool names | unset () | backend/config.py TOOL_NAMES | not chosen
2026-11-08 | Nemotron endpoint | unset (None) | backend/config.py NEMOTRON_ENDPOINT | not configured
2026-11-08 | Nemotron model | unset (None) | backend/config.py NEMOTRON_MODEL | not chosen
2026-11-08 | sim random step | 0.06 bps per tick (0.44 c at 733.50, asserted) | sim/options_model.py step_sigma_bps | P0b
2026-11-08 | sim random jumps | U(1, 12) bps (7.3-88.0 c at 733.50), asserted < R04 | sim/options_model.py jump_bps_min/max | P0b; shape unchanged, range rescaled
2026-11-08 | sim strike grid | golden strikes_for(session S_ref) | sim/options_model.py | P0b; session grid, no intra-session recentering
2026-11-08 | engine order latency | 10 us twin timing (send -> arrival) | golden/engine.py DEFAULTS order_latency_us | P1, E4; not FPGA latency
2026-11-08 | engine exit policy | exit when entry-side edge <= exit_edge_ticks (engine default 0.0) or held >= time_stop_us (5000) | golden/engine.py _exits | P1, E10; tune trial 0 uses exit_edge 0.5 (T1)
2026-11-08 | engine max hold | time_stop_us 5000 (search 1000-20000) | golden/engine.py, tune.py | P1
2026-11-08 | engine snapshot timing | first at book-ready, then every 100,000 us, commit delay 0 | golden/engine.py E3 | P1; R02 then open ~11 ms per snapshot
2026-11-08 | engine heartbeats | every 50,000 us from t = 0, always valid | golden/engine.py E14 | P1
2026-11-08 | engine hedge fills | underlying fills in full at the limit if the touch allows | golden/engine.py E8 | P1
2026-11-08 | engine flatten on trip/kill | liquidate at last sane touch, full size, outside the gate | golden/engine.py E12 | P1
2026-11-08 | engine arming | ARM raised as soon as the arming conditions hold | golden/engine.py E13 | P1
2026-11-08 | expected trip regimes | feed_fault (R03), rogue_burst (R12) | slowpath/tuning/tune.py EXPECTED_TRIP_REGIMES | P1, T7
2026-11-08 | tuning sizes | TPE 20 trials, random 10, top-K 5 | slowpath/tuning/tune.py | P1, T8
2026-11-08 | twin TCP port | 127.0.0.1:47420 | tools/fake_fpga.py DEFAULT_PORT | twin TW1
2026-11-08 | twin status period | 100 ms twin time (FRAMES.md reset value; op 0x02 changes it) | tools/fake_fpga.py STATUS_PERIOD_US | twin TW2
2026-11-08 | twin live pacing | --speed 0.01 (600 ms session = 60 s wall) | tools/fake_fpga.py | twin TW8
2026-11-08 | twin arm input | stdin "a" = SW9 rising edge, "d" = SW9 low, "c" = KEY0 clear | tools/fake_fpga.py ARM_SWITCH | twin; matches backend/config.py ARM_SWITCH
2026-11-08 | twin kill-frame clear | KILL frame not clearable in the twin session (stricter than board) | tools/fake_fpga.py | twin TW7
2026-11-08 | disaster world | 5,000 households, 12 zones (3x4 grid), 30 crews, depot zone 5; attribute and need model DW2-DW3 | backend/packs/disaster/world.py | disaster pack, all synthetic
2026-11-08 | disaster silent share | 15% of top-10%-need households never report; 2 dark zones (report x0.30) | backend/packs/disaster/world.py SILENT_SHARE, N_DARK_ZONES | disaster pack
2026-11-08 | disaster report model | report p by tier .95/.80/.35/.10, delays mean 60/120 min, stated severity accuracy .85 | backend/packs/disaster/world.py DW6-DW7 | disaster pack
2026-11-08 | disaster service times | travel 6 + 4 x grid distance min, on-site 15/20/25/30 min by tier | backend/packs/disaster/world.py DW8 | disaster pack
2026-11-08 | disaster care-rule limits | C2 starvation 8 h, C4 zone cap 5 crews, crew working cap 16 h, C5 heartbeat timeout 300 s | backend/packs/disaster/pack.py | disaster pack
2026-11-08 | disaster horizon / overlooked | 24 h horizon, 60 s ticks; overlooked = top-10% not contacted after 6 h | backend/packs/disaster/pack.py | disaster pack
2026-11-08 | disaster channels | overlooked_high_need 10/25/50, p90_ttfc_top10_min 120/240/360, max_wait_min, agent_heartbeat_age_s, crew_idle_frac | backend/packs/disaster/pack.py limits() | disaster pack; names TODO(FINAL)
2026-11-08 | disaster agents | risk weights DA1, triage score DA2 (welfare 25 + risk), zone flag suspicion 0.3, auditor 2 x 75 at 2 h / 4 h | backend/packs/disaster/agents.py | disaster pack
2026-11-08 | adapter action steps | raise_edge +0.5 tick, raise_min_move +1 cent, size_down base -1 / max -2 (clipped to 0x12 ranges) | latency_arena_adapter/app/adapters/trading.py TA3 | P3b
2026-11-08 | adapter raise_delta demo value | 2 x NET_DELTA_LIMIT = 40 (always refused) | latency_arena_adapter/app/adapters/trading.py DEMO_DELTA, TA4 | P3b; DEMO_REFUSAL_ACTION default "true"
2026-11-08 | adapter simulate regimes / workers | normal + feed_fault, 4 processes | latency_arena_adapter/app/adapters/trading.py DEFAULT_REGIMES, DEFAULT_WORKERS | P3b; env LA_ADAPTER_REGIMES, LA_ADAPTER_WORKERS
2026-11-08 | adapter simulate time budget | ~10 s per simulate() call | latency_arena_adapter/app/adapters/trading.py | P3b; measured ~7 s (4 workers, 80 seeds x 2 regimes)
2026-11-08 | adapter exposure rule | size_cap_base / size_cap_max increases refused; max_orders_per_event not counted | latency_arena_adapter/app/adapters/trading.py TA6 | P3b
2026-11-08 | adapter demo operator | inject() acts as human:demo_operator | latency_arena_adapter/app/adapters/trading.py OPERATOR | P3b
2026-11-08 | adapter audit path | latency_arena_adapter/audit_trading.jsonl (env LA_ADAPTER_AUDIT_LOG) | latency_arena_adapter/app/adapters/trading.py | P3b
