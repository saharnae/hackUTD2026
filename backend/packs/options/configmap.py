"""0x12 config fields <-> engine parameters, the tunable allowlist, and the hardware-limit names.

TUNABLE allowlist (TODO(FINAL)): derived, not typed in. It is exactly the fields of the 0x12 config
decoder (slowpath/frames.py CONFIG_FIELDS, docs/FRAMES.md "soft strategy parameters only"), with
their accepted ranges converted to engine units. GATE_SPEC.md / the project context say the
gate limits R01-R15 are compile-time constants that no frame can change; those names are listed
in HARDWARE_LIMITS and are always refused.

ASSUMPTIONS:
  CM1. hedge_band_shares is NOT tunable through a proposal: it is a laptop-side hedger parameter
       and has no field in the 0x12 frame. TODO(FINAL).
  CM2. Q8 fields round half up: field = floor(x * 256 + 0.5) (project rounding rule).
  CM3. A proposal may move a field anywhere inside the decoder's accepted range. Whether, e.g.,
       raising size_cap_* should also need extra sign-off is TODO(FINAL).
"""

import math

from ... import _paths  # noqa: F401
import frames as F
import golden as g

# frame field -> (engine parameter, scale, python type)
FIELD_MAP = {
    "edge_threshold": ("edge_threshold_ticks", 256, float),
    "exit_edge": ("exit_edge_ticks", 256, float),
    "min_move": ("min_move_cents", 1, int),
    "max_orders_per_event": ("max_orders_per_event", 1, int),
    "size_cap_base": ("size_cap_base", 1, int),
    "size_cap_max": ("size_cap_max", 1, int),
    "time_stop_us": ("time_stop_us", 1, int),
}
assert set(FIELD_MAP) == {n for n, *_ in F.CONFIG_FIELDS}, "0x12 decoder fields changed: update FIELD_MAP"

TUNABLE = {}                                   # engine param -> {"field", "lo", "hi", "default", "type", "scale"}
for _name, _nb, _dflt, _lo, _hi in F.CONFIG_FIELDS:
    _p, _sc, _ty = FIELD_MAP[_name]
    TUNABLE[_p] = {"field": _name, "scale": _sc, "type": "number" if _ty is float else "integer",
                   "lo": _lo / _sc if _sc != 1 else _lo, "hi": _hi / _sc if _sc != 1 else _hi,
                   "default": _dflt / _sc if _sc != 1 else _dflt}
TUNABLE_PARAMS = tuple(TUNABLE)                # TODO(FINAL)

# Gate limits (GATE_SPEC.md section 3, values from golden.py). Lower-case names and common aliases.
_HW = [
    ("R01", "stale snapshot", g.STALE_US, ("stale_us", "max_snapshot_age", "max_snapshot_age_us")),
    ("R02", "snapshot sync", g.SYNC_MAX_BEHIND, ("sync_max_behind",)),
    ("R04", "jump guard", g.JUMP_LIMIT_CENTS, ("jump_limit", "jump_limit_cents", "jump_limit_raw")),
    ("R05", "quote sanity", g.MAX_SPREAD_TICKS, ("max_spread_ticks", "max_spread")),
    ("R06", "price band", g.PRICE_BAND_TICKS, ("price_band_ticks", "price_band", "hedge_band_cents")),
    ("R07", "max order size", g.MAX_ORDER_QTY, ("max_order_qty", "max_hedge_qty")),
    ("R08", "max position", g.MAX_POSITION, ("max_position",)),
    ("R09", "net delta", g.NET_DELTA_LIMIT, ("net_delta_limit", "max_delta", "net_delta")),
    ("R10", "net gamma", g.NET_GAMMA_LIMIT_X100 / 100.0, ("net_gamma_limit", "net_gamma_limit_x100", "max_gamma")),
    ("R11", "net vega", g.NET_VEGA_LIMIT, ("net_vega_limit", "max_vega", "net_vega")),
    ("R12", "order rate", g.MAX_GATE_ORDERS_PER_EVENT,
     ("max_gate_orders_per_event", "token_bucket", "tokens", "token_refill_us", "max_orders_per_s")),
    ("R13", "drawdown", g.DRAWDOWN_LIMIT_DOLLARS, ("drawdown_limit", "drawdown_limit_dollars", "max_drawdown")),
    ("R14", "heartbeat", g.HEARTBEAT_US, ("heartbeat_us", "heartbeat_timeout_us")),
    ("R15", "manual kill", None, ("kill", "kill_pin")),
]
HARDWARE_LIMITS = {}
for _rule, _label, _val, _names in _HW:
    for _n in _names:
        HARDWARE_LIMITS[_n] = {"rule": _rule, "name": _label, "value": _val}

RULE_NAMES = {f"R{int(r[1:]):02d}": lbl for r, lbl, _, _ in _HW}
RULE_NAMES["R03"] = "feed integrity"
LATCHING_RULES = (3, 12, 13, 14, 15)


def rule_id(code):
    return f"R{int(code):02d}" if code else None


def _round_half_up(x):
    return int(math.floor(x + 0.5))


def to_fields(params):
    """Engine params (all 7 tunables) -> 0x12 field integers."""
    out = {}
    for field, (p, sc, ty) in FIELD_MAP.items():
        v = params[p]
        out[field] = _round_half_up(v * sc) if sc != 1 else int(v)
    return out


def from_fields(fields):
    out = {}
    for field, (p, sc, ty) in FIELD_MAP.items():
        out[p] = fields[field] / sc if sc != 1 else int(fields[field])
    return out


def config_payload(params):
    """Engine params -> validated 10-byte 0x12 payload (raises ValueError if out of range)."""
    payload = F.encode_config(to_fields(params))
    F.decode_config(payload)
    return payload


def quantize(params):
    """What the board would actually run: engine params after the 0x12 round trip."""
    return from_fields(F.decode_config(config_payload(params)))


def check_change(changes):
    """Problems with a proposed change (engine-unit keys). [] = acceptable. Never raises."""
    problems = []
    if not isinstance(changes, dict) or not changes:
        return [{"key": None, "kind": "empty", "reason": "a proposal must change at least one tunable parameter"}]
    for k, v in changes.items():
        key = str(k).lower()
        if key in HARDWARE_LIMITS:
            hw = HARDWARE_LIMITS[key]
            problems.append({"key": k, "kind": "hardware_limit",
                             "reason": f"{k} is a hardware limit ({hw['rule']} {hw['name']}), a compile-time constant; "
                                       "no frame or agent can change it"})
            continue
        if k not in TUNABLE:
            problems.append({"key": k, "kind": "not_tunable",
                             "reason": f"{k} is not in the 0x12 tunable allowlist {list(TUNABLE_PARAMS)}"})
            continue
        t = TUNABLE[k]
        ok_type = (isinstance(v, (int, float)) and not isinstance(v, bool)
                   and (t["type"] == "number" or (isinstance(v, int) or float(v).is_integer())))
        if not ok_type or (isinstance(v, float) and not math.isfinite(v)):
            problems.append({"key": k, "kind": "bad_type", "reason": f"{k} must be a finite {t['type']}"})
            continue
        if not t["lo"] <= v <= t["hi"]:
            problems.append({"key": k, "kind": "out_of_range",
                             "reason": f"{k} = {v} outside the accepted range {t['lo']}..{t['hi']}"})
    return problems


def merged_problems(current, changes):
    """check_change, plus cross-field rules on the merged set (size_cap_max >= size_cap_base)."""
    probs = check_change(changes)
    if probs:
        return probs
    merged = dict(current, **changes)
    if merged["size_cap_max"] < merged["size_cap_base"]:
        return [{"key": "size_cap_max", "kind": "out_of_range", "reason": "size_cap_max must be >= size_cap_base"}]
    try:
        config_payload(merged)
    except ValueError as e:
        return [{"key": None, "kind": "out_of_range", "reason": f"0x12 decoder refused: {e}"}]
    return []
