"""Latency Arena golden model: bit-exact fast-path reference plus float Black-Scholes.

SIMULATED: every market number here (spot, smile, rate, strikes) is a placeholder
for a simulated SPY options universe ("simulated arena"), not live data. The universe
block below is the single source of truth; every value in it is TODO(FINAL).

The fixed-point functions (rshift_round, sat, reprice, microprice_q4, edges, recip
table) are the reference the FPGA must match bit for bit. They are copied from the
project context file and must not be "improved" here (the 20-bit recip, A10b, is a
decided spec change, mirrored in the context file).

ASSUMPTIONS (the spec was silent or ambiguous; change here if wrong):
  A1. The project context lives in README.md.txt; there is no CLAUDE.md file.
  A2. dS saturates symmetrically at +/-16383 raw (+/-1023.9375 cents), per the
      "+/-1023.9 cents" text, even though a signed 16-bit field could hold +/-2047.9.
  A3. jump_limit = one constant for both lambda modes: the tighter of the two error-curve
      proposals (golden/error_curve.py adopted_jump_limit()), recomputed at the current
      calibration and recorded as JUMP_LIMIT_CENTS (gate raw value = cents x 16).
  A4. to_fixed rounds half up, floor(x * 2^f + 0.5), then saturates to the
      format range. from_fixed is the exact inverse scale (raw / 2^f).
  A5. Snapshot pricing uses the dequantized 21-bit S_ref, so laptop and FPGA
      agree on the reference spot.
  A6. theo_0, gamma_c and vega come from plain Black-Scholes at sigma(m); only
      delta_adj carries the smile/vol-rule adjustment.
  A7. dsigma_dS is in vol (decimal) per $1 of spot; beta has the same units.
  A8. Theta per day = annualised theta / 365 (calendar days).
  A9. Firing: strict edge > threshold. "Has displayed size" means the size on the
      side being hit (ask size for a buy, bid size for a sell). locked_out is an input.
  A10. recip[0] = 0. It is never used when R05 requires both sizes > 0.
  A10b. (DECIDED, replaces the 16-bit table in the original spec) recip[t] = round(2^20 / t),
       t = 1..8191: 13-bit ROM address, 20-bit entries. recip[1] = 2^20 is saturated to
       2^20 - 1; the result is still exact (spread*(2^20-1) + 2^15 >> 16 = spread*16 for any
       spread < 2^15). Two 12-bit sizes sum to <= 8,190, so the halving loop never runs.
       Output = (bid << 4) + ((spread*bsz*recip[tot] + 2^15) >> 16). Exhaustive error vs
       exact: <= 0.0351 cent at spreads 1-2 (target 0.06); <= 0.06 up to spread 14;
       0.0699 cent worst at spread 20 (old 16-bit table: 0.632).

  Parser (message word):
  A11. CRC-8 (PROPOSED) is MSB-first, no reflection, no final XOR, over bits 127..8
       sent as 15 big-endian bytes.
  A12. One sequence counter covers every message type. The first CRC-good word after
       reset sets the reference (no gap check). A bad-CRC word is dropped: it reports
       no gap/dup and does not move the reference. seq == last is a duplicate (reference
       unchanged); any other seq != last + 1 (mod 65,536) is a gap and resyncs the
       reference to the new seq.

  Trigger:
  A13. Edge index e = 2 * contract + side (side 0 = buy at ask, 1 = sell at bid);
       ties on equal edge go to the lowest e, i.e. the lowest contract.
  A14. edge cap (PROPOSED): size_cap_base (5) when edge <= 2 * edge_threshold (3 ticks),
       else size_cap_max (10).
  A15. lockout is a 16-bit mask indexed by e. pos = confirmed + pending; room is
       20 - pos for a buy, 20 + pos for a sell, floored at 0. A candidate whose size
       comes out 0 is dropped before ranking and does not use an order slot.

  Gate:
  A16. Fixed-point limits are exact integer forms of the CLAUDE.md rules (no rounding):
       R09 |100*sum(pos*delta_adj) + shares*65536| > 2000*65536,
       R10 |100*sum(pos*gamma_c)| > 3*2^23, R11 |sum(pos*vega)| > NET_VEGA_LIMIT*256,
       R13 64*cash_cents + 25*sum(pos_conf*theo_new) + 4*shares_conf*S < -2000*6400.
  A17. "pos" in R08-R11 is confirmed + pending + this order; "spy_shares" in R09 is
       hedge shares confirmed + pending (+ this order if it is a hedge). R13 uses
       confirmed positions and confirmed shares only. cash is in cents.
  A18. R12: orders_in_event counts orders already sent this event; this order fails if
       it would be the 9th, or if tokens == 0. The gate is evaluated combinationally:
       latching (R03, R12-R15) and the token refill live outside these functions.
  A19. Hedge orders: R05 applies the same rule to the underlying quote (spread <= 20 cents),
       R06 compares (price << 4) to microprice_q4 of that quote, R07 limit 1,000 shares,
       R08 does not apply (no share position limit in the spec).
  A20. Reduce-only: position before (confirmed + pending) is nonzero, the order has the
       opposite sign, and |qty| <= |position|. An order that crosses through zero is not
       reduce-only.
  A21. Time differences are mod 2^32 (now - t), sequence differences mod 2^16.

  Universe:
  A22. (TODO(FINAL)) Strikes recenter on every snapshot: center = nearest $1 strike to the
       dequantized S_ref, rounding half up; strikes = center-2 .. center+1. At 733.50 that is
       732..735. build_snapshot(strikes=None) applies it; pass strikes to pin a grid.
"""

import math
from dataclasses import dataclass

# ---------------------------------------------------------------- universe (SINGLE SOURCE OF TRUTH)
# SIMULATED SPY universe. Every value in this block is unconfirmed: TODO(FINAL) on each line,
# all listed in docs/PLACEHOLDERS.md. backend/config.py and the gate.sv constants mirror this block.
UNDERLYING_SYMBOL = "SPY"                # TODO(FINAL)
SPOT = 733.50                            # $  TODO(FINAL): confirm against a live quote later
VOL_BASE = 0.17                          # TODO(FINAL): smile level a
DIV = 0.011                              # continuous dividend yield  TODO(FINAL)
RATE = 0.039                             # TODO(FINAL)
EXPIRY_DAYS = 5                          # calendar days  TODO(FINAL)
T_YEARS = EXPIRY_DAYS / 365.0            # TODO(FINAL)
SMILE_A, SMILE_B, SMILE_C = VOL_BASE, -0.3, 2.0     # TODO(FINAL)
VOL_LAMBDA = 1.0                         # TODO(FINAL)
VOL_BETA = 0.0                           # spot-vol beta, 0 for now  TODO(FINAL)

N_STRIKES = 4                            # 4 strikes x {call, put} = 8 contracts  TODO(FINAL)
STRIKE_STEP = 1.0                        # $  TODO(FINAL)
STRIKE_OFFSET = -2                       # A22: lowest strike = nearest strike - 2  TODO(FINAL)


def strikes_for(S_dollars):
    """A22: recenter on the nearest strike (round half up). 733.50 -> (732, 733, 734, 735)."""
    center = math.floor(S_dollars / STRIKE_STEP + 0.5)
    return tuple((center + STRIKE_OFFSET + i) * STRIKE_STEP for i in range(N_STRIKES))


STRIKES = strikes_for(SPOT)              # grid at the default spot; snapshots recenter (A22)
SPOT_CENTS = math.floor(SPOT * 100 + 0.5)

# simulated market maker and costs (consumed by the simulator side; listed here as the source)
MM_HALF_SPREAD_TICKS = 1                 # TODO(FINAL)
QUOTE_SIZE_MIN, QUOTE_SIZE_MAX = 20, 200 # contracts  TODO(FINAL)
FEE_TICKS_PER_CONTRACT = 0.5             # TODO(FINAL)
STRESS_SINGLE_JUMP_CENTS = 40            # scheduled stress: one +40 c jump  TODO(FINAL)
STRESS_FLASH_STEPS = 5                   # TODO(FINAL)
STRESS_FLASH_STEP_CENTS = -150           # each of the 5 steps (unchanged in dollars)  TODO(FINAL)

# strategy defaults and search ranges (tuned on seeds 0-59 only)
EDGE_THRESHOLD_TICKS = 1.5               # TODO(FINAL)
EDGE_THRESHOLD_RANGE = (1.0, 3.0)        # ticks  TODO(FINAL)
MIN_MOVE_CENTS = 2                       # stays in CENTS (not bps)  TODO(FINAL)
MIN_MOVE_RANGE = (1, 5)                  # cents  TODO(FINAL)
HEDGE_BAND_SHARES_RANGE = (200, 1000)    # tunable; TODO(FINAL): no default confirmed
HEDGE_BAND_SHARES = None                 # TODO(FINAL): unset until tuned/confirmed

# R04: recomputed by golden/error_curve.py adopted_jump_limit() at this calibration and recorded
# here (gate.sv needs a literal). test_jump_limit_is_tighter_error_curve_proposal re-checks it.
JUMP_LIMIT_CENTS = 370                   # A3  TODO(FINAL)
EDGE_THRESHOLD = round(EDGE_THRESHOLD_TICKS * 256)   # 384 = 1.5 ticks, 8 fraction bits  TODO(FINAL)
MAX_SPREAD_TICKS = 20
MAX_ORDERS_PER_EVENT = 2
SIZE_CAP_BASE, SIZE_CAP_MAX = 5, 10
EDGE_CAP_STEP = 2 * EDGE_THRESHOLD       # A14

# risk gate limits (compile-time constants on the FPGA)
STALE_US = 250_000
SYNC_MAX_BEHIND = 64
JUMP_LIMIT_RAW = JUMP_LIMIT_CENTS * 16
PRICE_BAND_TICKS = 10
HEDGE_BAND_CENTS = 10
MAX_ORDER_QTY = 10
MAX_HEDGE_QTY = 1000
MAX_POSITION = 20
NET_DELTA_LIMIT = 20                     # R09: 20.00 deltas; hedge quantity is spy_shares  TODO(FINAL)
NET_GAMMA_LIMIT_X100 = 3                 # R10: 0.03 delta per cent  TODO(FINAL): unchanged from the
                                         #   legacy universe; per-contract gamma_c on SPY is ~2.4x smaller,
                                         #   so R10 binds ~2.4x later (effectively looser)
NET_VEGA_LIMIT = 1500                    # R11: $ per vol point (scaled from $300)  TODO(FINAL)
MAX_GATE_ORDERS_PER_EVENT = 8
DRAWDOWN_LIMIT_DOLLARS = 2000
HEARTBEAT_US = 150_000
REDUCE_ONLY_SKIPS = frozenset({1, 2, 3, 4, 6, 8, 9, 10, 11, 13, 14})

# ---------------------------------------------------------------- fixed-point formats
@dataclass(frozen=True)
class Fmt:
    name: str
    bits: int
    frac: int
    signed: bool
    lo: int
    hi: int
    unit: str


def _fmt(name, bits, frac, signed, unit, lo=None, hi=None):
    if lo is None:
        lo = -(1 << (bits - 1)) if signed else 0
    if hi is None:
        hi = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
    return Fmt(name, bits, frac, signed, lo, hi, unit)


DS_LIMIT = 16383                                                   # A2
FMT_S = _fmt("S", 21, 4, False, "cents")
FMT_DS = _fmt("dS", 16, 4, True, "cents", -DS_LIMIT, DS_LIMIT)
FMT_THEO = _fmt("theo", 18, 8, False, "ticks")
FMT_DELTA_ADJ = _fmt("delta_adj", 18, 16, True, "delta (cents per cent)")
FMT_GAMMA_C = _fmt("gamma_c", 18, 23, False, "delta per cent of spot")
FMT_VEGA = _fmt("vega", 16, 8, False, "$ per contract per vol point")
FMT_PRICE = _fmt("price", 18, 0, False, "option ticks or stock cents")
FMT_SIZE = _fmt("size", 12, 0, False, "contracts or shares")
FORMATS = {f.name: f for f in (FMT_S, FMT_DS, FMT_THEO, FMT_DELTA_ADJ,
                               FMT_GAMMA_C, FMT_VEGA, FMT_PRICE, FMT_SIZE)}


def sat(x, lo, hi): return max(lo, min(hi, x))


def to_fixed(x, fmt):
    """Float in fmt.unit -> saturated raw integer (round half up, A4)."""
    return sat(math.floor(x * (1 << fmt.frac) + 0.5), fmt.lo, fmt.hi)


def from_fixed(raw, fmt):
    """Raw integer -> float in fmt.unit. Saturates out-of-range raw first."""
    return sat(raw, fmt.lo, fmt.hi) / (1 << fmt.frac)


def ds_raw(S_raw, S_ref_raw):
    """dS = S - S_ref in raw 1/16-cent units, saturated to +/-DS_LIMIT."""
    return sat(S_raw - S_ref_raw, FMT_DS.lo, FMT_DS.hi)


# ---------------------------------------------------------------- golden repricing (bit-exact)
def rshift_round(x, k): return (x + (1 << (k - 1))) >> k


def reprice(theo0, d_adj, gam, dS):
    p1 = rshift_round(d_adj * dS, 12)                                   # ticks, 8 fraction bits
    g1 = sat(rshift_round(gam * dS, 11), -(1 << 17), (1 << 17) - 1)     # delta change, 16 fraction bits
    p2 = rshift_round(g1 * dS, 13)                                      # includes the 1/2 of the gamma term
    return sat(theo0 + p1 + p2, 0, (1 << 18) - 1)


def reprice_terms(theo0, d_adj, gam, dS):
    """Intermediate values of reprice() for RTL debug: (p1, g1, p2, theo_new)."""
    p1 = rshift_round(d_adj * dS, 12)
    g1 = sat(rshift_round(gam * dS, 11), -(1 << 17), (1 << 17) - 1)
    p2 = rshift_round(g1 * dS, 13)
    out = reprice(theo0, d_adj, gam, dS)
    assert out == sat(theo0 + p1 + p2, 0, (1 << 18) - 1)
    return p1, g1, p2, out


def reprice_float(theo0_ticks, delta, gamma_per_cent, dS_cents):
    """Float second-order Taylor of the same inputs, clipped to the theo range (ticks)."""
    v = theo0_ticks + delta * dS_cents + 0.5 * gamma_per_cent * dS_cents * dS_cents
    return min(max(v, 0.0), FMT_THEO.hi / (1 << FMT_THEO.frac))


# ---------------------------------------------------------------- microprice (bit-exact)
RECIP_BITS, RECIP_ADDR_BITS = 20, 13                                  # A10b
RECIP_TMAX = (1 << RECIP_ADDR_BITS) - 1


def make_recip():
    """recip[t] = round(2^20 / t), t = 1..8191, saturated to 20 bits. No exact .5 ties exist."""
    one = 1 << RECIP_BITS
    return [0] + [min((2 * one + t) // (2 * t), one - 1) for t in range(1, RECIP_TMAX + 1)]   # A10, A10b


RECIP = make_recip()


def microprice_q4(bid_c, ask_c, bsz, asz, recip):
    while bsz + asz > RECIP_TMAX: bsz >>= 1; asz >>= 1     # never runs for 12-bit sizes
    tot = bsz + asz
    frac = ((ask_c - bid_c) * bsz) * recip[tot]
    k = RECIP_BITS - 4
    return (bid_c << 4) + ((frac + (1 << (k - 1))) >> k)   # cents, 4 fraction bits


def microprice_float(bid_c, ask_c, bsz, asz):
    """Size-weighted microprice in cents, with the original sizes."""
    return bid_c + (ask_c - bid_c) * bsz / (bsz + asz)


# ---------------------------------------------------------------- edges and firing
def edge_buy(theo_new, ask): return theo_new - (ask << 8)


def edge_sell(theo_new, bid): return (bid << 8) - theo_new


def quote_sane(bid, ask, bsz, asz):
    """R05: bid > 0, ask > bid, spread <= 20 ticks, sizes > 0."""
    return bid > 0 and ask > bid and ask - bid <= MAX_SPREAD_TICKS and bsz > 0 and asz > 0


def fire_buy(theo_new, bid, ask, bsz, asz, locked_out, threshold=EDGE_THRESHOLD):
    return (edge_buy(theo_new, ask) > threshold and quote_sane(bid, ask, bsz, asz)
            and not locked_out and asz > 0)


def fire_sell(theo_new, bid, ask, bsz, asz, locked_out, threshold=EDGE_THRESHOLD):
    return (edge_sell(theo_new, bid) > threshold and quote_sane(bid, ask, bsz, asz)
            and not locked_out and bsz > 0)


# ---------------------------------------------------------------- message word (128 bits)
WORD_FIELDS = (            # name, lsb, width (CLAUDE.md message word table)
    ("type", 120, 8), ("seq", 104, 16), ("ts_us", 72, 32), ("id", 68, 4),
    ("bid", 50, 18), ("ask", 32, 18), ("bid_size", 20, 12), ("ask_size", 8, 12),
    ("crc8", 0, 8),
)
MSG_UNDERLYING, MSG_OPTION, MSG_BUY_FILL, MSG_SELL_FILL, MSG_SESSION = 1, 2, 3, 4, 5
UNDERLYING_ID = 8


def crc8(data, poly=0x07, init=0x00):
    """CRC-8 (PROPOSED poly 0x07, init 0), MSB first, no reflection, no final XOR (A11)."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def word_crc(word):
    """CRC-8 over bits 127..8 as 15 big-endian bytes."""
    return crc8((word >> 8).to_bytes(15, "big"))


def build_word(type, seq, ts_us, id, bid, ask, bid_size, ask_size, crc=None):
    """Pack fields into a 128-bit word; crc=None fills in the correct CRC-8."""
    vals = {"type": type, "seq": seq, "ts_us": ts_us, "id": id, "bid": bid, "ask": ask,
            "bid_size": bid_size, "ask_size": ask_size, "crc8": 0}
    w = 0
    for name, lsb, width in WORD_FIELDS:
        assert 0 <= vals[name] < (1 << width), (name, vals[name])
        w |= vals[name] << lsb
    return w | (word_crc(w) if crc is None else crc)


def parse_word(word):
    """Unpack a 128-bit word into fields plus crc_ok."""
    f = {name: (word >> lsb) & ((1 << width) - 1) for name, lsb, width in WORD_FIELDS}
    f["crc_ok"] = int(word_crc(word) == f["crc8"])
    return f


class SeqTracker:
    """Feed sequence checker (A12). step() returns the flags for one word."""

    def __init__(self):
        self.last = None

    def step(self, word):
        f = parse_word(word)
        out = {"crc_ok": f["crc_ok"], "first": 0, "seq_gap": 0, "seq_dup": 0}
        if f["crc_ok"]:
            if self.last is None:
                out["first"] = 1
                self.last = f["seq"]
            elif f["seq"] == self.last:
                out["seq_dup"] = 1
            else:
                out["seq_gap"] = int(f["seq"] != (self.last + 1) & 0xFFFF)
                self.last = f["seq"]
        out["last_seq"] = 0 if self.last is None else self.last
        return out


# ---------------------------------------------------------------- trigger (order selection)
def edge_cap(edge):
    return SIZE_CAP_MAX if edge > EDGE_CAP_STEP else SIZE_CAP_BASE          # A14


def trigger(theo_new, bid, ask, bid_size, ask_size, quote_ok, lockout, pos,
            max_orders=MAX_ORDERS_PER_EVENT):
    """Pick up to max_orders orders from the 16 edges (A13-A15).

    Returns {"edges": [16 signed], "eligible": 16-bit mask, "orders": [dict]}.
    """
    edges, eligible, cands = [], 0, []
    for c in range(8):
        for side in (0, 1):
            e = 2 * c + side
            if side == 0:
                edge, disp, room, price = edge_buy(theo_new[c], ask[c]), ask_size[c], MAX_POSITION - pos[c], ask[c]
            else:
                edge, disp, room, price = edge_sell(theo_new[c], bid[c]), bid_size[c], MAX_POSITION + pos[c], bid[c]
            edges.append(edge)
            if edge <= EDGE_THRESHOLD or not (quote_ok >> c) & 1 or (lockout >> e) & 1 or disp == 0:
                continue
            size = min(disp, edge_cap(edge), MAX_ORDER_QTY, max(room, 0))
            if size == 0:
                continue
            eligible |= 1 << e
            cands.append((-edge, e, {"contract": c, "side": side, "size": size, "price": price, "edge": edge}))
    cands.sort(key=lambda t: (t[0], t[1]))
    return {"edges": edges, "eligible": eligible, "orders": [d for _, _, d in cands[:max_orders]]}


# ---------------------------------------------------------------- risk gate
GATE_STATE_FIELDS = (
    "now_us", "snap_flip_us", "ref_seq", "feed_seq", "feed_bad", "S", "S_ref",
    "q_bid", "q_ask", "q_bsz", "q_asz",
    "theo_0", "theo_new", "delta_adj", "gamma_c", "vega", "pos_conf", "pos_pend",  # 8-lists
    "sh_conf", "sh_pend", "cash_cents", "orders_in_event", "tokens", "hb_last_us", "kill",
)
GATE_ORDER_FIELDS = ("is_hedge", "contract", "side", "qty", "price")


def is_reduce_only(pos_before, signed_qty):
    """A20: opposite sign to a nonzero position and no crossing through zero."""
    return (pos_before != 0 and signed_qty != 0 and (pos_before > 0) != (signed_qty > 0)
            and abs(signed_qty) <= abs(pos_before))


def gate_exposures(st):
    """Precomputed sums the gate reads (before the candidate order). pos = confirmed + pending.

    sum_delta = sum(pos*delta_adj) (Q16), sum_gamma = sum(pos*gamma_c), sum_vega = sum(pos*vega) (Q8),
    sh = hedge shares, equity_x6400 = 64*cash_cents + 25*sum(pos_conf*theo_new) + 4*sh_conf*S
    (equity in units of $1/6400).
    """
    pos = [st["pos_conf"][i] + st["pos_pend"][i] for i in range(8)]
    return {
        "sum_delta": sum(p * d for p, d in zip(pos, st["delta_adj"])),
        "sum_gamma": sum(p * gm for p, gm in zip(pos, st["gamma_c"])),
        "sum_vega": sum(p * v for p, v in zip(pos, st["vega"])),
        "sh": st["sh_conf"] + st["sh_pend"],
        "equity_x6400": (64 * st["cash_cents"] + 25 * sum(p * t for p, t in zip(st["pos_conf"], st["theo_new"]))
                         + 4 * st["sh_conf"] * st["S"]),
        "mp_q4": microprice_q4(st["q_bid"], st["q_ask"], st["q_bsz"], st["q_asz"], RECIP),
    }


def gate_eval(st, od):
    """Evaluate all 15 checks. st/od are dicts with GATE_STATE_FIELDS / GATE_ORDER_FIELDS.

    q_* is the quote of the instrument being ordered (option ticks, or underlying
    cents for a hedge). Returns {"reduce_only", "fail_mask", "reason"}; bit k-1 of
    fail_mask is check k after reduce-only exemptions; reason = lowest failing k or 0.
    Exposure after the fill = precomputed sum + one product for the candidate order.
    """
    M32 = 0xFFFFFFFF
    sq = od["qty"] if od["side"] == 0 else -od["qty"]
    c, hedge = od["contract"], bool(od["is_hedge"])
    x = gate_exposures(st)
    pos_c = st["pos_conf"][c] + st["pos_pend"][c]
    reduce = is_reduce_only(x["sh"] if hedge else pos_c, sq)
    opt_q = 0 if hedge else sq
    sh = x["sh"] + (sq if hedge else 0)
    net_delta = 100 * (x["sum_delta"] + opt_q * st["delta_adj"][c]) + sh * 65536
    net_gamma = 100 * (x["sum_gamma"] + opt_q * st["gamma_c"][c])
    net_vega = x["sum_vega"] + opt_q * st["vega"][c]

    fail = {
        1: ((st["now_us"] - st["snap_flip_us"]) & M32) > STALE_US,
        2: ((st["feed_seq"] - st["ref_seq"]) & 0xFFFF) > SYNC_MAX_BEHIND,
        3: bool(st["feed_bad"]),
        4: abs(st["S"] - st["S_ref"]) > JUMP_LIMIT_RAW,
        5: not quote_sane(st["q_bid"], st["q_ask"], st["q_bsz"], st["q_asz"]),
        6: (abs((od["price"] << 4) - x["mp_q4"]) > HEDGE_BAND_CENTS << 4) if hedge else
           (abs((od["price"] << 8) - st["theo_0"][c]) > PRICE_BAND_TICKS << 8),
        7: od["qty"] > (MAX_HEDGE_QTY if hedge else MAX_ORDER_QTY),
        8: (not hedge) and abs(pos_c + sq) > MAX_POSITION,
        9: abs(net_delta) > NET_DELTA_LIMIT * 100 * 65536,
        10: abs(net_gamma) > NET_GAMMA_LIMIT_X100 * (1 << 23),
        11: abs(net_vega) > NET_VEGA_LIMIT * 256,
        12: st["orders_in_event"] + 1 > MAX_GATE_ORDERS_PER_EVENT or st["tokens"] == 0,
        13: x["equity_x6400"] < -DRAWDOWN_LIMIT_DOLLARS * 6400,
        14: ((st["now_us"] - st["hb_last_us"]) & M32) > HEARTBEAT_US,
        15: bool(st["kill"]),
    }
    mask = 0
    for k, bad in fail.items():
        if bad and not (reduce and k in REDUCE_ONLY_SKIPS):
            mask |= 1 << (k - 1)
    reason = next((k for k in range(1, 16) if (mask >> (k - 1)) & 1), 0)
    return {"reduce_only": int(reduce), "fail_mask": mask, "reason": reason}


def gate(st, od): return gate_eval(st, od)["reason"]


# ---------------------------------------------------------------- float Black-Scholes
def _ncdf(x): return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _npdf(x): return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs(S, K, T, r, q, sigma, is_call):
    """European Black-Scholes, S and K in $, T in years.

    Returns dict: price ($/share), delta (per $), gamma (per $ per $),
    vega ($ per share per vol point), theta ($ per share per calendar day).
    """
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    pdf1 = _npdf(d1)
    call_delta = dq * _ncdf(d1)
    gamma = dq * pdf1 / (S * sigma * sq)
    vega = S * dq * pdf1 * sq / 100.0
    decay = -S * dq * pdf1 * sigma / (2.0 * sq)
    if is_call:
        price = S * dq * _ncdf(d1) - K * dr * _ncdf(d2)
        delta = call_delta
        theta = decay - r * K * dr * _ncdf(d2) + q * S * dq * _ncdf(d1)
    else:
        price = K * dr * _ncdf(-d2) - S * dq * _ncdf(-d1)
        delta = call_delta - dq                                # exact put delta
        theta = decay + r * K * dr * _ncdf(-d2) - q * S * dq * _ncdf(-d1)
    return {"price": price, "delta": delta, "gamma": gamma, "vega": vega, "theta": theta / 365.0}


# ---------------------------------------------------------------- snapshot builder
def smile_sigma(m, a=SMILE_A, b=SMILE_B, c=SMILE_C): return a + b * m + c * m * m


def build_snapshot(S_ref_dollars=SPOT, strikes=None, T=T_YEARS, r=RATE, q=DIV,
                   a=SMILE_A, b=SMILE_B, c=SMILE_C, lam=VOL_LAMBDA, beta=VOL_BETA):
    """Quantized 8-contract snapshot. Contract i = 4 * is_put + strike index.

    strikes=None recenters on the nearest strike to the quantized S_ref (A22).
    Returns {"S_ref": raw, "strikes": tuple, "contracts": [dict(theo_0, delta_adj, gamma_c,
    vega, plus float fields for inspection)]}.
    """
    S_ref_raw = to_fixed(S_ref_dollars * 100.0, FMT_S)
    S = from_fixed(S_ref_raw, FMT_S) / 100.0                    # A5
    if strikes is None:
        strikes = strikes_for(S)                                # A22
    out = []
    for is_put in (False, True):
        for K in strikes:
            m = math.log(K / S)
            sigma = smile_sigma(m, a, b, c)
            g = bs(S, K, T, r, q, sigma, not is_put)
            dsigma_dS = -lam * (b + 2.0 * c * m) / S + beta     # A7
            delta_adj = g["delta"] + (g["vega"] * 100.0) * dsigma_dS
            out.append({
                "K": K, "is_put": is_put, "sigma": sigma,
                "price": g["price"], "delta": g["delta"], "delta_adj_f": delta_adj,
                "gamma": g["gamma"], "vega_f": g["vega"], "theta": g["theta"],
                "theo_0": to_fixed(g["price"] * 100.0, FMT_THEO),
                "delta_adj": to_fixed(delta_adj, FMT_DELTA_ADJ),
                "gamma_c": to_fixed(g["gamma"] / 100.0, FMT_GAMMA_C),
                "vega": to_fixed(g["vega"] * 100.0, FMT_VEGA),
            })
    return {"S_ref": S_ref_raw, "strikes": tuple(strikes), "contracts": out}


# ---------------------------------------------------------------- format limits at this calibration
def check_format_limits(spot=SPOT):
    """Assert the universe fits the EXISTING fixed-point formats (formats are not changed).

    S field max $1,310.71 (spot + jump must fit); theo cap $10.24 (max 5-day price ~$6.77 must
    fit, and a reprice at +/-jump_limit must not saturate); dS cap 1,023.9 cents (jump_limit must
    fit); gamma_c ~2,300 must fit 18 bits and keep g1 unsaturated inside +/-jump_limit.
    Cents-based parameters (min_move, jump_limit) stay in cents. Returns the measured numbers.
    """
    snap = build_snapshot(S_ref_dollars=spot)
    cs = snap["contracts"]
    out = {
        "S_max_dollars": from_fixed(FMT_S.hi, FMT_S) / 100.0,
        "theo_cap_dollars": from_fixed(FMT_THEO.hi, FMT_THEO) / 100.0,
        "ds_cap_cents": from_fixed(FMT_DS.hi, FMT_DS),
        "max_theo_dollars": max(c["price"] for c in cs),
        "max_gamma_c": max(c["gamma_c"] for c in cs),
        "max_vega_code": max(c["vega"] for c in cs),
    }
    assert spot * 100 + JUMP_LIMIT_CENTS < out["S_max_dollars"] * 100, "S field cannot hold spot + jump"
    assert out["max_theo_dollars"] < out["theo_cap_dollars"], "theo field cannot hold the snapshot price"
    assert JUMP_LIMIT_CENTS < out["ds_cap_cents"], "dS field cannot hold jump_limit"
    assert out["max_gamma_c"] < FMT_GAMMA_C.hi and out["max_vega_code"] < FMT_VEGA.hi
    assert all(FMT_DELTA_ADJ.lo < c["delta_adj"] < FMT_DELTA_ADJ.hi for c in cs)
    for c in cs:
        for d in (JUMP_LIMIT_RAW, -JUMP_LIMIT_RAW):
            _, g1, _, th = reprice_terms(c["theo_0"], c["delta_adj"], c["gamma_c"], d)
            assert -(1 << 17) < g1 < (1 << 17) - 1, "gamma term saturates inside jump_limit"
            assert 0 < th < FMT_THEO.hi, "reprice saturates inside jump_limit"
    assert isinstance(MIN_MOVE_CENTS, int) and isinstance(JUMP_LIMIT_CENTS, int)   # cents, not bps
    return out


FORMAT_LIMITS = check_format_limits()    # import-time assertion at the current calibration
