"""Slow-path snapshot builder. SIMULATED market parameters (placeholders from golden.py).

All pricing and quantization comes from golden/golden.py (imported, not copied).

ASSUMPTIONS:
  S1. build_snapshot() takes S_ref in dollars; it is quantized to the 21-bit S format
      (cents, 4 fraction bits) and that quantized value is what gets priced and sent.
  S2. Snapshot payload (95 bytes), all fields big-endian:
        snapshot_id u16, ref_seq u16, S_ref u24 (21-bit value, top 3 bits zero),
        then 8 x [theo_0 u24 (18-bit), delta_adj s24 (18-bit two's complement,
        sign-extended to 24 bits), gamma_c u24 (18-bit), vega u16].
      Contract order is index 0..7 (4 * is_put + strike index).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "golden"))
import golden as g  # noqa: E402

PAYLOAD_LEN = 95
_FIELDS = ("theo_0", "delta_adj", "gamma_c", "vega")


def build_snapshot(S_ref_dollars, ref_seq, snapshot_id, **model_kwargs):
    """Quantized snapshot for the 8 contracts.

    Returns {"snapshot_id", "ref_seq", "S_ref" (raw 21-bit),
             "contracts": [{"theo_0", "delta_adj", "gamma_c", "vega"} x 8]}.
    model_kwargs pass through to golden.build_snapshot (T, r, q, smile, lam, beta).
    """
    if not 0 <= snapshot_id < 1 << 16 or not 0 <= ref_seq < 1 << 16:
        raise ValueError("snapshot_id and ref_seq are 16-bit")
    snap = g.build_snapshot(S_ref_dollars=S_ref_dollars, **model_kwargs)
    return {
        "snapshot_id": snapshot_id,
        "ref_seq": ref_seq,
        "S_ref": snap["S_ref"],
        "contracts": [{k: c[k] for k in _FIELDS} for c in snap["contracts"]],
    }


def _u(value, nbytes, fmt):
    if not fmt.lo <= value <= fmt.hi:
        raise ValueError(f"{fmt.name} out of range: {value}")
    return (value & ((1 << (8 * nbytes)) - 1)).to_bytes(nbytes, "big")


def to_payload(snap):
    """Snapshot dict -> 95-byte payload (S2)."""
    out = bytearray()
    out += snap["snapshot_id"].to_bytes(2, "big")
    out += snap["ref_seq"].to_bytes(2, "big")
    out += _u(snap["S_ref"], 3, g.FMT_S)
    if len(snap["contracts"]) != 8:
        raise ValueError("need 8 contracts")
    for c in snap["contracts"]:
        out += _u(c["theo_0"], 3, g.FMT_THEO)
        out += _u(c["delta_adj"], 3, g.FMT_DELTA_ADJ)      # sign-extended to 24 bits
        out += _u(c["gamma_c"], 3, g.FMT_GAMMA_C)
        out += _u(c["vega"], 2, g.FMT_VEGA)
    assert len(out) == PAYLOAD_LEN
    return bytes(out)


def _field(raw, nbytes, fmt):
    if fmt.signed:
        raw -= (raw >> (8 * nbytes - 1)) << (8 * nbytes)   # 24-bit two's complement
    if not fmt.lo <= raw <= fmt.hi:
        raise ValueError(f"{fmt.name} out of range: {raw}")
    return raw


def from_payload(payload):
    """95-byte payload -> snapshot dict. Raises ValueError on bad length or range."""
    if len(payload) != PAYLOAD_LEN:
        raise ValueError(f"snapshot payload must be {PAYLOAD_LEN} bytes, got {len(payload)}")
    rd = lambda off, n: int.from_bytes(payload[off:off + n], "big")  # noqa: E731
    snap = {"snapshot_id": rd(0, 2), "ref_seq": rd(2, 2),
            "S_ref": _field(rd(4, 3), 3, g.FMT_S), "contracts": []}
    off = 7
    for _ in range(8):
        c = {}
        for key, n, fmt in (("theo_0", 3, g.FMT_THEO), ("delta_adj", 3, g.FMT_DELTA_ADJ),
                            ("gamma_c", 3, g.FMT_GAMMA_C), ("vega", 2, g.FMT_VEGA)):
            c[key] = _field(rd(off, n), n, fmt)
            off += n
        snap["contracts"].append(c)
    return snap
