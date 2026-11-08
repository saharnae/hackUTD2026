"""Latency Arena software-twin engine (SIMULATED arena): position keeper, fill model, exits,
hedging, P&L and gate integration around the bit-exact golden functions.

Nothing in golden.py is changed; this module only calls it (reprice, microprice_q4, gate_eval,
build_snapshot, quote_sane, edge_buy/edge_sell). Every guessed value is TODO(FINAL) and is
listed in docs/ENGINE_ASSUMPTIONS.md (E1-E22) and docs/PLACEHOLDERS.md.
Any timing in here is "twin timing" (simulated microseconds), never FPGA latency.

Input: a scenario object with the sim/options_model.py interface
  .messages (feed order), .quote_hist (per contract [(t, quote)]), .und (underlying path),
  .rival_hits, .rogue_burst, .S_ref.
"""

import bisect
import hashlib
import json
from fractions import Fraction

import golden as g

LATCHING = (3, 12, 13, 14, 15)                  # README: latching trips
TICK_CENTS = 100                                # 1 tick = $1 per contract = 100 cents (README units)

DEFAULTS = {
    # strategy (search space lives in slowpath/tuning/tune.py)
    "edge_threshold_ticks": g.EDGE_THRESHOLD_TICKS,      # 1.5  TODO(FINAL)
    "min_move_cents": g.MIN_MOVE_CENTS,                  # 2, cents  TODO(FINAL)
    "max_orders_per_event": g.MAX_ORDERS_PER_EVENT,      # 2  TODO(FINAL)
    "size_cap_base": g.SIZE_CAP_BASE,                    # 5  TODO(FINAL)
    "size_cap_max": g.SIZE_CAP_MAX,                      # 10  TODO(FINAL)
    "time_stop_us": 5000,                                # max hold (README time_stop)  TODO(FINAL)
    "exit_edge_ticks": 0.0,                              # E10: exit when entry edge <= 0  TODO(FINAL)
    "hedge_band_shares": g.HEDGE_BAND_SHARES,            # None: must be passed (200-1000)  TODO(FINAL)
    # twin mechanics (guesses)
    "order_latency_us": 10.0,                            # E4 twin timing, send -> arrival  TODO(FINAL)
    "snapshot_interval_us": 100_000,                     # README snapshot interval  TODO(FINAL)
    "heartbeat_interval_us": 50_000,                     # FRAMES.md 0x11 every 50 ms  TODO(FINAL)
    "fee_ticks_per_contract": g.FEE_TICKS_PER_CONTRACT,  # 0.5  TODO(FINAL)
}


def trigger_p(theo_new, bid, ask, bid_size, ask_size, quote_ok, lockout, pos,
              threshold, cap_base, cap_max, max_orders):
    """golden.trigger() with the soft parameters passed in (golden uses module constants).
    Identical to golden.trigger at the defaults (tested). Edge-cap step = 2 * threshold (A14)."""
    cands = []
    for c in range(8):
        for side in (0, 1):
            e = 2 * c + side
            if side == 0:
                edge, disp, room, price = g.edge_buy(theo_new[c], ask[c]), ask_size[c], g.MAX_POSITION - pos[c], ask[c]
            else:
                edge, disp, room, price = g.edge_sell(theo_new[c], bid[c]), bid_size[c], g.MAX_POSITION + pos[c], bid[c]
            if edge <= threshold or not (quote_ok >> c) & 1 or (lockout >> e) & 1 or disp == 0:
                continue
            cap = cap_max if edge > 2 * threshold else cap_base
            size = min(disp, cap, g.MAX_ORDER_QTY, max(room, 0))
            if size:
                cands.append((-edge, e, {"contract": c, "side": side, "size": size, "price": price, "edge": edge}))
    cands.sort(key=lambda t: (t[0], t[1]))
    return [d for _, _, d in cands[:max_orders]]


class Engine:
    """One simulated session. Construct, then run(scenario). Latches are never cleared by
    the engine itself: only local_rearm() (the explicit operator path) clears them."""

    def __init__(self, params, start_cash_cents=0):
        p = dict(DEFAULTS, **(params or {}))
        if p["hedge_band_shares"] is None:
            raise ValueError("hedge_band_shares has no confirmed default: pass a value in 200..1000")
        lo, hi = g.HEDGE_BAND_SHARES_RANGE
        if not lo <= p["hedge_band_shares"] <= hi:
            raise ValueError(f"hedge_band_shares {p['hedge_band_shares']} outside {lo}..{hi}")
        self.p = p
        self.thr = round(p["edge_threshold_ticks"] * 256)
        self.fee_cents = round(p["fee_ticks_per_contract"] * TICK_CENTS)
        self.start_cash = start_cash_cents
        self.cash = start_cash_cents                       # integer cents
        self.pos_conf, self.pos_pend = [0] * 8, [0] * 8
        self.sh_conf, self.sh_pend = 0, 0
        # average-cost books for realized P&L (exact Fractions, cents per unit)
        self.avg = [Fraction(0)] * 8
        self.sh_avg = Fraction(0)
        self.realized = Fraction(0)
        self.entry_t = [None] * 8
        # gate / arming state
        self.latched, self.latch_reasons, self.kill = False, [], False
        self.armed = False
        self.tokens, self.refills_done = 20, 0                    # README R12: 20 burst
        self.feed_bad, self.last_seq, self.hb_count, self.hb_last = False, None, 0, 0
        self.snap, self.snap_flip, self.ref_seq, self.next_snap = None, 0, 0, None
        self.S_q4 = None
        self.und_q = None                                  # last displayed underlying quote
        self.opt_q = [None] * 8                            # last displayed option quotes
        self.last_sane = [None] * 9                        # last sane quote per instrument (8 = underlying)
        self.S_eval = None
        self.pending = []                                  # in-flight orders
        self.consumed = {}                                 # (c, side, quote_t) -> contracts we took
        self.fills, self.rejects, self.log = [], {}, []
        self.triggers, self.events, self.orders_sent = 0, 0, 0
        self.eq_peak, self.max_dd, self.equity_curve_len = 0.0, 0.0, 0
        self.max_abs_net_delta = 0.0
        self._tn_key, self._tn = None, None

    # ------------------------------------------------------------ marks and exposures
    def theo_new(self):
        sn = self.snap
        key = (id(sn), self.S_q4)
        if key != self._tn_key:                                     # cache: same snapshot and S
            ds = g.ds_raw(self.S_q4, sn["S_ref"])
            self._tn = [g.reprice(c["theo_0"], c["delta_adj"], c["gamma_c"], ds) for c in sn["contracts"]]
            self._tn_key = key
        return list(self._tn)

    def marked_value(self):
        """Exact value of open positions in cents: options at theo_new (mark-to-model), shares at S."""
        if self.snap is None or self.S_q4 is None:
            return Fraction(0)
        tn = self.theo_new()
        v = sum(Fraction(self.pos_conf[c] * tn[c] * TICK_CENTS, 256) for c in range(8))
        return v + Fraction(self.sh_conf * self.S_q4, 16)

    def unrealized(self):
        if self.snap is None or self.S_q4 is None:
            return Fraction(0)
        tn = self.theo_new()
        u = sum(self.pos_conf[c] * (Fraction(tn[c] * TICK_CENTS, 256) - self.avg[c]) for c in range(8))
        return u + self.sh_conf * (Fraction(self.S_q4, 16) - self.sh_avg)

    def pnl_cents(self):
        return self.realized + self.unrealized()

    def accounting_identity(self):
        """(cash + marked positions - starting cash, realized + unrealized), both exact cents."""
        return Fraction(self.cash) + self.marked_value() - self.start_cash, self.pnl_cents()

    def net_delta_shares(self, pending=True):
        """Gate R09 quantity in shares: 100 * sum(pos * delta_adj) + spy_shares (A16/A17)."""
        if self.snap is None:
            return 0.0
        pos = [self.pos_conf[c] + (self.pos_pend[c] if pending else 0) for c in range(8)]
        d = sum(p * c["delta_adj"] for p, c in zip(pos, self.snap["contracts"]))
        return d * 100 / 65536 + self.sh_conf + (self.sh_pend if pending else 0)

    # ------------------------------------------------------------ position keeper
    def _book_trade(self, kind, c, signed_qty, price_cents_per_unit, fee_cents):
        """Average-cost accounting. kind 'opt' (c = contract) or 'sh'. Cash is integer cents."""
        q = signed_qty
        self.cash -= q * price_cents_per_unit + fee_cents
        self.realized -= fee_cents
        pos = self.pos_conf[c] if kind == "opt" else self.sh_conf
        avg = self.avg[c] if kind == "opt" else self.sh_avg
        px = Fraction(price_cents_per_unit)
        if pos == 0 or (pos > 0) == (q > 0):                       # open / add
            new = pos + q
            avg = (avg * pos + px * q) / new
        else:                                                       # reduce, maybe flip
            close = min(abs(q), abs(pos)) * (1 if q > 0 else -1)
            self.realized += -close * (px - avg)
            new = pos + q
            if new == 0:
                avg = Fraction(0)
            elif (new > 0) != (pos > 0):
                avg = px
        if kind == "opt":
            if pos == 0 and new != 0 or (new != 0 and (new > 0) != (pos > 0)):
                self.entry_t[c] = self._now
            elif new == 0:
                self.entry_t[c] = None
            self.pos_conf[c], self.avg[c] = new, avg
        else:
            self.sh_conf, self.sh_avg = new, avg

    # ------------------------------------------------------------ gate
    def _gate_state(self, now, q):
        sn, tn = self.snap, self.theo_new()
        cs = sn["contracts"]
        return dict(now_us=int(now) & 0xFFFFFFFF, snap_flip_us=int(self.snap_flip) & 0xFFFFFFFF,
                    ref_seq=self.ref_seq, feed_seq=self.last_seq, feed_bad=int(self.feed_bad),
                    S=self.S_q4, S_ref=sn["S_ref"], q_bid=q["bid"], q_ask=q["ask"], q_bsz=q["bsz"], q_asz=q["asz"],
                    theo_0=[c["theo_0"] for c in cs], theo_new=tn, delta_adj=[c["delta_adj"] for c in cs],
                    gamma_c=[c["gamma_c"] for c in cs], vega=[c["vega"] for c in cs],
                    pos_conf=list(self.pos_conf), pos_pend=list(self.pos_pend),
                    sh_conf=self.sh_conf, sh_pend=self.sh_pend, cash_cents=self.cash,
                    orders_in_event=self.orders_in_event, tokens=self.tokens,
                    hb_last_us=int(self.hb_last) & 0xFFFFFFFF, kill=int(self.kill))

    def _latch(self, reason, now):
        if not self.latched:
            self.latched = True
            self.latch_reasons.append({"reason": reason, "t_us": now})
            self.log.append(("latch", round(now, 3), reason))
        self._flatten(now)

    def send(self, now, od, q, purpose, target_t=None):
        """Build gate inputs, call golden gate_eval (R01-R15). Approved -> in flight. Returns reason."""
        if self.latched or not self.armed:                          # G7: nothing passes while tripped
            self.rejects["latched" if self.latched else "not_armed"] = self.rejects.get(
                "latched" if self.latched else "not_armed", 0) + 1
            return -1
        res = g.gate_eval(self._gate_state(now, q), od)
        self.orders_sent += 1
        if (res["fail_mask"] >> 11) & 1:                            # G11: R12 latches on any bit-12 result
            self.rejects[12] = self.rejects.get(12, 0) + 1
            self._latch(12, now)
            return 12
        if res["reason"]:
            self.rejects[res["reason"]] = self.rejects.get(res["reason"], 0) + 1
            if res["reason"] in LATCHING:
                self._latch(res["reason"], now)
            return res["reason"]
        self.tokens -= 1
        self.orders_in_event += 1
        sq = od["qty"] if od["side"] == 0 else -od["qty"]
        if od["is_hedge"]:
            self.sh_pend += sq
        else:
            self.pos_pend[od["contract"]] += sq
        self.pending.append({"t_send": now, "t_arrive": now + self.p["order_latency_us"], "od": dict(od),
                             "purpose": purpose, "target_t": target_t, "sq": sq})
        self.log.append(("send", round(now, 3), purpose, od["is_hedge"], od["contract"], od["side"], od["qty"], od["price"]))
        return 0

    # ------------------------------------------------------------ fill model (E5-E9)
    def _quote_at(self, c, t):
        hist = self._qh_t[c]
        k = bisect.bisect_right(hist, t) - 1
        return self.sc.quote_hist[c][k] if k >= 0 else (None, None)

    def _resolve(self, o):
        od, ta = o["od"], o["t_arrive"]
        self._now = ta
        if od["is_hedge"]:
            self.sh_pend -= o["sq"]
            k = bisect.bisect_right(self._und_t, ta) - 1
            u = self.sc.und[max(k, 0)]
            ok = (u["ask"] <= od["price"]) if od["side"] == 0 else (u["bid"] >= od["price"])
            if ok:                                                  # E8: underlying fills in full at the limit
                self._book_trade("sh", None, o["sq"], od["price"], 0)
                self.fills.append({"t_us": ta, "kind": "hedge", "purpose": o["purpose"], "qty": o["sq"], "price": od["price"]})
                self.log.append(("fill", round(ta, 3), "hedge", o["sq"], od["price"]))
            else:
                self.log.append(("miss", round(ta, 3), "hedge"))
            return
        c, side = od["contract"], od["side"]
        self.pos_pend[c] -= o["sq"]
        tq, q = self._quote_at(c, ta)
        live = q is not None and tq == o["target_t"] and q["ask" if side == 0 else "bid"] == od["price"]
        why = None
        if not live:
            why = "requoted"
        else:
            key = (c, side, tq)
            if any(ra < ta for ra in self._rival_taken.get(key, ())):
                why = "rival"
            else:
                left = q["asz" if side == 0 else "bsz"] - self.consumed.get(key, 0)
                n = min(od["qty"], max(left, 0))
                if n == 0:
                    why = "size"
        if why:
            self.log.append(("miss", round(ta, 3), c, side, why))
            self.misses[why] = self.misses.get(why, 0) + 1
            return
        self.consumed[key] = self.consumed.get(key, 0) + n
        sqn = n if side == 0 else -n
        self._book_trade("opt", c, sqn, od["price"] * TICK_CENTS, n * self.fee_cents)
        self.fills.append({"t_us": ta, "kind": "option", "purpose": o["purpose"], "contract": c, "qty": sqn,
                           "price": od["price"]})
        self.log.append(("fill", round(ta, 3), c, sqn, od["price"]))

    # ------------------------------------------------------------ flatten (E12)
    def _flatten(self, now):
        """Liquidate everything at the last sane displayed touch, in full (operator path,
        outside the gate, TODO(FINAL)). Only ever reduces exposure."""
        self._now = now
        for c in range(8):
            p = self.pos_conf[c]
            if p:
                q = self.last_sane[c]
                px = (q["bid"] if p > 0 else q["ask"]) if q else max(1, round(self.theo_new()[c] / 256))
                self._book_trade("opt", c, -p, px * TICK_CENTS, abs(p) * self.fee_cents)
                self.fills.append({"t_us": now, "kind": "option", "purpose": "flatten", "contract": c, "qty": -p, "price": px})
                self.log.append(("flatten", round(now, 3), c, -p, px))
        if self.sh_conf:
            q = self.last_sane[8]
            px = (q["bid"] if self.sh_conf > 0 else q["ask"]) if q else self.S_q4 // 16
            self.fills.append({"t_us": now, "kind": "hedge", "purpose": "flatten", "qty": -self.sh_conf, "price": px})
            self.log.append(("flatten", round(now, 3), "sh", -self.sh_conf, px))
            self._book_trade("sh", None, -self.sh_conf, px, 0)

    # ------------------------------------------------------------ inputs
    def set_kill(self, now):
        """Kill input (R15): can only make things safer. Flattens and latches."""
        self.kill = True
        self._latch(15, now)

    def local_rearm(self):
        """The ONLY path that clears a latch (operator: KEY0 with ARM low, then ARM rising).
        Never called by the engine. Refused while the kill input is still asserted."""
        if self.kill:
            raise RuntimeError("kill input still asserted: cannot re-arm")
        self.latched = False
        self.feed_bad = False                                       # resync (A12)
        self.armed = False                                          # needs the arming conditions again

    # ------------------------------------------------------------ strategy pieces
    def _event_trigger(self, now):
        tn = self.theo_new()
        bid, ask, bsz, asz, ok = [0] * 8, [0] * 8, [0] * 8, [0] * 8, 0
        for c in range(8):
            q = self.opt_q[c]
            if q:
                bid[c], ask[c], bsz[c], asz[c] = q["bid"], q["ask"], q["bsz"], q["asz"]
                ok |= int(g.quote_sane(q["bid"], q["ask"], q["bsz"], q["asz"])) << c
        lockout = 0
        for o in self.pending:
            if not o["od"]["is_hedge"]:
                lockout |= 3 << (2 * o["od"]["contract"])
        pos = [self.pos_conf[c] + self.pos_pend[c] for c in range(8)]
        orders = trigger_p(tn, bid, ask, bsz, asz, ok, lockout, pos, self.thr,
                           self.p["size_cap_base"], self.p["size_cap_max"], self.p["max_orders_per_event"])
        if orders:
            self.triggers += 1
        for o in orders:
            c = o["contract"]
            od = dict(is_hedge=0, contract=c, side=o["side"], qty=o["size"], price=o["price"])
            self.send(now, od, self.opt_q[c], "entry", self.opt_q[c]["t"])

    def _exits(self, now):
        tn = None
        for c in range(8):
            p = self.pos_conf[c]
            if not p or self.pos_pend[c] or any(not o["od"]["is_hedge"] and o["od"]["contract"] == c for o in self.pending):
                continue
            q = self.opt_q[c]
            if q is None or not g.quote_sane(q["bid"], q["ask"], q["bsz"], q["asz"]):
                continue
            tn = tn or self.theo_new()
            held_edge = g.edge_buy(tn[c], q["ask"]) if p > 0 else g.edge_sell(tn[c], q["bid"])
            reverted = held_edge <= round(self.p["exit_edge_ticks"] * 256)
            timed_out = now - self.entry_t[c] >= self.p["time_stop_us"]
            if not (reverted or timed_out):
                continue
            side = 1 if p > 0 else 0
            disp = q["bsz"] if side == 1 else q["asz"]
            qty = min(abs(p), g.MAX_ORDER_QTY, disp)
            if qty:
                od = dict(is_hedge=0, contract=c, side=side, qty=qty, price=q["bid"] if side == 1 else q["ask"])
                self.send(now, od, q, "exit_time" if timed_out and not reverted else "exit_revert", q["t"])

    def _hedge(self, now):
        if any(o["od"]["is_hedge"] and o["purpose"] == "hedge" for o in self.pending):
            return
        x = self.net_delta_shares()
        if abs(x) <= self.p["hedge_band_shares"]:
            return
        u = self.und_q
        if u is None or not g.quote_sane(u["bid"], u["ask"], u["bsz"], u["asz"]):
            return
        qty = min(int(round(abs(x))), g.MAX_HEDGE_QTY)
        side = 1 if x > 0 else 0
        od = dict(is_hedge=1, contract=0, side=side, qty=qty, price=u["ask"] if side == 0 else u["bid"])
        self.send(now, od, u, "hedge")

    def _rogue(self, now):
        rb = self.sc.rogue_burst
        u = self.und_q
        self.orders_in_event = 0                                    # the burst is one event
        for f in rb["frames"]:
            if self.latched:
                break
            od = dict(is_hedge=1, contract=0, side=f["side"], qty=f["qty_shares"],
                      price=u["ask"] if f["side"] == 0 else u["bid"])
            self.send(now, od, u, "rogue")

    # ------------------------------------------------------------ main loop
    def run(self, sc, kill_at_us=None):
        self.sc, self.misses = sc, {}
        self._qh_t = [[t for t, _ in sc.quote_hist[c]] for c in range(8)]
        self._und_t = [u["t"] for u in sc.und]
        self._rival_taken = {}
        for rh in sc.rival_hits:
            for h in rh["hits"]:
                tq, _ = self._quote_at(h["contract"], rh["t_arrive_us"])
                self._rival_taken.setdefault((h["contract"], h["side"], tq), []).append(rh["t_arrive_us"])
        strikes = g.strikes_for(sc.S_ref)                           # E2: session grid (sim A22)
        self.strikes = strikes
        rogue_t = sc.rogue_burst["t_us"] if sc.rogue_burst else None
        hb_next, hb_iv = 0.0, self.p["heartbeat_interval_us"]
        refill_iv = 5000                                            # README R12 refill 1 per 5,000 us
        self._now = 0.0
        for m in sc.messages:
            t = m["t"]
            # 1. orders arriving before this message
            while self.pending and min(o["t_arrive"] for o in self.pending) <= t:
                o = min(self.pending, key=lambda o: o["t_arrive"])
                self.pending.remove(o)
                self._resolve(o)
            if self.latched and (any(self.pos_conf) or self.sh_conf):
                self._flatten(t)                                    # late fills after a trip
            # 2. heartbeats (E14) and token refill (G9)
            while hb_next <= t:
                self.hb_count += 1
                self.hb_last = hb_next
                hb_next += hb_iv
            n_ref = int(t // refill_iv)
            if n_ref > self.refills_done:
                self.tokens = min(20, self.tokens + n_ref - self.refills_done)
                self.refills_done = n_ref
            # 3. snapshot boundaries (E3): priced at the last sane S before the boundary
            if self.next_snap is not None:
                while self.next_snap <= t:
                    self._snapshot(self.next_snap)
                    self.next_snap += self.p["snapshot_interval_us"]
            # 4. apply the message
            self._now = t
            self._apply(m)
            if self.snap is None and self.S_q4 is not None and all(self.opt_q):
                self._snapshot(t)                                   # first snapshot at book ready
                self.next_snap = t + self.p["snapshot_interval_us"]
            if self.feed_bad and not self.latched:
                self._latch(3, t)                                   # R03 latches on the level (G11)
            if kill_at_us is not None and t >= kill_at_us and not self.kill:
                self.set_kill(t)
            if self.snap is None:
                continue
            # 5. arming (E13): the operator raises ARM once the conditions hold
            if not self.armed and not self.latched and self.hb_count >= 2:
                st_ok = (t - self.snap_flip) <= g.STALE_US and ((self.last_seq - self.ref_seq) & 0xFFFF) <= g.SYNC_MAX_BEHIND
                if st_ok:
                    self.armed = True
                    self.log.append(("armed", round(t, 3)))
            # 6. R13 is continuous (G8)
            tn = self.theo_new()
            eq_x6400 = (64 * self.cash + 25 * sum(p * x for p, x in zip(self.pos_conf, tn)) + 4 * self.sh_conf * self.S_q4)
            if eq_x6400 < -g.DRAWDOWN_LIMIT_DOLLARS * 6400 and not self.latched:
                self._latch(13, t)
            # 7. strategy (one event per message, E11)
            self.orders_in_event = 0
            if self.armed and not self.latched:
                if rogue_t is not None and t >= rogue_t:
                    self._rogue(t)
                    rogue_t = None
                self._exits(t)
                if m["type"] == g.MSG_UNDERLYING and self.S_eval is not None and \
                        abs(self.S_q4 - self.S_eval) >= self.p["min_move_cents"] * 16:
                    self.events += 1
                    self.S_eval = self.S_q4
                    self._event_trigger(t)
                elif self.S_eval is None:
                    self.S_eval = self.S_q4
                self._hedge(t)
            # 8. equity curve
            self.max_abs_net_delta = max(self.max_abs_net_delta, abs(self.net_delta_shares()) / 100)
            eq_u = (256 * (self.cash - self.start_cash) + TICK_CENTS * sum(p * x for p, x in zip(self.pos_conf, tn))
                    + 16 * self.sh_conf * self.S_q4)                 # exact, 1/256 cent (tn is pre-strategy)
            tn2 = self.theo_new()
            eq_u += TICK_CENTS * sum(p * (y - x) for p, x, y in zip(self.pos_conf, tn, tn2))
            eq = eq_u / 25600.0
            self.eq_peak = max(self.eq_peak, eq)
            self.max_dd = max(self.max_dd, self.eq_peak - eq)
        for o in sorted(self.pending, key=lambda o: o["t_arrive"]):  # orders still in flight at the end
            self._resolve(o)
        self.pending = []
        if self.latched and (any(self.pos_conf) or self.sh_conf):
            self._flatten(self._now)
        return self.result()

    def _apply(self, m):
        # feed integrity (A12 on seq; the sim's CRCs are always good)
        s = m["seq"]
        if self.last_seq is not None:
            if s == self.last_seq or s != (self.last_seq + 1) & 0xFFFF:
                self.feed_bad = True
        self.last_seq = s
        q = {"bid": m["bid"], "ask": m["ask"], "bsz": m["bid_size"], "asz": m["ask_size"], "t": m["t"]}
        sane = g.quote_sane(q["bid"], q["ask"], q["bsz"], q["asz"])
        if m["type"] == g.MSG_UNDERLYING:
            self.und_q = q
            if sane:
                self.last_sane[8] = q
                self.S_q4 = g.microprice_q4(q["bid"], q["ask"], q["bsz"], q["asz"], g.RECIP)   # E1
        elif m["type"] == g.MSG_OPTION:
            self.opt_q[m["id"]] = q
            if sane:
                self.last_sane[m["id"]] = q

    def _snapshot(self, t_flip):
        if self.S_q4 is None:
            return
        self.snap = g.build_snapshot(S_ref_dollars=self.S_q4 / 1600.0, strikes=self.strikes)
        assert self.snap["S_ref"] == self.S_q4                       # Q4 S round-trips exactly
        self.snap_flip, self.ref_seq = t_flip, self.last_seq

    # ------------------------------------------------------------ results
    def result(self):
        pnl = self.pnl_cents()
        lhs, rhs = self.accounting_identity()
        assert lhs == rhs, (lhs, rhs)
        opt_fills = [f for f in self.fills if f["kind"] == "option" and f["purpose"] != "flatten"]
        h = hashlib.sha256(json.dumps(self.log, separators=(",", ":")).encode()).hexdigest()
        return {
            "simulated": True,
            "net_pnl": round(float(pnl) / 100.0, 6),
            "realized_pnl": round(float(self.realized) / 100.0, 6),
            "unrealized_pnl": round(float(self.unrealized()) / 100.0, 6),
            "max_drawdown": round(self.max_dd, 6),
            "fills": len(opt_fills),
            "hedge_fills": sum(1 for f in self.fills if f["kind"] == "hedge" and f["purpose"] == "hedge"),
            "hard_trips": len(self.latch_reasons),
            "trip_reasons": [x["reason"] for x in self.latch_reasons],
            "triggers": self.triggers,
            "events": self.events,
            "orders_sent": self.orders_sent,
            "rejects": {str(k): v for k, v in sorted(self.rejects.items(), key=lambda kv: str(kv[0]))},
            "misses": dict(sorted(self.misses.items())),
            "max_abs_net_delta": round(self.max_abs_net_delta, 6),
            "final_position": list(self.pos_conf), "final_shares": self.sh_conf,
            "determinism_hash": h,
        }


def run_session(scenario, params, kill_at_us=None):
    """Convenience: one fresh engine over one scenario."""
    return Engine(params).run(scenario, kill_at_us)
