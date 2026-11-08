# Latency Arena: project context for Claude Code

One project, one presentation. A DE10-Lite FPGA reprices EOG Resources (NYSE: EOG) stock options in microseconds under a hardware risk gate. A laptop does the slow work (pricing snapshots, simulator, agent). Everything market-related is SIMULATED and must be labeled simulated.

## Rules for you
- Do not invent numbers or formats. If the spec below is ambiguous, ask me, or write the assumption at the top of the file and list it in your summary.
- The Python golden model is the reference. The FPGA must match it bit for bit.
- Deterministic everything: fixed seeds, no wall-clock in logic, no network calls.
- Never read or print .env or any key. Never run git push. Do not edit folders outside the one named in the task.
- Use pytest for Python. For SystemVerilog, write Questa testbenches, synthesizable RTL for Quartus Prime Lite (device 10M50DAF484C7G, 50 MHz), and no latches.
- Every task ends with the exact commands to run the tests.

## Units and formats
- tick = $0.01 of option price. One tick = $1 per contract (multiplier 100).
- S, S_ref: unsigned 21 bits, 4 fraction bits, in cents (1/16 cent).
- dS = S - S_ref: signed 16 bits, 4 fraction bits, in cents, saturating at +/-1023.9 cents.
- theo_0, theo_new: unsigned 18 bits, 8 fraction bits, in ticks.
- delta_adj: signed 18 bits, 16 fraction bits.
- gamma_c: unsigned 18 bits, step 2^-23 per cent of spot (gamma per $1 divided by 100, times 2^23).
- vega: PROPOSED unsigned 16 bits, 8 fraction bits, in dollars per contract per vol point.
- Rounding: round half up via (x + (1 << (k-1))) >> k. Python >> is arithmetic on negatives.

## Golden repricing (bit-exact reference)
```python
def rshift_round(x, k): return (x + (1 << (k - 1))) >> k
def sat(x, lo, hi): return max(lo, min(hi, x))
def reprice(theo0, d_adj, gam, dS):
    p1 = rshift_round(d_adj * dS, 12)                                   # ticks, 8 fraction bits
    g1 = sat(rshift_round(gam * dS, 11), -(1 << 17), (1 << 17) - 1)     # delta change, 16 fraction bits
    p2 = rshift_round(g1 * dS, 13)                                      # includes the 1/2 of the gamma term
    return sat(theo0 + p1 + p2, 0, (1 << 18) - 1)
```
Microprice (size-weighted), recip[t] = round(2^20 / t) for t = 1..8191 (13-bit ROM address, 20-bit entries; recip[1] saturates to 2^20 - 1, still exact). Two 12-bit sizes sum to at most 8,190, so no halving happens. Error vs exact <= 0.035 cent at spreads 1-2 (target 0.06), <= 0.06 up to spread 14, 0.070 at spread 20. (Changed from the 16-bit round(65536 / t) table, which reached 0.09 cent at spread 2.)
```python
def microprice_q4(bid_c, ask_c, bsz, asz, recip):
    while bsz + asz > 8191: bsz >>= 1; asz >>= 1           # never runs for 12-bit sizes
    tot = bsz + asz
    frac = ((ask_c - bid_c) * bsz) * recip[tot]
    return (bid_c << 4) + ((frac + (1 << 15)) >> 16)       # cents, 4 fraction bits
```
Edge: edge_buy = theo_new - (ask << 8); edge_sell = (bid << 8) - theo_new. Fire when edge > edge_threshold (1.5 ticks = 384) and the quote is sane, not locked out, and has displayed size.

## Message word (128 bits, big-endian bit numbering)
| Bits | Field | Width | Notes |
| --- | --- | --- | --- |
| 127..120 | type | 8 | 1 underlying quote, 2 option quote, 3 buy fill report, 4 sell fill report, 5 session control |
| 119..104 | seq | 16 | wraps at 65,536 |
| 103..72 | ts_us | 32 | microseconds from session start |
| 71..68 | id | 4 | 0..7 option contract, 8 underlying |
| 67..50 | bid | 18 | option ticks or stock cents; fill price in a fill report |
| 49..32 | ask | 18 | same units; filled quantity in a fill report |
| 31..20 | bid_size | 12 | |
| 19..8 | ask_size | 12 | |
| 7..0 | crc8 | 8 | PROPOSED CRC-8 poly 0x07, init 0, over the other 15 bytes |

## UART frames (1 Mbaud, 8N1)
sync 0x5A, type, length, payload, CRC-16-CCITT (PROPOSED poly 0x1021, init 0xFFFF) over type, length and payload.
Types: 0x10 snapshot, 0x11 heartbeat, 0x12 config, 0x13 load block, 0x14 hedge order, 0x15 kill, 0x16 control, 0x17 feed word (proposed), 0x20 status, 0x21 order and fill log.
Snapshot payload (95 bytes): snapshot_id (2), ref_seq (2), S_ref (3), then 8 contracts of theo_0 (3), delta_adj (3), gamma_c (3), vega (2).

## Universe and defaults (EOG options, placeholders to recalibrate)
- 8 contracts: index = 4 * (1 if put) + strike index. Strikes 146, 147, 148, 149 (calls 0..3, puts 4..7). Spot 147.13. Base vol 35%. Rate 3.9%. No dividend in the window. T = 5/365.
- Smile sigma(m) = a + b*m + c*m^2 with m = ln(K / S_ref): a = 0.35, b = -0.3, c = 2.0 (guesses). Vol rule: dsigma_dS = -lambda*(b + 2*c*m)/S_ref + beta (lambda = 1, beta = 0). delta_adj = delta + (vega_per_share_per_point * 100) * dsigma_dS.
- Put delta is the call delta minus exp(-qT), not minus 1.
- Parameters: min_move 2 cents; edge_threshold_ticks 1.5 (0.5 fee + 0.5 model + 0.5 adverse); model_margin_ticks 0.5; jump_limit $1.85 (from the error curve: the tighter of lambda 0 / lambda 1, one constant for both); max_snapshot_age 250 ms; snapshot interval 100 ms; max_orders_per_event 2; size_cap_base/max 5/10; time_stop 5,000 us.

## Risk gate (15 checks, lowest failing number sets the reason code, 0 = pass)
| Code | Check | Rule (blocks when) | Class |
| --- | --- | --- | --- |
| R01 | Stale snapshot | now - snapshot flip time > 250,000 us | State block |
| R02 | Snapshot sync | ref_seq ahead of the feed seq, or more than 64 behind (mod 65,536) | State block |
| R03 | Feed integrity | sequence gap, repeat or bad CRC since the last resync | Latching trip |
| R04 | Jump guard | abs(S - S_ref) > jump_limit | State block, clears on a new snapshot |
| R05 | Quote sanity | bid > 0, ask > bid, spread <= 20 ticks, sizes > 0 | Order reject |
| R06 | Price band | abs(order price - theo_0) > 10 ticks (hedges: 10 cents from microprice) | Order reject |
| R07 | Max order size | qty > 10 contracts (hedges: > 1,000 shares) | Order reject |
| R08 | Max position | abs(confirmed + pending + signed qty) > 20 per contract | Order reject |
| R09 | Net delta | abs(sum(pos*delta_adj) + spy_shares/100) > 20.00 after the fill | Order reject |
| R10 | Net gamma | abs(sum(pos*gamma_c)) > 0.03 after the fill (300 shares per $1) | Order reject |
| R11 | Net vega | abs(sum(pos*vega)) > $300 per vol point after the fill | Order reject |
| R12 | Order rate | more than 8 orders in one event, or the token bucket (20 burst, refills 1 per 5,000 us) is empty | Latching trip |
| R13 | Drawdown | equity = cash + sum(pos_conf*theo_new) + shares*S/100 below -$2,000 | Latching trip |
| R14 | Heartbeat | no valid heartbeat for 150,000 us | Latching trip |
| R15 | Manual kill | kill pin high, or a KILL frame | Latching trip |
Reduce-only orders (the gate computes this from the sign of the position change, never from a sender flag) skip R01-R04, R06, R08-R11, R13, R14, and still pass R05, R07, R12, R15. Limits are compile-time constants; nothing in software can change them.

## Arming
At power-up every order is rejected. Trading starts only when: book ready (all 9 instruments seen), a snapshot is committed and passes R01 and R02, at least 2 valid heartbeats, no trip latched, and SW9 (ARM) rises from low to high after those hold. Powering on with ARM already up does nothing. Clear a latch with KEY0 while ARM is low.