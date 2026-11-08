# UART frame payloads

Status: **PROPOSED**, except 0x10 (fixed by the project context) and 0x13 (already used by `fpga/io/io_shock.sv`, IO3). Everything market-related is SIMULATED.

Frame envelope (project context and `slowpath/frames.py`): sync `0x5A`, type (1 byte), length (1 byte = payload bytes), payload, then CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over type, length and payload, sent high byte first. Link: 1 Mbaud 8N1, which is 100 bytes/ms.

Conventions:
- Multi-byte fields are big-endian. Signed fields (`s`) are two's complement at the byte width shown.
- A value narrower than its bytes is right-aligned, and its unused top bits must be zero.
- **L->F** means laptop to FPGA; **F->L** means FPGA to laptop.
- A frame with the wrong length for its type is dropped like a bad CRC (frames.py F4) and counted in `frames_bad`.

## Summary

| Type | Name | Dir | Length | Sender | Reader(s) |
| --- | --- | --- | --- | --- | --- |
| 0x10 | snapshot | L->F | 95 | slowpath snapshot builder | snapshot loader (double-buffered RAM), gate R01/R02 |
| 0x11 | heartbeat | L->F | 4 | laptop agent, every 50 ms | frame decoder -> `arming.hb_valid`, gate `hb_last_us` (R14) |
| 0x12 | config | L->F | 10 | tuning / agent | trigger, move filter, exit logic (soft strategy parameters only) |
| 0x13 | load block | L->F | 2 + 2N (N = 1..126) | laptop demo setup | `io_shock` load port |
| 0x14 | hedge order | L->F | 8 | laptop agent (delta hedger) | gate (as `is_hedge = 1`), then the order path |
| 0x15 | kill | L->F | 0 | laptop agent / operator | frame decoder -> `arming.latch_req[4]` (R15) |
| 0x16 | control | L->F | 2 | laptop agent / dashboard | frame encoder (status and log scheduling) |
| 0x17 | feed word | L->F | 16 | simulator replay (`sim/options_model.py`) | parser (same input as the on-board feed) |
| 0x20 | status | F->L | 33 | FPGA status encoder | laptop dashboard, agent |
| 0x21 | order and fill log | F->L | 20 | FPGA log encoder (FIFO) | laptop dashboard, P&L / evaluator |

## 0x10 snapshot (95 bytes, fixed by the project context; reference)

| Offset | Field | Bytes | Width / scale |
| --- | --- | --- | --- |
| 0 | snapshot_id | 2 | u16 |
| 2 | ref_seq | 2 | u16, feed seq the snapshot was priced at (R02) |
| 4 | S_ref | 3 | u21 in 24 bits, cents Q4 |
| 7 + 11*c | theo_0[c] | 3 | u18, ticks Q8 |
| 10 + 11*c | delta_adj[c] | 3 | s18 in 24 bits (sign-extended), Q16 |
| 13 + 11*c | gamma_c[c] | 3 | u18, 2^-23 delta per cent |
| 16 + 11*c | vega[c] | 2 | u16, $ per contract per vol point Q8 |

c = 0..7 (contract = 4*is_put + strike index). Built by `slowpath/snapshot.py`.

## 0x11 heartbeat (4 bytes)

| Offset | Field | Bytes | Width / scale | Notes |
| --- | --- | --- | --- | --- |
| 0 | hb_seq | 2 | u16 | +1 per heartbeat, wraps |
| 2 | last_snapshot_id | 2 | u16 | id of the last 0x10 the laptop sent (echoed in status, debug only) |

- **Valid heartbeat** = CRC good, length 4, and `hb_seq` different from the previous valid one, so a replayed frame does not count.
- Each valid heartbeat pulses `hb_valid` for one cycle and latches `hb_last_us = now_us`.
- The laptop sends one every 50 ms, a 3x margin against the 150,000 us R14 limit.
- Arming needs 2 valid heartbeats (arming AR7).

## 0x12 config (10 bytes): soft strategy parameters only

The gate limits (R01-R15) are compile-time constants, and this frame cannot change them (project context: "nothing in software can change them").

| Offset | Field | Bytes | Width / scale | Default | Accepted range |
| --- | --- | --- | --- | --- | --- |
| 0 | edge_threshold | 2 | u16, ticks Q8 | 384 (1.5) | 256..768 (1.0..3.0) |
| 2 | exit_edge | 2 | u16, ticks Q8 | 128 (0.5) | 64..384 (0.25..1.5) |
| 4 | min_move | 1 | u8, cents (compared as `<< 4` against Q4 dS) | 2 | 1..5 |
| 5 | max_orders_per_event | 1 | u8 (trigger's limit, not the gate's 8) | 2 | 1..4 |
| 6 | size_cap_base | 1 | u8, contracts | 5 | 2..10 |
| 7 | size_cap_max | 1 | u8, contracts | 10 | size_cap_base..10 |
| 8 | time_stop_us | 2 | u16, us | 5000 | 1000..20000 |

- The accepted ranges equal the tuning search space (`slowpath/tuning/tune.py`).
- The edge-cap step stays `2 * edge_threshold` (golden A14).
- The frame is **applied only while armed = 0**, so parameters never change mid-session.
- If it arrives while armed, or if any field is out of range, the whole frame is ignored and counted in `config_rejects`. The reference model is `slowpath/frames.py` `ConfigRegister`.

## 0x13 load block (2 + 2N bytes; already implemented, io_shock IO3)

| Offset | Field | Bytes | Width / scale |
| --- | --- | --- | --- |
| 0 | start_addr | 2 | u14 in 16 bits |
| 2 + 2*i | data[i] | 2 | u16, written to `start_addr + i` |

Address map (io_shock IO3):

| Address | Contents |
| --- | --- |
| 0x0000-0x1FFF | shock table, addr = `{dir, size[7:0], contract[2:0], is_ask}` |
| 0x2000-0x2003 | underlying base bid / ask |
| 0x2004-0x2007 | underlying / option sizes |
| 0x2008 | stale_median_us |
| 0x2009 | slider_scale Q8 |

N = 1..126, so the length is at most 254.

## 0x14 hedge order (8 bytes)

| Offset | Field | Bytes | Width / scale | Notes |
| --- | --- | --- | --- | --- |
| 0 | client_id | 2 | u16 | laptop's tag, echoed in the 0x21 record |
| 2 | side | 1 | u1 in 8 bits | 0 buy, 1 sell |
| 3 | qty | 2 | u16, shares | the gate rejects > 1,000 (R07) |
| 5 | price | 3 | u18 in 24 bits, cents | limit price; R06 checks it against the microprice +/- 10 c |

- The gate sees `is_hedge = 1, contract = 0` and runs every check. The frame has no bypass and no reduce-only flag.
- Hedges share the one-in-flight order slot with trigger orders. Trigger orders win a same-cycle tie.
- The result is always reported as a 0x21 record.

## 0x15 kill (0 bytes)

No payload. It pulses `arming.latch_req[4]` (R15) and latches a trip. No frame can un-kill: clearing is KEY0 with ARM low only (arming AR11).

## 0x16 control (2 bytes)

| Offset | Field | Bytes | Notes |
| --- | --- | --- | --- |
| 0 | op | 1 | see below |
| 1 | arg | 1 | |

| op | Meaning | arg |
| --- | --- | --- |
| 0x01 | send one 0x20 status now | ignored |
| 0x02 | status period | arg x 10 ms, 0 = periodic status off (reset value 10, i.e. 100 ms) |
| 0x03 | order/fill log enable | bit 0: 1 = on (reset value 1) |

- Unknown ops are ignored and counted in `frames_bad`.
- Control has **no** op that arms, clears a latch, or changes a gate limit.

## 0x17 feed word (16 bytes, proposed in the project context)

| Offset | Field | Bytes | Notes |
| --- | --- | --- | --- |
| 0 | word | 16 | 128-bit message word, bit 127 first (byte 0 = bits 127..120) |

- The parser treats it exactly like a word from the on-board feed: it checks the CRC-8 and sequence, and drives R03.
- At 1 Mbaud a feed word frame takes 210 us. This is a replay/test path, not the latency path.

## 0x20 status (33 bytes)

| Offset | Field | Bytes | Width / scale |
| --- | --- | --- | --- |
| 0 | now_us | 4 | u32, FPGA microsecond counter |
| 4 | state | 1 | see the state bits below |
| 5 | trip_reason | 1 | first latched cause: 0, 3, 12, 13, 14, 15 |
| 6 | last_reject | 1 | reason code of the last rejected order (0 = none since reset) |
| 7 | tokens | 1 | R12 bucket, 0..20 |
| 8 | snapshot_id | 2 | u16, active snapshot |
| 10 | ref_seq | 2 | u16, active snapshot |
| 12 | feed_seq | 2 | u16, last good feed word |
| 14 | S | 3 | u21, cents Q4, current underlying |
| 17 | equity_x6400 | 6 | s48, $1/6400 (golden `gate_exposures`) |
| 23 | orders_sent | 2 | u16, approved orders, wraps |
| 25 | fills | 2 | u16, fill reports with qty > 0, wraps |
| 27 | last_latency_ns | 3 | u20, io_shock T0 -> first order (999,999 = saturated) |
| 30 | frames_bad | 1 | u8, saturating |
| 31 | config_rejects | 1 | u8, saturating |
| 32 | log_drops | 1 | u8, saturating (0x21 FIFO overflow) |

State bits:

| Bit | Meaning |
| --- | --- |
| 0 | armed |
| 1 | tripped |
| 2 | book_ready |
| 3 | snapshot_ok |
| 4 | hb_ok |
| 5 | shock_busy |
| 6 | ARM switch |
| 7 | kill pin |

The FPGA sends status every status period, and on op 0x01.

## 0x21 order and fill log (20 bytes, one record per frame)

| Offset | Field | Bytes | Width / scale | Notes |
| --- | --- | --- | --- | --- |
| 0 | rec_type | 1 | u8 | 1 = gate decision, 2 = fill report |
| 1 | ts_us | 4 | u32 | now_us at the decision / fill |
| 5 | instr | 1 | bit 7 is_hedge, bit 4 side, bits 2..0 contract | |
| 6 | qty | 2 | u16 | ordered qty (decision) or filled qty (fill, may be 0) |
| 8 | price | 3 | u18 in 24 bits | option ticks, or cents for a hedge |
| 11 | reason | 1 | u4 in 8 bits | gate reason code, 0 = approved (fills: 0) |
| 12 | flags | 1 | see the flag bits below | |
| 13 | fail_mask | 2 | u15 in 16 bits | gate `fail_mask` (fills: 0) |
| 15 | client_id | 2 | u16 | 0x14 tag for hedges from the laptop, else 0 |
| 17 | tokens | 1 | u8 | bucket level after this decision |
| 18 | orders_in_event | 1 | u8 | after this decision |
| 19 | reserved | 1 | 0 | |

Flag bits:

| Bit | Meaning |
| --- | --- |
| 0 | approved |
| 1 | reduce_only |
| 2 | blocked (not armed or tripped) |
| 3 | source = 0x14 frame (else trigger) |

- The FPGA writes one record per gate decision (approved or rejected) and one per fill-report word.
- Records go through a 32-deep FIFO. At 25 bytes per frame (250 us), a burst of more than 32 decisions inside about 8 ms drops records and counts `log_drops`. Trading never waits on the log.

## Assumptions

| Id | Assumption |
| --- | --- |
| FR1 | Every layout above is PROPOSED, except 0x10, which comes from the project context. 0x13 matches the existing RTL. |
| FR2 | `slowpath/frames.py` enforces every length in this file (F3). `encode()` raises `ValueError`, and the decoder drops the frame like a bad CRC. Field ranges are checked only for 0x12 (`decode_config`). |
| FR3 | The heartbeat period (50 ms), status period (100 ms) and log FIFO depth (32) are proposals, not spec numbers. |
| FR4 | Config is ignored while armed. That way a strategy-parameter change needs a disarm and re-arm, like every other state change on the board. |
