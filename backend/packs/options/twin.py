"""Software twin: run golden/engine.py over a PREFIX of a seeded sim scenario, then read its state.

ASSUMPTIONS (TW):
  TW1. Prefix replay. The state at twin time t is a fresh Engine run over the scenario messages with
       time <= t. Pure and deterministic per (seed, regime, params, t, kill time). Cost grows with t
       (a full 600 ms session is ~0.15 s); results are memoized.
  TW2. At the end of a prefix run the engine resolves orders still in flight (engine.run does this);
       an order sent within order_latency_us (10 us twin timing) of t is therefore resolved against
       quotes up to 10 us after t. Accepted look-ahead. TODO(FINAL).
  TW3. Kill: the KILL frame time is passed as engine.run(kill_at_us=...). If no feed message is left
       after it (session over), set_kill() is applied after the run so the latch still shows.
  TW4. 0x20 fields the twin does not model are sent as 0 and listed in UNMODELED_STATUS:
       last_reject (the engine counts rejects but not their order), snapshot_id, last_latency_ns
       (twin timing is never FPGA latency), frames_bad, log_drops, shock_busy.
  TW5. 0x21 records: one "decision" record (rec_type 1, approved) per engine "send" and one "fill"
       record (rec_type 2) per fill or miss (miss = qty 0). Rejected orders are not logged by the
       engine, so no rejected decision records exist. reduce_only, fail_mask, tokens and
       orders_in_event in the records are 0 (not tracked per order). TODO(FINAL).
  TW6. Engine arming (E13) stands in for the operator raising ARM. Only a human "new_session"
       (twin power cycle) restarts it; no agent path reaches that.
"""

import bisect
import json
from collections import OrderedDict

from ... import _paths  # noqa: F401
import engine as EN
import golden as g
import options_model as om

UNMODELED_STATUS = ("last_reject", "snapshot_id", "last_latency_ns", "frames_bad", "log_drops", "shock_busy")
DURATION_US = om.CONFIG["duration_us"]

_SCEN = OrderedDict()
_RUNS = OrderedDict()
_MAX_SCEN, _MAX_RUNS = 16, 64


def scenario(seed, regime):
    k = (seed, regime)
    if k not in _SCEN:
        _SCEN[k] = om.OptionsMarketScenario(seed, regime)
        if len(_SCEN) > _MAX_SCEN:
            _SCEN.popitem(last=False)
    _SCEN.move_to_end(k)
    return _SCEN[k]


class _Prefix:
    """The scenario with messages cut at t_end (everything else unchanged)."""

    def __init__(self, sc, t_end):
        if not hasattr(sc, "_la_times"):
            sc._la_times = [m["t"] for m in sc.messages]
        n = bisect.bisect_right(sc._la_times, t_end)
        self.messages = sc.messages[:n]
        self.quote_hist, self.und, self.rival_hits = sc.quote_hist, sc.und, sc.rival_hits
        self.rogue_burst, self.S_ref = sc.rogue_burst, sc.S_ref


def run_prefix(seed, regime, params, t_us, kill_t_us=None):
    key = (seed, regime, json.dumps(params, sort_keys=True), int(t_us), kill_t_us)
    if key in _RUNS:
        _RUNS.move_to_end(key)
        return _RUNS[key]
    sc = scenario(seed, regime)
    e = EN.Engine(params)
    res = e.run(_Prefix(sc, t_us), kill_at_us=kill_t_us)
    if kill_t_us is not None and not e.kill:                          # TW3
        e._now = t_us
        e.set_kill(t_us)
        res = e.result()
    e.t_us = int(t_us)
    e.res = res
    _RUNS[key] = e
    if len(_RUNS) > _MAX_RUNS:
        _RUNS.popitem(last=False)
    return e


# ---------------------------------------------------------------- readouts
def equity_x6400(e):
    if e.snap is None or e.S_q4 is None:
        return 64 * e.cash
    tn = e.theo_new()
    return 64 * e.cash + 25 * sum(p * x for p, x in zip(e.pos_conf, tn)) + 4 * e.sh_conf * e.S_q4


def status_fields(e, config_rejects):
    t = e.t_us
    book_ready = e.und_q is not None and all(e.opt_q)
    snap_ok = (e.snap is not None and e.last_seq is not None and (t - e.snap_flip) <= g.STALE_US
               and ((e.last_seq - e.ref_seq) & 0xFFFF) <= g.SYNC_MAX_BEHIND)
    hb_ok = e.hb_count >= 2 and (t - e.hb_last) <= g.HEARTBEAT_US
    bits = [e.armed and not e.latched, e.latched, book_ready, snap_ok, hb_ok, False, e.armed, e.kill]
    state = sum(int(bool(b)) << i for i, b in enumerate(bits))
    fills = sum(1 for f in e.fills if f["purpose"] != "flatten" and f["qty"] != 0)
    approved = sum(1 for x in e.log if x[0] == "send")   # FRAMES 0x20: approved orders (engine.orders_sent
    #                                                      counts every gate evaluation, rejected ones too)
    return {"now_us": t, "state": state, "trip_reason": e.latch_reasons[0]["reason"] if e.latch_reasons else 0,
            "last_reject": 0, "tokens": max(0, min(20, e.tokens)), "snapshot_id": 0,
            "ref_seq": e.ref_seq & 0xFFFF, "feed_seq": (e.last_seq or 0) & 0xFFFF, "S": e.S_q4 or 0,
            "equity_x6400": equity_x6400(e), "orders_sent": approved & 0xFFFF, "fills": fills & 0xFFFF,
            "last_latency_ns": 0, "frames_bad": 0, "config_rejects": min(config_rejects, 255), "log_drops": 0}


def log_records(e, t_after=-1.0):
    """0x21 records for engine log entries with t_after < t <= e.t_us (TW5)."""
    out = []
    for ent in e.log:
        kind, t = ent[0], ent[1]
        if not (t_after < t <= e.t_us):
            continue
        base = {"ts_us": int(t), "reason": 0, "fail_mask": 0, "client_id": 0, "tokens": 0, "orders_in_event": 0}
        if kind == "send":
            _, _, purpose, is_hedge, c, side, qty, price = ent
            out.append(dict(base, rec_type=1, is_hedge=is_hedge, side=side, contract=0 if is_hedge else c,
                            qty=qty, price=price, flags=1))
        elif kind == "fill":
            if ent[2] == "hedge":
                sq, price = ent[3], ent[4]
                out.append(dict(base, rec_type=2, is_hedge=1, side=0 if sq > 0 else 1, contract=0, qty=abs(sq),
                                price=price, flags=0))
            else:
                c, sq, price = ent[2], ent[3], ent[4]
                out.append(dict(base, rec_type=2, is_hedge=0, side=0 if sq > 0 else 1, contract=c, qty=abs(sq),
                                price=price, flags=0))
        elif kind == "miss":
            if ent[2] == "hedge":
                out.append(dict(base, rec_type=2, is_hedge=1, side=0, contract=0, qty=0, price=0, flags=0))
            else:
                out.append(dict(base, rec_type=2, is_hedge=0, side=ent[3], contract=ent[2], qty=0, price=0, flags=0))
    return out


def channels(e):
    """Gate-limit channels, every one "higher is worse" (pack convention PK1)."""
    t = e.t_us
    eq = equity_x6400(e) / 6400.0
    ch = {"loss_dollars": round(max(0.0, -eq), 6), "tokens_used": 20 - max(0, min(20, e.tokens)),
          "heartbeat_age_us": max(0, t - int(e.hb_last)) if e.hb_count else None}
    if e.snap is None or e.S_q4 is None:
        ch.update(net_delta=None, jump_cents=None, snapshot_age_us=None, max_abs_position=None,
                  net_vega_dollars=None, net_gamma=None)
        return ch
    cs = e.snap["contracts"]
    pos = [e.pos_conf[c] + e.pos_pend[c] for c in range(8)]
    ch.update(
        net_delta=round(abs(e.net_delta_shares()) / 100.0, 6),
        jump_cents=round(abs(e.S_q4 - e.snap["S_ref"]) / 16.0, 4),
        snapshot_age_us=max(0, int(t - e.snap_flip)),
        max_abs_position=max(abs(p) for p in pos),
        net_vega_dollars=round(abs(sum(p * c["vega"] for p, c in zip(pos, cs))) / 256.0, 6),
        net_gamma=round(abs(sum(p * c["gamma_c"] for p, c in zip(pos, cs))) / float(1 << 23), 8),
    )
    return ch


def summary(e):
    return {"t_us": e.t_us, "armed": bool(e.armed and not e.latched), "tripped": bool(e.latched),
            "trip_reason": e.latch_reasons[0]["reason"] if e.latch_reasons else 0,
            "trip_t_us": round(e.latch_reasons[0]["t_us"], 3) if e.latch_reasons else None,
            "kill": bool(e.kill), "net_pnl": e.res["net_pnl"], "fills": e.res["fills"],
            "orders_sent": e.orders_sent, "position": list(e.pos_conf), "shares": e.sh_conf,
            "session_over": e.t_us >= DURATION_US}
