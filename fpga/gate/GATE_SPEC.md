# Risk gate: spec

Status: **PROPOSAL, UNVERIFIED.** RTL exists (`gate.sv`) and a self-checking testbench exists
(`sim/tb_gate.sv`), but the testbench has **not been run** (Questa license failed on 2026-11-08,
see section 10). No rule is marked verified until `sim/run_tb_gate.ps1` prints `TB PASS`.
Reference model: `golden/golden.py` `gate_eval()` (assumptions A16-A21). Vectors: `docs/vectors/gate.csv`.
Target: Quartus Prime Lite, 10M50DAF484C7G, one 50 MHz clock, no latches, no initial blocks.

## 1. Structure

```
              +------------------------------- gate (top) ---------------------------------+
order_valid ->|  gate_core (pure checks, 3-stage pipe)        token bucket   event counter |-> approve, reject
order fields->|  15 fail bits -> reduce-only mask -> priority    (R12)          (R12)       |-> reason_code[3:0]
state inputs->|                                                                             |-> latch_req[4:0]
              +-----------------------------------------------------------------------------+
```

- `gate_core`: every input is a port, no state except pipeline registers. `tb_gate` drives it row by row from `gate.csv`. `tokens` and `orders_in_event` are inputs here.
- `gate` (top): wraps `gate_core` and owns the R12 state (token bucket, orders-in-event counter) and the latch request outputs. The sequential tests (20 orders in 1 ms) run on the top.
- Out of scope, but needed upstream: a **position keeper** that maintains the precomputed sums (section 5) and per-contract positions.

## 2. Ports

### 2.1 Clock, control

| Port | Dir | Width | Notes |
| --- | --- | --- | --- |
| `clk` | in | 1 | 50 MHz |
| `rst_n` | in | 1 | synchronous active-low reset (same style as `fpga/arming`) |
| `tick_1us` | in | 1 | 1-cycle pulse every 1 us (token refill) |
| `now_us` | in | 32 | free-running microsecond counter |
| `event_start` | in | 1 | pulse at the start of each market-data event; clears `orders_in_event` |
| `armed` | in | 1 | from `arming` |
| `tripped` | in | 1 | from `arming` (any latch held) |

### 2.2 Order request (one in flight, valid/ready)

| Port | Width | Notes |
| --- | --- | --- |
| `order_valid` | 1 | request |
| `order_ready` | 1 (out) | high when no order is in the pipe and the sums have settled (section 6) |
| `is_hedge` | 1 | 1 = underlying shares |
| `contract` | 3 | 0..7 (ignored for hedges) |
| `side` | 1 | 0 buy, 1 sell |
| `qty` | 16 | contracts, or shares for a hedge |
| `price` | 18 | option ticks, or cents for a hedge |

There is **no** reduce-only input. The gate derives it (section 4).

### 2.3 State inputs (registers held elsewhere, read combinationally)

| Port | Width | Source | Used by |
| --- | --- | --- | --- |
| `snap_flip_us` | 32 | snapshot loader | R01 |
| `ref_seq`, `feed_seq` | 16, 16 | snapshot loader, parser | R02 |
| `feed_bad` | 1 | parser (gap, repeat or bad CRC since resync) | R03 |
| `S`, `S_ref` | 21, 21 | book, snapshot | R04, R13 |
| `q_bid`, `q_ask`, `q_bsz`, `q_asz` | 18, 18, 12, 12 | book: quote of the ordered instrument | R05 |
| `mp_q4` | 24 | microprice block (underlying) | R06 hedges |
| `theo_0_c` | 18 | snapshot RAM, muxed by `contract` | R06 options |
| `delta_adj_c`, `gamma_c_c`, `vega_c` | 18s, 18, 16 | snapshot RAM, muxed by `contract` | R09-R11 |
| `pos_c` | 9s | position keeper: conf + pend of `contract` | R08, reduce-only |
| `sh` | 17s | position keeper: hedge shares conf + pend | R09, reduce-only |
| `sum_delta` | 32s | position keeper: sum(pos * delta_adj), Q16 | R09 |
| `sum_gamma` | 32s | sum(pos * gamma_c) | R10 |
| `sum_vega` | 32s | sum(pos * vega), Q8 | R11 |
| `equity_x6400` | 48s | mark-to-market block: 64*cash_cents + 25*sum(pos_conf*theo_new) + 4*sh_conf*S | R13 |
| `hb_last_us` | 32 | heartbeat receiver | R14 |
| `kill` | 1 | kill pin (synchronized) OR KILL frame seen | R15 |
| `tokens`, `orders_in_event` | 5, 4 | **gate_core only**; in the top these come from the gate's own registers | R12 |

The `gate.csv` columns map 1:1 onto these. The `*_c` ports are picked from the 8-wide arrays by `contract`; `pos_c` = `pos_conf[c] + pos_pend[c]`. The derived columns (`sum_*`, `sh`, `equity_x6400`, `mp_q4`) are in the file, computed by golden `gate_exposures()`.

### 2.4 Outputs

| Port | Width | Notes |
| --- | --- | --- |
| `result_valid` | 1 | 3 cycles after `order_valid & order_ready` |
| `approve` | 1 | `result_valid & armed & ~tripped & reason_code == 0` |
| `reason_code` | 4 | lowest failing check after exemptions, 0 = all checks pass |
| `fail_mask` | 15 | bit k-1 = check k failed (debug / log frame; matches the `gate.csv` column) |
| `reduce_only` | 1 | debug: what the gate decided |
| `blocked` | 1 | `~armed | tripped`; the order was rejected regardless of the checks |
| `latch_req` | 5 | to `arming.latch_req` = {R15 KILL frame, R14, R13, R12, R03}; see section 7 |

## 3. Checks and priority

All 15 conditions are evaluated in parallel from the registered inputs. Then the reduce-only exemptions are masked, and a priority encoder picks the **lowest** failing number.

| Code | Fails when (exact integer form, A16-A21) |
| --- | --- |
| R01 | `(now_us - snap_flip_us) mod 2^32 > 250_000` |
| R02 | `(feed_seq - ref_seq) mod 2^16 > 64` (covers "ahead", which wraps to >= 65,472) |
| R03 | `feed_bad` |
| R04 | `abs(S - S_ref) > 5920` (jump_limit 370 cents = golden `JUMP_LIMIT_CENTS` x 16; TODO(FINAL)) |
| R05 | `!(q_bid > 0 && q_ask > q_bid && q_ask - q_bid <= 20 && q_bsz > 0 && q_asz > 0)` |
| R06 | option: `abs((price << 8) - theo_0_c) > 2560`; hedge: `abs((price << 4) - mp_q4) > 160` |
| R07 | `qty > 10` (hedge: `> 1000`) |
| R08 | option only: `abs(pos_c + sq) > 20` |
| R09 | `abs(100 * (sum_delta + oq * delta_adj_c) + (sh + hq) * 65536) > 131_072_000` |
| R10 | `abs(100 * (sum_gamma + oq * gamma_c_c)) > 25_165_824` (3 * 2^23) |
| R11 | `abs(sum_vega + oq * vega_c) > 384_000` (golden `NET_VEGA_LIMIT` 1500 x 256; TODO(FINAL)) |
| R12 | `orders_in_event + 1 > 8 || tokens == 0` |
| R13 | `equity_x6400 < -12_800_000` |
| R14 | `(now_us - hb_last_us) mod 2^32 > 150_000` |
| R15 | `kill` |

Here `sq` is +qty for a buy and -qty for a sell. `oq` = `sq` for options and 0 for hedges; `hq` = `sq` for hedges and 0 for options.

## 4. Reduce-only (computed inside the gate)

```
p      = is_hedge ? sh : pos_c                 // confirmed + pending, before this order
reduce = (p != 0) && (sign(sq) != sign(p)) && (qty <= |p|)
```

- An order that crosses through zero (|qty| > |p|) is a **normal** order. So is one that adds to the position.
- When `reduce` is set, R01, R02, R03, R04, R06, R08, R09, R10, R11, R13 and R14 are masked. R05, R07, R12 and R15 still apply.
- No sender flag exists, so a "reduce-only" order that adds risk cannot get past the gate.

## 5. Exposure math (one multiply per exposure)

The position keeper holds `sum_delta`, `sum_gamma` and `sum_vega` over the current positions (conf + pend). The gate adds only the candidate order's contribution:

| Exposure | Multiply in the gate | Size |
| --- | --- | --- |
| delta | `oq * delta_adj_c` | 17s x 18s |
| gamma | `oq * gamma_c_c` | 17s x 18u |
| vega | `oq * vega_c` | 17s x 16u |

The x100 and x65536 constant scalings are shift-adds, not multipliers. That is 3 embedded 18x18 multipliers out of the 144 on the 10M50. R13 uses `equity_x6400` as an input, because it depends on every `theo_new` and is re-accumulated by the mark-to-market block after each reprice (8 MACs).

## 6. Pipeline and latency (target: 3 cycles, 60 ns)

| Stage | Work |
| --- | --- |
| S1 (input regs) | register the order and state; time and sequence differences; `sq`; the three products; price-band differences |
| S2 | sums with the precomputed exposures; abs; all 15 compares; reduce-only |
| S3 (output regs) | exemption mask, priority encode, `approve`, `latch_req` |

- **One order in flight.** `order_ready` drops when an order is accepted. It rises again once the position keeper has added the approved order to pending and its sums, which we budget at 1 cycle after `result_valid`. Two orders from one event therefore take about 8 cycles (160 ns). Without this serialization, the second order would be checked against stale sums.
- The longest path is S2: a 32-bit add, x100 shift-add and 34-bit compare. That should fit in 20 ns on a -C7 device; if timing fails, the multiplies move into an extra stage (4 cycles).

## 7. R12 state and latch requests (gate top)

- **Token bucket:** 5-bit, resets to 20 and saturates at 20. It refills 1 token every 5,000 `tick_1us` and consumes 1 per **approved** order (rejected orders don't drain it).
- **Event counter:** 4-bit. `event_start` clears it and each approved order increments it.
- Twenty orders inside 1 ms with no refill all pass. The 21st fails R12 with `tokens == 0`.
- **`latch_req`** (one-cycle pulses, held by `arming`):
  - R03: raised when `feed_bad` is seen, even with no order in flight.
  - R12: raised when an order fails R12.
  - R13: raised when the equity condition holds, even with no order in flight.
  - R14 and R15 are generated inside `arming` (heartbeat watchdog, kill pin). The gate drives `latch_req[4]` from a KILL frame only and `latch_req[3]` = 0.

## 8. Assumptions (in addition to golden A16-A21)

- **G1.** The gate checks the order's own instrument quote (`q_*`) for R05 and uses `theo_0` (not `theo_new`) for R06, as CLAUDE.md says.
- **G2.** `approve` also needs `armed & ~tripped`. Blocked orders still report the checks' `reason_code`, with `blocked = 1`.
- **G3.** Hedge orders have no position limit (R08 does not apply), since CLAUDE.md gives none for shares.
- **G4.** Tokens are consumed and the event counter incremented only on approval.
- **G5.** One order in flight (section 6). This needs the position keeper's sums to settle within one cycle of `result_valid`.
- **G6.** The latch outputs are requests; `arming` stores the latch and the first cause.

## 9. Open questions for review

1. **Q1. jump_limit:** answered by golden A3: `JUMP_LIMIT_CENTS = 370` (5920 raw), still TODO(FINAL). `gate.sv` and `gate.csv` both use 5920.
2. **Q2.** Is R05's spread limit of 20 the right rule for the underlying quote (cents) in hedge checks?
3. **Q3.** Should R12 also count rejected attempts against the token bucket (so a rogue burst drains it faster)?
4. **Q4.** Should R13 be latched continuously by the mark-to-market block instead of the gate?
5. **Q5.** Should the 8-orders-per-event limit include hedges? (Currently yes: any approved order counts.)
6. **Q6.** Is a 4-cycle fallback acceptable if S2 misses timing?

## 10. Verification status (updated 2026-11-08)

Testbench: `sim/tb_gate.sv`, vectors `sim/gate_vectors.mem` made by `sim/gate_csv2mem.py` from
`docs/vectors/gate.csv` (1658 rows). Run: `powershell -ExecutionPolicy Bypass -File fpga\gate\sim\run_tb_gate.ps1`
(exit code 0 only on `TB PASS`).

**Simulation has not been run.** `vlog -lint` and `vopt` compile `gate.sv` + `tb_gate.sv` with
0 errors, but `vsim` failed: `License Issue: Invalid host (C:/Users/varis/questa_lic.dat)`. The
license is node-locked to NIC ID `e2fc20524153`, and that adapter is not present on this machine
right now (`tb_arming` ran on 2026-11-07, so it probably was then). No iverilog or verilator.

Supporting evidence only, **not** verification: a hand transliteration of `gate_core`'s RTL
equations into Python (same bit widths and wraps) matches all 1658 golden rows. Swapping in the
old R04 (2960) or R11 (76,800) limits makes it mismatch 467 and 729 rows. That checks the
equations as transcribed, not the HDL as a simulator executes it.

| Rule / behaviour | What tb_gate checks | Status |
| --- | --- | --- |
| R01-R15 (gate_core) | every golden row, 2 passes (back-to-back, random bubbles); trip, clear, boundary pair, two-at-once, reduce-only exempt / non-exempt / still-applies, cross-zero, reduce-to-zero; coverage gate fails the run on any hole | UNVERIFIED (not run) |
| R04 at 370 c | ±5919/5920 pass, ±5921 fail; old 185 c / 200 c values pass; reduce-only exempt; hedge | UNVERIFIED (not run) |
| Pipeline / order_ready (S6, G5) | result 3 cycles after accept; ready low 4 cycles | UNVERIFIED (not run) |
| approve / blocked (G2, G7) | armed=0 or tripped=1 block; reduce-only blocked while tripped | UNVERIFIED (not run) |
| Token bucket (S7, G4, G9) | 20 in < 1 ms pass, 21st R12; rejects don't drain; refill every 5000 ticks; saturates at 20; refill + consume same cycle nets out | UNVERIFIED (not run) |
| Orders in event (G10) | 8 pass, 9th R12; event_start clears; approve + event_start same cycle -> 1 | UNVERIFIED (not run) |
| R13 latch (G8) | not set at exactly -12,800,000; set at -12,800,001 with no order; persists after recovery (orders fail R13, reduce-only exempt); not cleared by armed toggle, event_start, kill_frame, tripped held or tripped falling while equity bad; cleared by tripped falling with equity ok; reset clears | UNVERIFIED (not run) |
| latch_req (S7, G11) | [0] follows feed_bad; [1] on R12 fail incl. lower reason; [2] = cond or latch; [3] always 0; [4] = kill_frame | UNVERIFIED (not run) |

Not covered by tb_gate: R03/R12/R14/R15 latch persistence (that lives in `arming`, see
`tb_arming.sv`); multi-cycle `SETTLE_CYCLES` values other than 1.

