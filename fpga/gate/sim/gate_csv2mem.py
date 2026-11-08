#!/usr/bin/env python3
"""gate_csv2mem.py - convert docs/vectors/gate.csv into a $readmemh file for tb_gate.sv.

Outputs (default: next to this script):
  gate_vectors.mem    one packed hex word per CSV row (row order kept, row i = vector i)
  gate_vec_pkg.svh    generated SystemVerilog: the packed struct layout of one word + N_VEC

The struct is generated from FIELDS below, so the .mem layout and the testbench can't drift.
Every field is padded to a whole number of hex digits (pad bits are zero, above the field's
MSB) so each field is just a slice of the hex string.

The gate's *_c ports are per-contract values muxed by `contract` upstream of the gate
(GATE_SPEC.md section 2.3). The converter does that mux: theo_0_c = theo_0[contract], etc.,
and pos_c = pos_conf[contract] + pos_pend[contract] (9-bit signed). Nothing else is computed
here: every other field, including every expected output, is copied straight from the CSV.

Usage:  python fpga/gate/sim/gate_csv2mem.py [--csv docs/vectors/gate.csv] [--out DIR]
"""
import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))

# (struct field name, width, signed, source).  source = CSV column name, or a callable(row) -> int.
def _arr(name):
    return lambda r: r[f"{name}{int(r['contract'], 16)}"]


def _sx(hexstr, bits):
    v = int(hexstr, 16)
    return v - (1 << bits) if v >> (bits - 1) & 1 else v


def _pos_c(r):
    c = int(r["contract"], 16)
    return _sx(r[f"pos_conf{c}"], 8) + _sx(r[f"pos_pend{c}"], 8)


FIELDS = [
    # identity / labels
    ("kind",            4,  False, "kind"),
    ("target",          4,  False, "target"),
    ("target2",         4,  False, "target2"),
    ("contract",        3,  False, "contract"),
    # order
    ("is_hedge",        1,  False, "is_hedge"),
    ("side",            1,  False, "side"),
    ("qty",             16, False, "qty"),
    ("price",           18, False, "price"),
    # state
    ("now_us",          32, False, "now_us"),
    ("snap_flip_us",    32, False, "snap_flip_us"),
    ("ref_seq",         16, False, "ref_seq"),
    ("feed_seq",        16, False, "feed_seq"),
    ("feed_bad",        1,  False, "feed_bad"),
    ("S",               21, False, "S"),
    ("S_ref",           21, False, "S_ref"),
    ("q_bid",           18, False, "q_bid"),
    ("q_ask",           18, False, "q_ask"),
    ("q_bsz",           12, False, "q_bsz"),
    ("q_asz",           12, False, "q_asz"),
    ("mp_q4",           24, False, "mp_q4"),
    ("theo_0_c",        18, False, _arr("theo_0")),
    ("delta_adj_c",     18, True,  _arr("delta_adj")),
    ("gamma_c_c",       18, False, _arr("gamma_c")),
    ("vega_c",          16, False, _arr("vega")),
    ("pos_c",           9,  True,  _pos_c),
    ("sh",              17, True,  "sh"),
    ("sum_delta",       32, True,  "sum_delta"),
    ("sum_gamma",       32, True,  "sum_gamma"),
    ("sum_vega",        32, True,  "sum_vega"),
    ("equity_x6400",    48, True,  "equity_x6400"),
    ("hb_last_us",      32, False, "hb_last_us"),
    ("kill",            1,  False, "kill"),
    ("tokens",          5,  False, "tokens"),
    ("orders_in_event", 4,  False, "orders_in_event"),
    # expected (golden gate_eval)
    ("exp_reduce_only", 1,  False, "reduce_only"),
    ("exp_fail_mask",   15, False, "fail_mask"),
    ("exp_reason",      4,  False, "reason"),
]


def field_value(row, width, signed, src):
    # A CSV hex string is already the two's complement pattern at this width (column widths
    # are checked in main()). Callables may return a negative int, encoded here.
    v = src(row) if callable(src) else row[src]
    if isinstance(v, str):
        v = int(v, 16)
    if v < 0:
        if v < -(1 << (width - 1)):
            raise ValueError(f"{v} does not fit {width}-bit signed")
        v &= (1 << width) - 1
    if v >> width:
        raise ValueError(f"value {v:#x} does not fit {width} bits")
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(REPO, "docs", "vectors", "gate.csv"))
    ap.add_argument("--out", default=HERE)
    a = ap.parse_args()

    with open(a.csv, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("no rows in " + a.csv)

    # sanity: CSV signed columns use the same widths as our ports, so their hex already
    # is the two's complement pattern; check the column widths we rely on.
    for name, w, signed, src in FIELDS:
        if not callable(src):
            digits = len(rows[0][src])
            if digits != (w + 3) // 4:
                sys.exit(f"column {src}: {digits} hex digits, expected {(w + 3) // 4} for {w} bits")

    lines = []
    for i, r in enumerate(rows):
        parts = []
        for name, w, signed, src in FIELDS:
            try:
                v = field_value(r, w, signed, src)
            except ValueError as e:
                sys.exit(f"row {i} field {name}: {e}")
            parts.append(f"{v:0{(w + 3) // 4}x}")
        lines.append("".join(parts))

    total_bits = sum(4 * ((w + 3) // 4) for _, w, _, _ in FIELDS)
    os.makedirs(a.out, exist_ok=True)
    mem = os.path.join(a.out, "gate_vectors.mem")
    with open(mem, "w", newline="\n") as f:
        f.write(f"// generated by gate_csv2mem.py from {os.path.relpath(a.csv, REPO)}; {len(rows)} rows,"
                f" {total_bits} bits each. Do not edit.\n")
        f.write("\n".join(lines) + "\n")

    svh = os.path.join(a.out, "gate_vec_pkg.svh")
    with open(svh, "w", newline="\n") as f:
        f.write("// generated by gate_csv2mem.py - do not edit. Layout of one gate_vectors.mem word.\n")
        f.write(f"localparam int N_VEC  = {len(rows)};\n")
        f.write(f"localparam int VEC_W  = {total_bits};\n")
        f.write("typedef struct packed {\n")
        for name, w, signed, _ in FIELDS:
            pad = 4 * ((w + 3) // 4) - w
            if pad:
                f.write(f"    logic [{pad - 1}:0] _pad_{name};\n")
            s = " signed" if signed else ""
            f.write(f"    logic{s} [{w - 1}:0] {name};\n")
        f.write("} gate_vec_t;\n")

    print(f"wrote {len(rows)} vectors ({total_bits} bits) -> {os.path.relpath(mem, REPO)}, "
          f"{os.path.relpath(svh, REPO)}")


if __name__ == "__main__":
    main()
