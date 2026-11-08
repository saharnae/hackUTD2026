"""SIMULATED SPY options market feed for the Latency Arena replay ("simulated arena").

Everything here is SIMULATED. No real market data; every default below is a
placeholder guess and lives in CONFIG. Universe values (spot, strikes, MM spread and sizes,
fees, scheduled stress, R04 limit) are read from golden/golden.py, not duplicated here;
each is TODO(FINAL) there and in docs/PLACEHOLDERS.md.

Pieces:
  OptionsMarketScenario(seed, regime, config=None)  pure, deterministic, no I/O
      .initial_state() / .step(state) / .limits() / .cost(fills)   scenario interface
      .messages      list of message dicts in feed order (after fault injection)
      .words()       list of 128-bit message words (CRC-8 filled in, via golden)
      .sidecar()     dict: MM refresh times, rival hits, jumps, faults, injections
  write_scenario(seed, regime, out_root)  writes <split>/<regime>_seedNNN.{bin,hex,json}

ASSUMPTIONS (spec silent or ambiguous; change CONFIG or here):
  S1. Underlying quote: bid = floor(S_true * 100) cents, ask = bid + 1. A type-1 word is
      emitted on every underlying tick. Underlying sizes ~ U(underlying_size_min..max).
  S2. Tick step: log return = step_sigma_bps * 1e-4 * N(0,1) (0.06 bps, about 0.44 cents at
      733.50). A jump adds sign * U(jump_bps_min, jump_bps_max) bps (log) on top of that tick's
      step (U(1, 12) bps, about 7-88 cents; same uniform shape as before, range rescaled). The
      0.5% jump probability is per Poisson tick. Every random jump is asserted below R04.
      The bps values are relative moves; all absolute moves (mm_move, stress) stay in cents.
  S3. Fake signal (20% of random jumps): the exact opposite log move is applied at
      t_jump + U(0, fake_revert_max_us), as an extra underlying tick at that time.
      The MM's view of spot excludes any unreverted fake displacement, so the MM never
      re-quotes because of a fake jump.
  S4. MM trigger: on an underlying event, if |mm_spot - last_quoted_spot| >= 3 cents and no
      refresh cycle is pending, a cycle starts at that event time (t_jump in the spec).
      All 8 quotes are priced at the trigger-time MM spot and sent at
      t_trigger + W * (1 - refresh_stagger * u_c). No new trigger until the cycle's
      last refresh has gone out.
  S5. Option quote: theo = full BS with the vol rule vs the session S_ref
      (sigma = sigma(m_ref) + lambda*(smile(ln K/S) - smile(ln K/S_ref)) + beta*dS),
      rounded to ticks; bid = max(1, theo - h), ask = max(bid + 1, theo + h), h = mm_half_spread_ticks.
      T is held at T_years (decay over a sub-second session is ignored).
  S6. Rivals: every jump (random real, random fake, scheduled) has rival_prob of a rival
      arriving at t_jump + lognormal(median 40 us, rival_delay_sigma). The rival sees
      the MM quotes in effect at arrival and true theo at the true spot. It takes up to
      rival_max_hits quotes with edge > 0 ticks, best edge first, ties to the lowest
      contract and buy before sell, at full displayed size. Hits are recorded in the
      sidecar only; they do not change the message stream.
  S7. Faults: each quote message (types 1 and 2) independently, p = fault_prob (10% in
      quote_flicker), is replaced by crossed (bid and ask swapped) or zero (bid = 0), 50/50.
  S8. Sequence numbers start at seq_start and wrap at 65,536. After assignment the
      feed_fault regime drops 3 words (gap) and duplicates 3 words (exact copy right after
      the original), at drawn positions that do not touch each other.
  S9. Rogue order burst: CLAUDE.md gives no payload layout for the 0x14 hedge frame, so
      the burst is recorded as a sidecar event (time, count 50, per-frame qty/side)
      rather than encoded as frames.
  S10. Scheduled jumps (single_jump +40 c, flash_crash 5 x -150 c evenly over 30 ms; values from
      golden STRESS_*) are
      absolute moves applied as extra underlying ticks at fixed times; those regimes
      have no random jumps. Underlying fill/session words (types 3-5) are not generated.
  S11. Randomness: numpy PCG64 seeded with SeedSequence([seed, regime_index]); separate
      child streams for ticks, jumps, MM, rivals and faults.
  S12. .hex = first 4,096 words, one 32-hex-digit word per line ($readmemh). Shorter
      feeds are padded with all-zero words (type 0 = idle; a zero word has CRC 0, so the
      replay engine must stop at sidecar["hex_valid_words"]). Longer feeds are truncated
      and the sidecar says so.
  S13. Seed split: 0..79 in-sample, 80..99 out-of-sample; any other seed is rejected.
      The writer picks the folder from the seed, so the two splits can never share one.

OPEN QUESTION: the CS teammate's interface (initial_state, step, limits, cost) signatures
are unknown. The versions here are a minimal guess, documented on each method.
"""

import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "golden"))
import golden as g  # noqa: E402

CONFIG = {
    "label": "SIMULATED",
    "spot": g.SPOT,                     # $ start and session S_ref (golden, TODO(FINAL))
    "duration_us": 600_000,             # one replay session
    "tick_gap_mean_us": 200.0,
    "step_sigma_bps": 0.06,             # ~0.44 cents at 733.50 (asserted below)  TODO(FINAL)
    "jump_prob": 0.005,                 # per tick
    "jump_bps_min": 1.0,                # ~7 cents at 733.50  TODO(FINAL)
    "jump_bps_max": 12.0,               # ~88 cents at 733.50, must stay < R04 (asserted)  TODO(FINAL)
    "fake_frac": 0.20,
    "fake_revert_max_us": 5_000.0,
    "underlying_size_min": 1,           # guess (S1)
    "underlying_size_max": 40,
    "mm_half_spread_ticks": g.MM_HALF_SPREAD_TICKS,   # 1 tick (golden, TODO(FINAL))
    "mm_size_min": g.QUOTE_SIZE_MIN,                  # 20 (golden, TODO(FINAL))
    "mm_size_max": g.QUOTE_SIZE_MAX,                  # 200 (golden, TODO(FINAL))
    "mm_move_cents": 3.0,
    "stale_median_us": 100.0,
    "stale_sigma": 0.5,
    "refresh_stagger": 0.2,
    "rival_prob": 0.3,
    "rival_delay_median_us": 40.0,
    "rival_delay_sigma": 0.5,           # guess
    "rival_max_hits": 2,                # guess
    "fault_prob": 0.01,
    "flicker_fault_prob": 0.10,
    "seq_start": 0,
    "n_gaps": 3,
    "n_dups": 3,
    "single_jump_t_us": 300_000.0,
    "single_jump_cents": float(g.STRESS_SINGLE_JUMP_CENTS),   # +40 c (golden, TODO(FINAL))
    "flash_t_us": 250_000.0,
    "flash_n": g.STRESS_FLASH_STEPS,                          # 5 (golden, TODO(FINAL))
    "flash_span_us": 30_000.0,
    "flash_cents": float(g.STRESS_FLASH_STEP_CENTS),          # -150 c per step (golden, TODO(FINAL))
    "rogue_t_us": 300_000.0,
    "rogue_frames": 50,
    "rogue_qty_shares": 100,            # guess
    "fee_ticks_per_contract": g.FEE_TICKS_PER_CONTRACT,       # 0.5 (golden, TODO(FINAL))
    "vol_lambda": g.VOL_LAMBDA,
    "vol_beta": g.VOL_BETA,
    "hex_words": 4096,
    "in_sample_seeds": (0, 79),
    "out_sample_seeds": (80, 99),
}


def bps_to_cents(bps, spot=None):
    """Relative move in bps -> cents at spot (default CONFIG spot)."""
    return bps * 1e-4 * (CONFIG["spot"] if spot is None else spot) * 100.0


def check_config(cfg=CONFIG):
    """Unit checks on a config (raises AssertionError). Returns the derived cents values."""
    step_c = bps_to_cents(cfg["step_sigma_bps"], cfg["spot"])
    jmin_c, jmax_c = (bps_to_cents(cfg[k], cfg["spot"]) for k in ("jump_bps_min", "jump_bps_max"))
    # the largest single random move is a max jump plus a 6-sigma step on the same tick
    worst_c = bps_to_cents(cfg["jump_bps_max"] + 6 * cfg["step_sigma_bps"], cfg["spot"]) * 1.0001
    assert worst_c < g.JUMP_LIMIT_CENTS, (worst_c, g.JUMP_LIMIT_CENTS)
    assert abs(cfg["single_jump_cents"]) < g.JUMP_LIMIT_CENTS          # single_jump must not trip R04
    assert abs(cfg["flash_cents"] * cfg["flash_n"]) > g.JUMP_LIMIT_CENTS   # the flash crash must
    return {"step_cents": step_c, "jump_cents_min": jmin_c, "jump_cents_max": jmax_c}


DERIVED = check_config()
assert abs(DERIVED["step_cents"] - 0.44) < 0.005, DERIVED        # 0.06 bps at 733.50 = 0.4401 c
assert abs(DERIVED["jump_cents_min"] - 7.3) < 0.1 and abs(DERIVED["jump_cents_max"] - 88.0) < 0.1, DERIVED

REGIMES = ("quiet", "normal", "single_jump", "flash_crash", "quote_flicker", "feed_fault", "rogue_burst")


def split_of(seed, config=CONFIG):
    lo, hi = config["in_sample_seeds"]
    if lo <= seed <= hi:
        return "in_sample"
    lo, hi = config["out_sample_seeds"]
    if lo <= seed <= hi:
        return "out_of_sample"
    raise ValueError(f"seed {seed} is outside the in-sample and out-of-sample ranges")


def _lognormal(rng, median, sigma, size=None):
    return median * np.exp(sigma * rng.standard_normal(size))


class OptionsMarketScenario:
    """One seeded, SIMULATED session. Pure: all work happens in __init__, no I/O."""

    def __init__(self, seed, regime="normal", config=None):
        if regime not in REGIMES:
            raise ValueError(f"unknown regime {regime!r}")
        self.cfg = dict(CONFIG, **(config or {}))
        self.seed, self.regime = int(seed), regime
        self.split = split_of(self.seed, self.cfg)
        ss = np.random.SeedSequence([self.seed, REGIMES.index(regime)])
        r_tick, r_jump, r_mm, r_rival, r_fault, r_seq = (np.random.Generator(np.random.PCG64(s))
                                                         for s in ss.spawn(6))
        self.S_ref = self.cfg["spot"]
        strikes = g.strikes_for(self.S_ref)                                # A22 grid for the session S_ref
        self._contracts = [(strikes[i % 4], i < 4) for i in range(8)]       # (K, is_call)
        self._build_underlying(r_tick, r_jump)
        self._run_mm(r_mm)
        self._draw_rivals(r_rival)
        self._assemble(r_fault, r_seq)

    # ------------------------------------------------------------ pricing
    def theo_ticks(self, S, c):
        """Full BS theo (ticks, float) for contract c at spot S under the vol rule (S5)."""
        K, is_call = self._contracts[c]
        m_ref, m_new = math.log(K / self.S_ref), math.log(K / S)
        sigma = (g.smile_sigma(m_ref) + self.cfg["vol_lambda"] * (g.smile_sigma(m_new) - g.smile_sigma(m_ref))
                 + self.cfg["vol_beta"] * (S - self.S_ref))
        return g.bs(S, K, g.T_YEARS, g.RATE, g.DIV, sigma, is_call)["price"] * 100.0

    # ------------------------------------------------------------ underlying path
    def _build_underlying(self, r_tick, r_jump):
        cfg, dur = self.cfg, self.cfg["duration_us"]
        n = int(dur / cfg["tick_gap_mean_us"] * 1.3) + 200
        gaps = r_tick.exponential(cfg["tick_gap_mean_us"], n)
        while gaps.sum() <= dur:
            gaps = np.concatenate([gaps, r_tick.exponential(cfg["tick_gap_mean_us"], n)])
        t = np.cumsum(gaps)
        t = t[t <= dur]
        steps = cfg["step_sigma_bps"] * 1e-4 * r_tick.standard_normal(t.size)
        jump_prob = 0.0 if self.regime in ("quiet", "single_jump", "flash_crash") else cfg["jump_prob"]
        is_jump = r_jump.random(t.size) < jump_prob
        jbps = r_jump.uniform(cfg["jump_bps_min"], cfg["jump_bps_max"], t.size)
        jsign = np.where(r_jump.random(t.size) < 0.5, -1.0, 1.0)
        fake = r_jump.random(t.size) < cfg["fake_frac"]
        revert_delay = r_jump.uniform(0.0, cfg["fake_revert_max_us"], t.size)
        sizes = r_tick.integers(cfg["underlying_size_min"], cfg["underlying_size_max"] + 1, (t.size, 2))
        self.tick_times = t                                    # Poisson ticks only (tests)

        # events: (time, order, kind, payload); kind tick | revert | sched
        ev = [(float(t[i]), 0, "tick", i) for i in range(t.size)]
        self.jumps = []
        for i in np.flatnonzero(is_jump):
            lr = float(jsign[i] * jbps[i] * 1e-4)
            j = {"t_us": float(t[i]), "bps": float(jsign[i] * jbps[i]), "fake": bool(fake[i]),
                 "scheduled": False, "revert_t_us": None, "_lr": lr}
            if fake[i]:
                j["revert_t_us"] = float(t[i] + revert_delay[i])
                if j["revert_t_us"] <= dur:
                    ev.append((j["revert_t_us"], 1, "revert", len(self.jumps)))
            self.jumps.append(j)
        sched = []
        if self.regime == "single_jump":
            sched = [(cfg["single_jump_t_us"], cfg["single_jump_cents"])]
        elif self.regime == "flash_crash":
            k = cfg["flash_n"]
            sched = [(cfg["flash_t_us"] + cfg["flash_span_us"] * i / (k - 1), cfg["flash_cents"]) for i in range(k)]
        for ts, cents in sched:
            ev.append((float(ts), 2, "sched", cents))
        ev.sort(key=lambda e: (e[0], e[1]))
        # sizes for inserted events come from a fixed extra draw
        extra_sizes = r_tick.integers(cfg["underlying_size_min"], cfg["underlying_size_max"] + 1, (len(ev), 2))

        jump_of_tick = {int(np.flatnonzero(is_jump)[k]): k for k in range(len(self.jumps))}
        lnS, fake_lr = math.log(self.S_ref), 0.0
        self.und = []                                          # dicts: t, S_true, mm_spot, bid, ask, bsz, asz
        for n_ev, (ts, _, kind, p) in enumerate(ev):
            if kind == "tick":
                lnS += float(steps[p])
                bsz, asz = int(sizes[p, 0]), int(sizes[p, 1])
                if p in jump_of_tick:
                    j = self.jumps[jump_of_tick[p]]
                    lnS += j["_lr"]
                    if j["fake"]:
                        fake_lr += j["_lr"]
            elif kind == "revert":
                j = self.jumps[p]
                lnS -= j["_lr"]
                fake_lr -= j["_lr"]
                bsz, asz = (int(x) for x in extra_sizes[n_ev])
            else:
                S_now = math.exp(lnS)
                lnS = math.log(S_now + p / 100.0)
                self.jumps.append({"t_us": ts, "bps": (math.exp(lnS) / S_now - 1) * 1e4, "fake": False,
                                   "scheduled": True, "revert_t_us": None, "_lr": 0.0})
                bsz, asz = (int(x) for x in extra_sizes[n_ev])
            S = math.exp(lnS)
            bid = int(math.floor(S * 100.0))
            self.und.append({"t": ts, "S_true": S, "mm_spot": math.exp(lnS - fake_lr),
                             "bid": bid, "ask": bid + 1, "bsz": bsz, "asz": asz})
        self.jumps.sort(key=lambda j: j["t_us"])
        for j in self.jumps:
            del j["_lr"]

    # ------------------------------------------------------------ market maker
    def _quote(self, S, c, r_mm):
        theo = int(math.floor(self.theo_ticks(S, c) + 0.5))
        h = self.cfg["mm_half_spread_ticks"]
        bid = max(1, theo - h)
        ask = max(bid + 1, theo + h)
        bsz, asz = (int(x) for x in r_mm.integers(self.cfg["mm_size_min"], self.cfg["mm_size_max"] + 1, 2))
        return {"bid": bid, "ask": ask, "bsz": bsz, "asz": asz}

    def _run_mm(self, r_mm):
        cfg = self.cfg
        self.opt = []                                          # option quote messages (clean)
        self.quote_hist = [[] for _ in range(8)]               # per contract: (t, quote)
        for c in range(8):
            q = dict(self._quote(self.S_ref, c, r_mm), t=0.0, id=c)
            self.opt.append(q)
            self.quote_hist[c].append((0.0, q))
        last_spot, busy_until = self.S_ref, -1.0
        self.refreshes = []
        for u in self.und:
            if u["t"] < busy_until:
                continue
            if abs(u["mm_spot"] - last_spot) * 100.0 >= cfg["mm_move_cents"] - 1e-9:
                W = float(_lognormal(r_mm, cfg["stale_median_us"], cfg["stale_sigma"]))
                uc = r_mm.random(8)
                times = [u["t"] + W * (1.0 - cfg["refresh_stagger"] * float(uc[c])) for c in range(8)]
                spot = u["mm_spot"]
                for c in range(8):
                    if times[c] > cfg["duration_us"]:
                        continue
                    q = dict(self._quote(spot, c, r_mm), t=times[c], id=c)
                    self.opt.append(q)
                    self.quote_hist[c].append((times[c], q))
                self.refreshes.append({"trigger_t_us": u["t"], "W_us": W, "mm_spot": spot,
                                       "refresh_t_us": times})
                last_spot, busy_until = spot, max(times)

    # ------------------------------------------------------------ rivals
    def _draw_rivals(self, r_rival):
        cfg = self.cfg
        und_t = np.array([u["t"] for u in self.und])
        self.rival_hits = []
        for ji, j in enumerate(self.jumps):
            arrive = r_rival.random() < cfg["rival_prob"]
            delay = float(_lognormal(r_rival, cfg["rival_delay_median_us"], cfg["rival_delay_sigma"]))
            if not arrive:
                continue
            ta = j["t_us"] + delay
            k = int(np.searchsorted(und_t, ta, side="right")) - 1
            S = self.und[max(k, 0)]["S_true"]
            cands = []
            for c in range(8):
                q = [qq for tq, qq in self.quote_hist[c] if tq <= ta][-1]
                theo = self.theo_ticks(S, c)
                for side, edge, price, size in ((0, theo - q["ask"], q["ask"], q["asz"]),
                                                (1, q["bid"] - theo, q["bid"], q["bsz"])):
                    if edge > 0:
                        cands.append((-edge, c, side, {"contract": c, "side": side, "price": price,
                                                       "size": size, "edge_ticks": edge}))
            cands.sort(key=lambda x: x[:3])
            self.rival_hits.append({"jump_index": ji, "t_jump_us": j["t_us"], "t_arrive_us": ta,
                                    "hits": [x[3] for x in cands[:cfg["rival_max_hits"]]]})

    # ------------------------------------------------------------ feed assembly
    def _assemble(self, r_fault, r_seq):
        cfg = self.cfg
        msgs = [{"t": u["t"], "type": g.MSG_UNDERLYING, "id": g.UNDERLYING_ID, "bid": u["bid"], "ask": u["ask"],
                 "bid_size": u["bsz"], "ask_size": u["asz"]} for u in self.und]
        msgs += [{"t": q["t"], "type": g.MSG_OPTION, "id": q["id"], "bid": q["bid"], "ask": q["ask"],
                  "bid_size": q["bsz"], "ask_size": q["asz"]} for q in self.opt]
        msgs.sort(key=lambda m: (m["t"], m["type"], m["id"]))
        p = cfg["flicker_fault_prob"] if self.regime == "quote_flicker" else cfg["fault_prob"]
        hit, kind = r_fault.random(len(msgs)) < p, r_fault.random(len(msgs)) < 0.5
        for i, m in enumerate(msgs):
            m["ts_us"] = int(m["t"]) & 0xFFFFFFFF
            m["fault"] = None
            if hit[i]:
                if kind[i]:
                    m["bid"], m["ask"], m["fault"] = m["ask"], m["bid"], "crossed"
                else:
                    m["bid"], m["fault"] = 0, "zero"
        for i, m in enumerate(msgs):
            m["seq"] = (cfg["seq_start"] + i) & 0xFFFF
        self.injections = []
        if self.regime == "feed_fault":
            n_g, n_d = cfg["n_gaps"], cfg["n_dups"]
            # spaced positions so injections never touch each other
            slots = np.sort(r_seq.choice(np.arange(5, len(msgs) - 5, 4), n_g + n_d, replace=False))
            kinds = ["gap"] * n_g + ["dup"] * n_d
            r_seq.shuffle(kinds)
            out, plan = [], dict(zip(slots.tolist(), kinds))
            for i, m in enumerate(msgs):
                k = plan.get(i)
                if k == "gap":
                    self.injections.append({"kind": "gap", "dropped_seq": m["seq"], "at_index": len(out)})
                    continue
                out.append(m)
                if k == "dup":
                    out.append(dict(m))
                    self.injections.append({"kind": "dup", "seq": m["seq"], "at_index": len(out) - 1})
            msgs = out
        self.messages = msgs
        self.faults = [{"index": i, "kind": m["fault"]} for i, m in enumerate(msgs) if m["fault"]]
        self.rogue_burst = None
        if self.regime == "rogue_burst":
            self.rogue_burst = {"t_us": cfg["rogue_t_us"], "count": cfg["rogue_frames"],
                                "frame_type": "0x14 hedge order (payload layout not in CLAUDE.md)",
                                "frames": [{"i": i, "side": i % 2, "qty_shares": cfg["rogue_qty_shares"]}
                                           for i in range(cfg["rogue_frames"])]}

    # ------------------------------------------------------------ outputs
    def words(self):
        return [g.build_word(m["type"], m["seq"], m["ts_us"], m["id"], m["bid"], m["ask"],
                             m["bid_size"], m["ask_size"]) for m in self.messages]

    def sidecar(self):
        return {
            "label": f"SIMULATED market data; not real {g.UNDERLYING_SYMBOL} quotes",
            "seed": self.seed, "split": self.split, "regime": self.regime,
            "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.cfg.items()},
            "n_words": len(self.messages),
            "hex_valid_words": min(len(self.messages), self.cfg["hex_words"]),
            "hex_truncated": len(self.messages) > self.cfg["hex_words"],
            "mm_refreshes": self.refreshes,
            "rival_hits": self.rival_hits,
            "jumps": self.jumps,
            "faults": self.faults,
            "seq_injections": self.injections,
            "rogue_burst": self.rogue_burst,
        }

    # ------------------------------------------------------------ scenario interface (guess)
    def initial_state(self):
        """State before the first message: {"index", "t_us", "S_ref", "regime", "seed"}."""
        return {"index": 0, "t_us": 0, "S_ref": self.S_ref, "regime": self.regime, "seed": self.seed}

    def step(self, state):
        """Pure: returns (next_state, message dict with "word") or (state, None) at the end."""
        i = state["index"]
        if i >= len(self.messages):
            return state, None
        m = dict(self.messages[i])
        m["word"] = g.build_word(m["type"], m["seq"], m["ts_us"], m["id"], m["bid"], m["ask"],
                                 m["bid_size"], m["ask_size"])
        return dict(state, index=i + 1, t_us=m["ts_us"]), m

    def limits(self):
        """Risk-gate limits (compile-time constants) from the golden model."""
        return {"stale_us": g.STALE_US, "jump_limit_cents": g.JUMP_LIMIT_CENTS,
                "max_order_qty": g.MAX_ORDER_QTY, "max_hedge_qty": g.MAX_HEDGE_QTY,
                "max_position": g.MAX_POSITION, "net_delta": g.NET_DELTA_LIMIT,
                "net_gamma": g.NET_GAMMA_LIMIT_X100 / 100.0, "net_vega": g.NET_VEGA_LIMIT,
                "max_orders_per_event": g.MAX_ORDERS_PER_EVENT, "drawdown_dollars": g.DRAWDOWN_LIMIT_DOLLARS,
                "heartbeat_us": g.HEARTBEAT_US}

    def cost(self, fills):
        """Fees in $ for an iterable of fills with "qty" (contracts): fee_ticks * $1 per tick."""
        return sum(f["qty"] for f in fills) * self.cfg["fee_ticks_per_contract"]


# ---------------------------------------------------------------- writer (the only I/O)
def write_scenario(seed, regime, out_root, config=None):
    """Write <out_root>/<split>/<regime>_seedNNN.{bin,hex,json}. Returns the three paths."""
    sc = OptionsMarketScenario(seed, regime, config)
    folder = os.path.join(out_root, sc.split)
    os.makedirs(folder, exist_ok=True)
    stem = os.path.join(folder, f"{regime}_seed{seed:03d}")
    words = sc.words()
    with open(stem + ".bin", "wb") as f:
        for w in words:
            f.write(w.to_bytes(16, "big"))
    n = sc.cfg["hex_words"]
    hex_words = (words + [0] * n)[:n]
    with open(stem + ".hex", "w", newline="\n") as f:
        f.write("".join(f"{w:032x}\n" for w in hex_words))
    with open(stem + ".json", "w", newline="\n") as f:
        json.dump(sc.sidecar(), f, indent=1, sort_keys=True)
        f.write("\n")
    return stem + ".bin", stem + ".hex", stem + ".json"


def write_split(split, out_root, regimes=REGIMES, config=None):
    """Write every seed of one split ("in_sample" or "out_of_sample") for the given regimes."""
    cfg = dict(CONFIG, **(config or {}))
    lo, hi = cfg["in_sample_seeds"] if split == "in_sample" else cfg["out_sample_seeds"]
    return [write_scenario(s, r, out_root, config) for s in range(lo, hi + 1) for r in regimes]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Write SIMULATED scenario files")
    ap.add_argument("--split", choices=("in_sample", "out_of_sample"), required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios"))
    args = ap.parse_args()
    paths = write_split(args.split, args.out)
    print(f"wrote {len(paths)} scenarios to {os.path.join(args.out, args.split)}")
