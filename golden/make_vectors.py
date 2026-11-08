"""Write hex test vectors for the RTL testbenches into docs/vectors/ (fixed seed).

Every expected column comes from a golden.py function; this file only builds inputs,
formats hex and (for directed gate cases) asserts that a vector really exercises
what its label says.

SIMULATED: market-like numbers here are placeholders, not live data. Underlying prices are
derived from golden SPOT_CENTS / JUMP_LIMIT_RAW / NET_VEGA_LIMIT (all TODO(FINAL) in golden.py).

Usage: python golden/make_vectors.py [out_dir]
"""

import copy
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import golden as g  # noqa: E402

ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "docs", "vectors")
SEED = 20261107
M32 = 0xFFFFFFFF
U = g.SPOT_CENTS                       # default spot in cents (TODO(FINAL) in golden.py)
VEGA_MAX = g.FMT_VEGA.hi               # largest per-contract vega code
VEGA_LIM = g.NET_VEGA_LIMIT * 256      # R11 limit in Q8


# ---------------------------------------------------------------- table writer
def hx(v, bits, signed):
    if signed:
        assert -(1 << (bits - 1)) <= v < (1 << (bits - 1)), (v, bits)
        v &= (1 << bits) - 1
    else:
        assert 0 <= v < (1 << bits), (v, bits)
    return f"{v:0{(bits + 3) // 4}x}"


class Table:
    """cols: (name, bits, signed, description[, count]). count > 1 expands to name0..nameN-1."""

    def __init__(self, fname, title, about, cols, kinds=None):
        self.fname, self.title, self.about, self.kinds = fname, title, about, kinds or {}
        self.cols = [c if len(c) == 5 else (*c, 1) for c in cols]
        self.rows = []

    def add(self, **row):
        missing = {c[0] for c in self.cols} - row.keys()
        assert not missing, missing
        self.rows.append(row)

    def headers(self):
        return [n if k == 1 else f"{n}{i}" for n, _, _, _, k in self.cols for i in range(k)]

    def write(self, out_dir):
        with open(os.path.join(out_dir, self.fname), "w", newline="\n") as f:
            f.write(",".join(self.headers()) + "\n")
            for r in self.rows:
                cells = []
                for n, bits, signed, _, k in self.cols:
                    vals = [r[n]] if k == 1 else list(r[n])
                    assert len(vals) == k, (n, vals)
                    cells += [hx(int(v), bits, signed) for v in vals]
                f.write(",".join(cells) + "\n")

    def readme(self):
        lines = [f"## {self.fname} - {self.title}", "", self.about, "",
                 f"Rows: {len(self.rows)}. Columns: {len(self.headers())}.", "",
                 "| Column | Bits | Signed | Hex digits | Description |",
                 "| --- | --- | --- | --- | --- |"]
        for n, bits, signed, desc, k in self.cols:
            name = n if k == 1 else f"{n}0..{n}{k - 1}"
            lines.append(f"| `{name}` | {bits} | {'yes' if signed else 'no'} | {(bits + 3) // 4} | {desc} |")
        if self.kinds:
            lines += ["", "`kind` codes:", "", "| kind | Meaning |", "| --- | --- |"]
            lines += [f"| {k:x} | {v} |" for k, v in sorted(self.kinds.items())]
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 1. parser
PARSER_KINDS = {
    0: "valid, in order", 1: "bad CRC: one payload bit flipped after the CRC was computed",
    2: "bad CRC: CRC byte wrong", 3: "sequence gap forward", 4: "duplicate (exact repeat of the last good word)",
    5: "sequence goes backwards (reported as a gap)", 6: "valid word across the 65,535 -> 0 wrap",
}


def gen_parser():
    t = Table("parser.csv", "128-bit message words and parsed fields",
              "A **stream**: feed rows in file order into one parser that starts from reset. "
              "Expected flags depend on the words before them (golden `SeqTracker`, A12). "
              "Fields are extracted even when the CRC is bad. The stream starts at seq 65,400 "
              "so it wraps naturally.",
              [("kind", 4, False, "generator label, see table below"),
               ("word", 128, False, "message word, bit 127 first (MSB)"),
               ("type", 8, False, "bits 127..120"), ("seq", 16, False, "bits 119..104"),
               ("ts_us", 32, False, "bits 103..72"), ("id", 4, False, "bits 71..68"),
               ("bid", 18, False, "bits 67..50 (fill price in a fill report)"),
               ("ask", 18, False, "bits 49..32 (filled quantity in a fill report)"),
               ("bid_size", 12, False, "bits 31..20"), ("ask_size", 12, False, "bits 19..8"),
               ("crc8", 8, False, "bits 7..0 as received"),
               ("crc_calc", 8, False, "CRC-8 computed over bits 127..8"),
               ("crc_ok", 1, False, "crc_calc == crc8"),
               ("first", 1, False, "first CRC-good word since reset (sets the reference, no seq check)"),
               ("seq_gap", 1, False, "seq != last + 1 and != last (CRC-good words only)"),
               ("seq_dup", 1, False, "seq == last (CRC-good words only)"),
               ("last_seq", 16, False, "parser reference seq after this word (0 before the first good word)")],
              PARSER_KINDS)
    rng = random.Random(SEED + 1)
    trk = g.SeqTracker()
    seq, ts, last_good = 65400, 0, None

    def fields():
        typ = rng.choices([1, 2, 3, 4, 5], [40, 45, 6, 6, 3])[0]
        if typ == 1:
            bid = rng.randint(U - 100, U + 100)
            return dict(type=typ, id=8, bid=bid, ask=bid + 1, bid_size=rng.randint(1, 4095), ask_size=rng.randint(1, 4095))
        if typ == 2:
            bid = rng.randint(20, 600)
            return dict(type=typ, id=rng.randint(0, 7), bid=bid, ask=bid + rng.randint(1, 4),
                        bid_size=rng.randint(1, 50), ask_size=rng.randint(1, 50))
        if typ in (3, 4):
            return dict(type=typ, id=rng.randint(0, 7), bid=rng.randint(20, 600), ask=rng.randint(1, 10),
                        bid_size=0, ask_size=0)
        return dict(type=typ, id=0, bid=0, ask=0, bid_size=0, ask_size=0)

    def emit(kind, word):
        exp = trk.step(word)
        f = g.parse_word(word)
        t.add(kind=kind, word=word, crc_calc=g.word_crc(word),
              **{k: f[k] for k in ("type", "seq", "ts_us", "id", "bid", "ask", "bid_size", "ask_size", "crc8")},
              **{k: exp[k] for k in ("crc_ok", "first", "seq_gap", "seq_dup", "last_seq")})

    schedule = [0] * 20 + [1, 0, 0, 2, 0, 3, 0, 4, 0, 5, 0, 0, 4, 4, 0, 1, 3, 0]
    for i in range(600):
        kind = schedule[i] if i < len(schedule) else rng.choices([0, 1, 2, 3, 4, 5], [85, 3, 3, 3, 3, 3])[0]
        ts = (ts + max(1, int(rng.expovariate(1 / 200)))) & M32
        if kind == 4 and last_good is not None:
            emit(4, last_good)
            continue
        if kind == 3:
            seq = (seq + rng.randint(2, 40)) & 0xFFFF
        elif kind == 5:
            seq = (seq - rng.randint(2, 40)) & 0xFFFF
        w = g.build_word(seq=seq, ts_us=ts, **fields())
        if kind == 1:
            w ^= 1 << rng.randint(8, 127)
        elif kind == 2:
            w ^= rng.randint(1, 255)
        elif kind == 0 and seq == 0:
            kind = 6
        emit(kind if kind != 4 else 0, w)
        if kind in (0, 3, 5, 6):
            last_good = w
        seq = (seq + 1) & 0xFFFF
    counts = {k: sum(r["kind"] == k for r in t.rows) for k in PARSER_KINDS}
    assert all(counts.values()), counts
    return t


# ---------------------------------------------------------------- 2. microprice
MP_KINDS = {0: "random, bid_size + ask_size <= 4,095", 1: "bid_size + ask_size > 4,095 (upper half of the 13-bit recip ROM)",
            2: "edge: one side 0, zero spread, size 4,095 both sides, top of the 18-bit range, total 1 (saturated recip)",
            3: "large totals near 4,095 and 8,190 (where the old 16-bit recip error was worst)"}


def gen_microprice():
    t = Table("microprice.csv", "size-weighted microprice, Q4 cents",
              "Independent rows. Expected value is golden `microprice_q4(bid, ask, bid_size, ask_size, RECIP)` "
              "with recip[t] = round(2^20 / t), t = 1..8191 (13-bit address, 20-bit entries, recip[1] saturated "
              "to 2^20 - 1; golden A10b). Sizes are 12-bit message fields, so the total is at most 8,190 and "
              "no halving ever happens. "
              "Spreads are 0..20 cents (R05 range); both sizes 0 is never generated (R05 rejects it).",
              [("kind", 4, False, "generator label"),
               ("bid", 18, False, "bid, cents"), ("ask", 18, False, "ask, cents (>= bid)"),
               ("bid_size", 12, False, "bid size"), ("ask_size", 12, False, "ask size"),
               ("mp_q4", 24, False, "expected microprice, cents with 4 fraction bits")],
              MP_KINDS)
    rng = random.Random(SEED + 2)

    def add(kind, bid, ask, bsz, asz):
        t.add(kind=kind, bid=bid, ask=ask, bid_size=bsz, ask_size=asz,
              mp_q4=g.microprice_q4(bid, ask, bsz, asz, g.RECIP))

    for _ in range(3000):
        tot = rng.randint(2, 4095)
        bsz = rng.randint(1, tot - 1)
        bid = rng.randint(1, 50000)
        add(0, bid, bid + rng.randint(0, 20), bsz, tot - bsz)
    for _ in range(1500):
        bsz, asz = rng.randint(1, 4095), rng.randint(1, 4095)
        while bsz + asz <= 4095:
            bsz, asz = rng.randint(1, 4095), rng.randint(1, 4095)
        bid = rng.randint(1, 50000)
        add(1, bid, bid + rng.randint(0, 20), bsz, asz)
    top = (1 << 18) - 1
    for bid, ask, bsz, asz in [(U, U + 1, 0, 5), (U, U + 1, 5, 0), (U, U, 7, 9),
                               (U, U + 1, 4095, 4095), (U, U + 20, 4095, 4095), (U, U + 20, 4095, 1),
                               (U, U + 20, 1, 4095), (top - 20, top, 4095, 1), (top - 20, top, 1, 1),
                               (1, 21, 2048, 2048), (1, 21, 2047, 2048), (100, 102, 1, 1), (100, 101, 15, 1),
                               (100, 101, 1, 0), (100, 120, 1, 0), (100, 120, 0, 1)]:
        add(2, bid, ask, bsz, asz)
    for tot in range(3960, 4000):
        for bsz in (tot - 1, tot - 69, tot // 2):
            add(3, 1000, 1002, bsz, tot - bsz)
    for tot in range(8150, 8191):
        for bsz in (4095, tot - 4095, tot // 2):
            add(3, 1000, 1020, bsz, tot - bsz)
    return t


# ---------------------------------------------------------------- 3. reprice
RP_KINDS = {0: f"random, realistic (|dS| <= jump_limit {g.JUMP_LIMIT_CENTS} cents, gamma_c < 2^15)", 1: "random over every input's full range",
            2: "dS saturation (|S - S_ref| > 16,383)", 3: "gamma term g1 saturates",
            4: "output clamps at 0", 5: "output clamps at 2^18 - 1", 6: "dS = 0",
            7: "snapshot contracts swept over dS -500..+500 cents in 5-cent steps",
            8: "round-half-up boundaries (positive and negative products)"}


def gen_reprice():
    t = Table("reprice.csv", "fast-path repricing",
              "Independent rows. ds = golden `ds_raw(s, s_ref)`; outputs = golden `reprice_terms(theo_0, delta_adj, gamma_c, ds)`; "
              "theo_new is exactly `reprice()`. p1, g1, p2 are intermediates for debugging.",
              [("kind", 4, False, "generator label"),
               ("s", 21, False, "S, cents Q4"), ("s_ref", 21, False, "S_ref, cents Q4"),
               ("ds", 16, True, "expected dS = sat(s - s_ref, -16383, +16383), cents Q4"),
               ("theo_0", 18, False, "ticks Q8"), ("delta_adj", 18, True, "Q16"),
               ("gamma_c", 18, False, "2^-23 delta per cent"),
               ("p1", 24, True, "rshift_round(delta_adj*ds, 12), ticks Q8"),
               ("g1", 18, True, "sat(rshift_round(gamma_c*ds, 11)), delta Q16"),
               ("p2", 24, True, "rshift_round(g1*ds, 13), ticks Q8"),
               ("theo_new", 18, False, "expected reprice() output, ticks Q8")],
              RP_KINDS)
    rng = random.Random(SEED + 3)
    SMAX = (1 << 21) - 1

    def add(kind, theo0, d_adj, gam, s, s_ref):
        ds = g.ds_raw(s, s_ref)
        p1, g1, p2, out = g.reprice_terms(theo0, d_adj, gam, ds)
        t.add(kind=kind, s=s, s_ref=s_ref, ds=ds, theo_0=theo0, delta_adj=d_adj, gamma_c=gam,
              p1=p1, g1=g1, p2=p2, theo_new=out)

    def spot_pair(d):
        s_ref = rng.randint(max(0, -d), min(SMAX, SMAX - d))
        return s_ref + d, s_ref

    full = lambda: (rng.randint(0, g.FMT_THEO.hi), rng.randint(g.FMT_DELTA_ADJ.lo, g.FMT_DELTA_ADJ.hi),
                    rng.randint(0, g.FMT_GAMMA_C.hi))
    for _ in range(8000):
        add(0, rng.randint(0, 600 << 8), rng.randint(-65536, 65536), rng.randint(0, (1 << 15) - 1),
            *spot_pair(rng.randint(-g.JUMP_LIMIT_RAW, g.JUMP_LIMIT_RAW)))
    for _ in range(6000):
        add(1, *full(), *spot_pair(rng.randint(-g.DS_LIMIT, g.DS_LIMIT)))
    for _ in range(1500):
        d = rng.choice([1, -1]) * rng.randint(g.DS_LIMIT + 1, 200000)
        add(2, *full(), *spot_pair(d))
    for d in (g.DS_LIMIT, g.DS_LIMIT + 1, -g.DS_LIMIT, -g.DS_LIMIT - 1, SMAX, -SMAX):
        add(2, 100 << 8, 1 << 15, 5000, *(spot_pair(d) if abs(d) < SMAX else ((SMAX, 0) if d > 0 else (0, SMAX))))
    for _ in range(1500):
        add(3, *full()[:2], rng.randint(140000, g.FMT_GAMMA_C.hi), *spot_pair(rng.choice([1, -1]) * rng.randint(9000, 20000)))
    for _ in range(1000):
        add(4, rng.randint(0, 2000), rng.randint(-(1 << 17), -1000), rng.randint(0, 3000), *spot_pair(rng.randint(500, 16383)))
    for _ in range(1000):
        add(5, rng.randint(g.FMT_THEO.hi - 2000, g.FMT_THEO.hi), rng.randint(1000, (1 << 17) - 1),
            rng.randint(0, 3000), *spot_pair(rng.randint(500, 16383)))
    for _ in range(500):
        add(6, *full(), *spot_pair(0))
    snap = g.build_snapshot()
    for c in snap["contracts"]:
        for cents in range(-500, 501, 5):
            add(7, c["theo_0"], c["delta_adj"], c["gamma_c"], snap["S_ref"] + cents * 16, snap["S_ref"])
    for d_adj, d in [(1, 2048), (-1, 2048), (1, -2048), (1, 2047), (-1, 2049), (3, 2048), (-3, 2048), (2, 1024), (-2, 1024)]:
        add(8, 1000, d_adj, 0, *spot_pair(d))
    for gam, d in [(1, 1024), (1, -1024), (3, 1365), (1, 2048)]:
        add(8, 1000, 0, gam, *spot_pair(d))
    assert len(t.rows) >= 20000, len(t.rows)
    return t


# ---------------------------------------------------------------- 4. trigger
TRIG_KINDS = {0: "random", 1: "no edge anywhere", 2: "edge exactly 384 (no fire) vs 385 (fire)",
              3: "edge cap step: 768 -> cap 5, 769 -> cap 10", 4: "more than 2 eligible: rank by edge",
              5: "ties: equal edges go to the lowest edge index", 6: "best edge locked out",
              7: "best contract quote_ok = 0", 8: "best side displayed size 0", 9: "position room 0 (dropped, next fills the slot)",
              10: "room / displayed size limit the size", 11: "all 8 contracts eligible on the buy side"}


def gen_trigger():
    t = Table("trigger.csv", "order selection from 16 edges",
              "Independent rows. Expected = golden `trigger()` (A13-A15). Edge index e = 2*contract + side "
              "(side 0 buy at ask, 1 sell at bid). An edge fires when edge > 384, quote_ok[c] = 1, "
              "lockout[e] = 0 and the displayed size on that side > 0; size = min(displayed, edge cap, 10, room), "
              "room = 20 - pos (buy) or 20 + pos (sell), floored at 0; size-0 candidates are dropped. "
              "At most 2 orders, highest edge first, ties to the lowest e. Unused order slots are all zero.",
              [("kind", 4, False, "generator label"),
               ("theo_new", 18, False, "per contract, ticks Q8", 8),
               ("bid", 18, False, "per contract option bid, ticks", 8),
               ("ask", 18, False, "per contract option ask, ticks", 8),
               ("bid_size", 12, False, "per contract displayed bid size", 8),
               ("ask_size", 12, False, "per contract displayed ask size", 8),
               ("quote_ok", 8, False, "bit c = contract c quote usable"),
               ("lockout", 16, False, "bit e = edge e locked out"),
               ("pos", 8, True, "per contract position, confirmed + pending", 8),
               ("edge", 28, True, "expected edge e (e = 2c + side), ticks Q8", 16),
               ("eligible", 16, False, "expected bit e = edge e would trade with size > 0"),
               ("n_orders", 2, False, "expected number of orders (0..2)"),
               ("o0_valid", 1, False, "order slot 0 used"), ("o0_contract", 3, False, ""),
               ("o0_side", 1, False, "0 buy, 1 sell"), ("o0_size", 4, False, "contracts"),
               ("o0_price", 18, False, "ask (buy) or bid (sell), ticks"), ("o0_edge", 28, True, "ticks Q8"),
               ("o1_valid", 1, False, "order slot 1 used"), ("o1_contract", 3, False, ""),
               ("o1_side", 1, False, ""), ("o1_size", 4, False, ""), ("o1_price", 18, False, ""),
               ("o1_edge", 28, True, "")],
              TRIG_KINDS)
    rng = random.Random(SEED + 4)

    def add(kind, s):
        res = g.trigger(s["theo_new"], s["bid"], s["ask"], s["bid_size"], s["ask_size"],
                        s["quote_ok"], s["lockout"], s["pos"])
        row = dict(kind=kind, edge=res["edges"], eligible=res["eligible"], n_orders=len(res["orders"]), **s)
        for slot in (0, 1):
            o = res["orders"][slot] if slot < len(res["orders"]) else None
            row.update({f"o{slot}_valid": int(o is not None)})
            for k in ("contract", "side", "size", "price", "edge"):
                row[f"o{slot}_{k}"] = o[k] if o else 0
        t.add(**row)
        return res

    def neutral():
        mids = [rng.randint(30, 800) for _ in range(8)]
        return dict(theo_new=[m << 8 for m in mids], bid=[m - 1 for m in mids], ask=[m + 1 for m in mids],
                    bid_size=[10] * 8, ask_size=[10] * 8, quote_ok=0xFF, lockout=0, pos=[0] * 8)

    def set_edge(s, c, side, edge):
        s["theo_new"][c] = (s["ask"][c] << 8) + edge if side == 0 else (s["bid"][c] << 8) - edge

    for _ in range(2000):
        mids = [rng.randint(30, 800) for _ in range(8)]
        bid = [m - rng.randint(1, 2) for m in mids]
        ask = [m + rng.randint(0, 2) for m in mids]
        ask = [max(a, b + 1) for a, b in zip(ask, bid)]
        s = dict(theo_new=[(m << 8) + rng.randint(-6 * 256, 6 * 256) for m in mids], bid=bid, ask=ask,
                 bid_size=[0 if rng.random() < 0.1 else rng.randint(1, 30) for _ in range(8)],
                 ask_size=[0 if rng.random() < 0.1 else rng.randint(1, 30) for _ in range(8)],
                 quote_ok=sum((rng.random() < 0.9) << c for c in range(8)),
                 lockout=sum((rng.random() < 0.08) << e for e in range(16)),
                 pos=[rng.randint(-22, 22) for _ in range(8)])
        add(0, s)
    add(1, neutral())
    for side in (0, 1):
        for edge in (384, 385):
            s = neutral(); set_edge(s, 3, side, edge); add(2, s)
        for edge in (768, 769):
            s = neutral(); s["bid_size"][5] = s["ask_size"][5] = 20; set_edge(s, 5, side, edge); add(3, s)
    s = neutral()
    for c, side, edge in [(0, 0, 500), (2, 1, 900), (4, 0, 700), (6, 1, 1200), (7, 0, 400)]:
        set_edge(s, c, side, edge)
    assert [o["contract"] for o in add(4, s)["orders"]] == [6, 2]
    s = neutral()
    for c in (5, 3, 6):
        set_edge(s, c, 0, 600)
    assert [o["contract"] for o in add(5, s)["orders"]] == [3, 5]
    s = neutral(); set_edge(s, 2, 0, 600); set_edge(s, 1, 1, 600)
    assert [(o["contract"], o["side"]) for o in add(5, s)["orders"]] == [(1, 1), (2, 0)]
    s = neutral(); set_edge(s, 4, 1, 2000); set_edge(s, 1, 0, 500); set_edge(s, 2, 0, 450)
    s["lockout"] = 1 << 9
    assert [o["contract"] for o in add(6, s)["orders"]] == [1, 2]
    s = neutral(); set_edge(s, 4, 1, 2000); set_edge(s, 1, 0, 500); s["quote_ok"] = 0xFF & ~(1 << 4)
    assert [o["contract"] for o in add(7, s)["orders"]] == [1]
    s = neutral(); set_edge(s, 4, 1, 2000); set_edge(s, 1, 0, 500); s["bid_size"][4] = 0
    assert [o["contract"] for o in add(8, s)["orders"]] == [1]
    s = neutral(); set_edge(s, 4, 0, 2000); set_edge(s, 1, 0, 500); set_edge(s, 2, 1, 450); s["pos"][4] = 20
    assert [o["contract"] for o in add(9, s)["orders"]] == [1, 2]
    s = neutral(); set_edge(s, 4, 1, 2000); s["pos"][4] = -20
    assert add(9, s)["orders"] == []
    s = neutral(); set_edge(s, 4, 0, 2000); s["pos"][4] = 17; s["ask_size"][4] = 20
    assert add(10, s)["orders"][0]["size"] == 3
    s = neutral(); set_edge(s, 4, 0, 2000); s["ask_size"][4] = 2
    assert add(10, s)["orders"][0]["size"] == 2
    s = neutral(); set_edge(s, 4, 1, 2000); s["pos"][4] = -18; s["bid_size"][4] = 20
    assert add(10, s)["orders"][0]["size"] == 2
    s = neutral()
    for c in range(8):
        set_edge(s, c, 0, 400 + 37 * c)
    assert add(11, s)["eligible"] == 0x5555
    return t


# ---------------------------------------------------------------- 5. gate
GATE_KINDS = {0: "baseline, passes", 1: "single violation of check `target` only",
              2: "boundary pair: exactly at the limit (passes)", 3: "boundary pair: one step beyond (fails `target`)",
              4: "two violations at once (`target` < `target2`; lower code wins)",
              5: "reduce-only order with check `target` violated (exempt, passes)",
              6: "same state as the row before, non-reduce order (fails `target`)",
              7: "reduce-only order, check `target` still applies (fails)",
              8: "order crosses through zero: not reduce-only (fails `target`)",
              9: "reduce exactly to zero: reduce-only (exempt, passes)", 10: "random"}


def gate_cols():
    return [("kind", 4, False, "generator label"), ("target", 4, False, "check the row targets (0 = none)"),
            ("target2", 4, False, "second check for kind 4, else 0"),
            ("now_us", 32, False, "current time, us"), ("snap_flip_us", 32, False, "time the active snapshot was committed, us"),
            ("ref_seq", 16, False, "snapshot ref_seq"), ("feed_seq", 16, False, "last feed seq"),
            ("feed_bad", 1, False, "R03 feed-integrity flag (gap, repeat or bad CRC since resync)"),
            ("S", 21, False, "current underlying, cents Q4"), ("S_ref", 21, False, "snapshot S_ref, cents Q4"),
            ("q_bid", 18, False, "quote of the ordered instrument: option ticks, or underlying cents for a hedge"),
            ("q_ask", 18, False, "same units as q_bid"), ("q_bsz", 12, False, "quote bid size"),
            ("q_asz", 12, False, "quote ask size"),
            ("theo_0", 18, False, "per contract, ticks Q8 (R06)", 8),
            ("theo_new", 18, False, "per contract, ticks Q8 (R13 equity)", 8),
            ("delta_adj", 18, True, "per contract, Q16", 8), ("gamma_c", 18, False, "per contract, 2^-23 delta per cent", 8),
            ("vega", 16, False, "per contract, $ per contract per vol point Q8", 8),
            ("pos_conf", 8, True, "per contract confirmed position, contracts", 8),
            ("pos_pend", 8, True, "per contract pending position, contracts", 8),
            ("sh_conf", 16, True, "confirmed hedge shares"), ("sh_pend", 16, True, "pending hedge shares"),
            ("cash_cents", 32, True, "cash, cents"), ("orders_in_event", 4, False, "orders already sent this event"),
            ("tokens", 5, False, "R12 token bucket level (0..20)"), ("hb_last_us", 32, False, "time of the last valid heartbeat, us"),
            ("kill", 1, False, "kill pin high or KILL frame received"),
            ("is_hedge", 1, False, "order is a hedge in underlying shares"),
            ("contract", 3, False, "option contract (0 for hedges)"), ("side", 1, False, "0 buy, 1 sell"),
            ("qty", 16, False, "contracts, or shares for a hedge"),
            ("price", 18, False, "limit price: option ticks, or cents for a hedge"),
            ("sum_delta", 32, True, "derived input (golden gate_exposures): sum(pos*delta_adj) before the order, pos = conf + pend, Q16"),
            ("sum_gamma", 32, True, "derived input: sum(pos*gamma_c) before the order"),
            ("sum_vega", 32, True, "derived input: sum(pos*vega) before the order, Q8"),
            ("sh", 17, True, "derived input: sh_conf + sh_pend"),
            ("equity_x6400", 48, True, "derived input: 64*cash_cents + 25*sum(pos_conf*theo_new) + 4*sh_conf*S ($1/6400 units)"),
            ("mp_q4", 24, False, "derived input: microprice_q4 of the q_* quote (used by R06 for hedges)"),
            ("reduce_only", 1, False, "expected: gate classifies the order as reduce-only (A20)"),
            ("fail_mask", 15, False, "expected: bit k-1 = check k fails after exemptions"),
            ("reason", 4, False, "expected reason code: lowest failing check, 0 = approve")]


def gate_baseline(hedge=False):
    snap = g.build_snapshot()
    cs = snap["contracts"]
    now = 1_000_000
    st = dict(now_us=now, snap_flip_us=now - 100_000, ref_seq=1000, feed_seq=1010, feed_bad=0,
              S=snap["S_ref"], S_ref=snap["S_ref"],
              theo_0=[c["theo_0"] for c in cs], theo_new=[c["theo_0"] for c in cs],
              delta_adj=[c["delta_adj"] for c in cs], gamma_c=[c["gamma_c"] for c in cs],
              vega=[c["vega"] for c in cs], pos_conf=[0] * 8, pos_pend=[0] * 8, sh_conf=0, sh_pend=0,
              cash_cents=0, orders_in_event=0, tokens=20, hb_last_us=now - 50_000, kill=0)
    if hedge:
        st.update(q_bid=U, q_ask=U + 1, q_bsz=10, q_asz=10)
        od = dict(is_hedge=1, contract=0, side=0, qty=100, price=U)
    else:
        price = (st["theo_0"][0] + 128) >> 8
        st.update(q_bid=price - 1, q_ask=price + 1, q_bsz=10, q_asz=10)
        od = dict(is_hedge=0, contract=0, side=0, qty=1, price=price)
    return st, od


def _set(**kw):
    def f(st, od):
        for k, v in kw.items():
            (od if k in g.GATE_ORDER_FIELDS else st)[k] = v(st, od) if callable(v) else v
    return f


def _arr(name, idx, val):
    def f(st, od):
        st[name][idx] = val
    return f


def _all(*fs):
    def f(st, od):
        for x in fs:
            x(st, od)
    return f


# one mutation per check that violates only that check (on either baseline)
VIOLATE = {
    1: _set(snap_flip_us=lambda st, od: (st["now_us"] - 300_000) & M32),
    2: _set(feed_seq=lambda st, od: (st["ref_seq"] + 100) & 0xFFFF),
    3: _set(feed_bad=1),
    4: _set(S=lambda st, od: st["S_ref"] + g.JUMP_LIMIT_RAW + 1040),
    5: _set(q_ask=lambda st, od: st["q_bid"]),
    6: _set(price=lambda st, od: od["price"] + (20 if od["is_hedge"] else 15)),
    7: _set(qty=lambda st, od: 1001 if od["is_hedge"] else 11),
    8: _all(_arr("pos_conf", 0, 15), _arr("pos_pend", 0, 5)),
    9: _set(sh_pend=lambda st, od: 2001 - (od["qty"] if od["is_hedge"] else 0)),
    10: _all(_arr("gamma_c", 0, 260000), lambda st, od: st["pos_pend"].__setitem__(0, od["is_hedge"])),
    11: _all(_arr("vega", 0, VEGA_MAX), _arr("pos_pend", 0, VEGA_LIM // VEGA_MAX + 1)),
    12: _set(tokens=0),
    13: _set(cash_cents=-300_000),
    14: _set(hb_last_us=lambda st, od: (st["now_us"] - 200_000) & M32),
    15: _set(kill=1),
}
HEDGE_NA = {8}                       # R08 never applies to hedges (A19)


def gen_gate():
    t = Table("gate.csv", "risk gate: full state + one order -> reason code",
              "Independent rows (pure combinational check; latching, token refill and arming are outside). "
              "Expected = golden `gate_eval(state, order)` (A16-A21). Directed rows are asserted by the generator: "
              "single-violation rows fail exactly `target`, boundary pass rows fail nothing, two-at-once rows fail exactly "
              "{target, target2}. R10's limit (0.03 * 2^23 * 100) is not an integer, so its pair is the largest passing "
              "and smallest failing integer sum. R09/R11/R13/R14/R01/R02/R04/R05/R06/R07/R08/R12 pairs hit the limit exactly.",
              gate_cols(), GATE_KINDS)

    def add(kind, st, od, target=0, target2=0, expect=None):
        res = g.gate_eval(st, od)
        if expect is not None:
            got = [k for k in range(1, 16) if (res["fail_mask"] >> (k - 1)) & 1]
            assert got == sorted(expect), (kind, target, target2, got, expect)
        t.add(kind=kind, target=target, target2=target2, **st, **od, **res, **g.gate_exposures(st))
        return res

    def case(mut=None, hedge=False):
        st, od = gate_baseline(hedge)
        if mut:
            mut(st, od)
        return st, od

    for hedge in (False, True):
        add(0, *case(None, hedge), expect=[])
        for k, mut in VIOLATE.items():
            if hedge and k in HEDGE_NA:
                continue
            add(1, *case(mut, hedge), target=k, expect=[k])

    def pair(k, ok, bad, hedge=False):
        add(2, *case(ok, hedge), target=k, expect=[])
        add(3, *case(bad, hedge), target=k, expect=[k])

    rel = lambda d: (lambda st, od: (st["now_us"] - d) & M32)
    wrap_now = _set(now_us=100, hb_last_us=lambda st, od: (100 - 50_000) & M32)
    pair(1, _set(snap_flip_us=rel(250_000)), _set(snap_flip_us=rel(250_001)))
    pair(1, _all(wrap_now, _set(snap_flip_us=rel(250_000))), _all(wrap_now, _set(snap_flip_us=rel(250_001))))
    pair(2, _set(feed_seq=1064), _set(feed_seq=1065))
    pair(2, _set(feed_seq=1000), _set(feed_seq=999))                     # ref_seq ahead of feed
    pair(2, _set(ref_seq=65530, feed_seq=(65530 + 64) & 0xFFFF), _set(ref_seq=65530, feed_seq=(65530 + 65) & 0xFFFF))
    for sgn in (1, -1):
        pair(4, _set(S=lambda st, od, s=sgn: st["S_ref"] + s * g.JUMP_LIMIT_RAW),
             _set(S=lambda st, od, s=sgn: st["S_ref"] + s * (g.JUMP_LIMIT_RAW + 1)))
    pair(5, _set(q_ask=lambda st, od: st["q_bid"] + 20), _set(q_ask=lambda st, od: st["q_bid"] + 21))
    pair(5, _set(q_bid=1, q_ask=2), _set(q_bid=0, q_ask=1))
    pair(5, _set(q_ask=lambda st, od: st["q_bid"] + 1), _set(q_ask=lambda st, od: st["q_bid"]))
    pair(5, _set(q_bsz=1), _set(q_bsz=0))
    pair(5, _set(q_asz=1), _set(q_asz=0))
    for sgn in (1, -1):
        pair(6, lambda st, od, s=sgn: st["theo_0"].__setitem__(0, (od["price"] - s * 10) * 256),
             lambda st, od, s=sgn: st["theo_0"].__setitem__(0, (od["price"] - s * 10) * 256 - s))
    B = U
    pair(6, _set(q_bid=B, q_ask=B + 2, q_bsz=1, q_asz=1, price=B + 11),           # mp = (B+1)*16, +160
         _set(q_bid=B, q_ask=B + 1, q_bsz=15, q_asz=1, price=B + 11), hedge=True)  # mp = B*16+15, +161
    pair(6, _set(q_bid=B, q_ask=B + 2, q_bsz=1, q_asz=1, price=B - 9),            # -160
         _set(q_bid=B, q_ask=B + 1, q_bsz=1, q_asz=15, price=B - 10), hedge=True)  # mp = B*16+1, -161
    pair(7, _set(qty=10), _set(qty=11))
    pair(7, _set(qty=1000), _set(qty=1001), hedge=True)
    pair(8, _arr("pos_conf", 0, 19), _arr("pos_conf", 0, 20))
    # shorts go in pos_pend: a confirmed short with zero cash would also trip R13 (equity)
    pair(8, _all(_arr("pos_pend", 0, -19), _set(side=1)), _all(_arr("pos_pend", 0, -20), _set(side=1)))
    d9 = lambda d, p, side: _all(_arr("delta_adj", 0, d), _arr("gamma_c", 0, 1000), _arr("vega", 0, 1000),
                                 _arr("pos_pend", 0, p), _set(side=side))
    pair(9, d9(65536, 19, 0), d9(65537, 19, 0))
    pair(9, d9(65536, -19, 1), d9(65537, -19, 1))
    pair(9, _set(sh_pend=1500, qty=500), _set(sh_pend=1500, qty=501), hedge=True)
    pair(9, _set(sh_pend=-1500, qty=500, side=1), _set(sh_pend=-1500, qty=501, side=1), hedge=True)
    pair(10, _arr("gamma_c", 0, 251658), _arr("gamma_c", 0, 251659))
    # R11 exactly at the limit: order (1 x vega0) + n x VEGA_MAX on contract 1 + remainder on contract 2
    n11 = (VEGA_LIM - VEGA_MAX) // VEGA_MAX
    r11 = VEGA_LIM - VEGA_MAX - n11 * VEGA_MAX
    v11 = lambda v: _all(_arr("vega", 0, VEGA_MAX), _arr("vega", 1, VEGA_MAX), _arr("pos_conf", 1, n11),
                         _arr("vega", 2, v), _arr("pos_conf", 2, 1))
    pair(11, v11(r11), v11(r11 + 1))
    pair(12, _set(orders_in_event=7), _set(orders_in_event=8))
    pair(12, _set(tokens=1), _set(tokens=0))
    pair(13, _set(cash_cents=-200_000), _set(cash_cents=-200_001))
    pair(14, _set(hb_last_us=rel(150_000)), _set(hb_last_us=rel(150_001)))

    for a, b in [(1, 5), (2, 3), (4, 15), (6, 8), (7, 12), (9, 10), (11, 13), (13, 14), (3, 14), (1, 15), (5, 7), (14, 15)]:
        add(4, *case(_all(VIOLATE[a], VIOLATE[b])), target=a, target2=b, expect=[a, b])

    # reduce-only. Violations chosen so they still hold after the reducing fill.
    persist = dict(VIOLATE)
    persist[8] = _arr("pos_conf", 0, 25)
    persist[9] = _set(sh_pend=lambda st, od: 2500 if not od["is_hedge"] else st["sh_pend"] + 2500)
    persist[10] = _arr("gamma_c", 0, 100_000)
    persist[11] = _all(_arr("vega", 1, VEGA_MAX), _arr("pos_conf", 1, VEGA_LIM // VEGA_MAX + 1))   # holds after any reduce of contract 0
    persist[13] = _set(cash_cents=-600_000)          # the long position adds equity back
    persist[7] = _set(qty=lambda st, od: 1001 if od["is_hedge"] else 11)
    for hedge in (False, True):
        hold = _set(sh_pend=1500) if hedge else _arr("pos_conf", 0, 15)     # big enough to reduce by 11 / 1001
        base_pos = (_set(sh_pend=500) if hedge else _arr("pos_conf", 0, 4))
        red = _set(side=1)
        for k in sorted(g.REDUCE_ONLY_SKIPS):
            if hedge and k in HEDGE_NA:
                continue
            setup = _all(base_pos, _arr("pos_conf", 0, 4) if hedge and k in (10, 11) else (lambda st, od: None))
            r = add(5, *case(_all(setup, persist[k], red), hedge), target=k, expect=[])
            assert r["reduce_only"] == 1
            r = add(6, *case(_all(setup, persist[k]), hedge), target=k, expect=[k])
            assert r["reduce_only"] == 0
        for k in (5, 7, 12, 15):
            r = add(7, *case(_all(hold if k == 7 else base_pos, persist[k], red), hedge), target=k, expect=[k])
            assert r["reduce_only"] == 1
        # crossing through zero is a normal order; reducing exactly to zero is reduce-only
        cross = _all(_set(sh_pend=50, qty=100) if hedge else _all(_arr("pos_conf", 0, 3), _set(qty=5)), red, VIOLATE[1])
        assert add(8, *case(cross, hedge), target=1, expect=[1])["reduce_only"] == 0
        flat = _all(_set(sh_pend=100, qty=100) if hedge else _all(_arr("pos_conf", 0, 3), _set(qty=3)), red, VIOLATE[1])
        assert add(9, *case(flat, hedge), target=1, expect=[])["reduce_only"] == 1
        # pending counts toward the position being reduced
        if not hedge:
            split = _all(_arr("pos_conf", 0, 2), _arr("pos_pend", 0, 2), _set(qty=4), red, VIOLATE[1])
            assert add(9, *case(split), target=1, expect=[])["reduce_only"] == 1

    rng = random.Random(SEED + 5)
    for _ in range(1500):
        hedge = rng.random() < 0.3
        st, od = gate_baseline(hedge)
        if not hedge:
            od["contract"] = rng.randint(0, 7)
            od["price"] = (st["theo_0"][od["contract"]] + 128) >> 8
            st.update(q_bid=od["price"] - 1, q_ask=od["price"] + 1)
        od["side"] = rng.randint(0, 1)
        od["qty"] = rng.randint(1, 12) if not hedge else rng.randint(1, 1100)
        for i in range(8):
            st["pos_conf"][i] = rng.randint(-12, 12)
            st["pos_pend"][i] = rng.randint(-4, 4)
        st["sh_conf"], st["sh_pend"] = rng.randint(-1500, 1500), rng.randint(-300, 300)
        st["cash_cents"] = rng.randint(-250_000, 100_000)
        st["theo_new"] = [max(0, x + rng.randint(-2000, 2000)) for x in st["theo_new"]]
        jr = g.JUMP_LIMIT_RAW * 23 // 20                # straddles R04 (+/-15% beyond the limit)
        st["S"] = st["S_ref"] + rng.randint(-jr, jr)
        st["tokens"], st["orders_in_event"] = rng.randint(0, 20), rng.randint(0, 9)
        st["now_us"] = rng.randint(300_000, M32)
        st["snap_flip_us"] = (st["now_us"] - rng.randint(0, 260_000)) & M32
        st["hb_last_us"] = (st["now_us"] - rng.randint(0, 160_000)) & M32
        st["feed_seq"] = (st["ref_seq"] + rng.randint(-3, 70)) & 0xFFFF
        st["feed_bad"], st["kill"] = int(rng.random() < 0.05), int(rng.random() < 0.05)
        if rng.random() < 0.1:
            st["q_ask"] = st["q_bid"]
        od["price"] += rng.randint(-12, 12)
        add(10, st, od)
    return t


# ---------------------------------------------------------------- README + main
README_HEAD = """# Test vectors (generated - do not edit)

Generated by `golden/make_vectors.py` from `golden/golden.py` (seed {seed}). SIMULATED data.
Every expected column is computed by a golden-model function; the assumptions behind those
functions are A1-A21 at the top of `golden/golden.py`.

## Format

- One header line of column names, then one row per line. Comma separated, `\\n` line endings.
- Every value is lowercase hex, zero-padded to ceil(bits / 4) digits, no `0x` prefix.
- Signed columns are two's complement at the stated bit width (e.g. 18-bit `delta_adj` = -1 -> `3ffff`).
  Sign-extend from that width in the testbench.
- Array columns are written `name0 .. name7` (or `.. name15`), index 0 first.

Reading a file in SystemVerilog (Questa):

```systemverilog
int fd = $fopen("docs/vectors/reprice.csv", "r");
string hdr; void'($fgets(hdr, fd));            // skip the header line
while (!$feof(fd)) begin
  if ($fscanf(fd, "%h,%h,%h,%h,%h,%h,%h,%h,%h,%h,%h\\n",
              kind, s, s_ref, ds, theo0, dadj, gam, p1, g1, p2, theo_new) != 11) break;
  // drive inputs, compare outputs
end
```

Declare each variable at least as wide as its column (signed columns: declare `logic signed [N-1:0]`
at the column width so `%h` fills the two's complement pattern directly).

"""


def build_tables():
    return [gen_parser(), gen_microprice(), gen_reprice(), gen_trigger(), gen_gate()]


def main(out_dir=OUT_DIR):
    os.makedirs(out_dir, exist_ok=True)
    tables = build_tables()
    for tb in tables:
        tb.write(out_dir)
    with open(os.path.join(out_dir, "README.md"), "w", newline="\n") as f:
        f.write(README_HEAD.format(seed=SEED))
        f.write("\n".join(tb.readme() for tb in tables))
    for tb in tables:
        print(f"{tb.fname:16s} {len(tb.rows):6d} rows")
    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else OUT_DIR)
