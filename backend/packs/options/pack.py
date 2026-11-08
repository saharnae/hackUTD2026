"""Options pack (SIMULATED arena): wraps golden/engine.py and sim/options_model.py behind the Pack interface.

State (JSON-safe): pack, label, seed, regime, t_us, duration_us, session_params (engine params of the
running session), config_register (0x12 fields held by the board), config_rejects, kill_t_us,
last_config (result of the last 0x12), channels, summary.

Commands (PK2):
  {"kind": "kill"}                                  0x15 KILL frame: latches R15 at the current time
  {"kind": "config_frame", "payload_hex": "..."}    0x12 payload; applied only while not armed (FRAMES FR4,
                                                    frames.ConfigRegister); takes effect at the next session
  {"kind": "new_session", "seed"?, "regime"?}       twin power cycle (human-only path; TW6)
  {"kind": "config", "params": {...}}               what-if only (never sent to the board): replay with params

ASSUMPTIONS (OP):
  OP1. The live config at start = tuning_result.json "recommended" params quantized through 0x12, plus its
       hedge_band_shares (laptop-side, CM1). TODO(FINAL).
  OP2. Limits: trip = the golden gate constant for that channel; warn / high = LIMIT_WARN_FRAC / LIMIT_HIGH_FRAC
       of it (0.5 / 0.8, backend/config.py). TODO(FINAL).
  OP3. what_if(jump N cents): the "shock table" is the per-contract table the laptop would load into
       io_shock (0x13, addr = {dir, size[7:0], contract[2:0], is_ask}): golden reprice of the active snapshot
       at S_ref + N, next to the exact BS value under the vol rule, the stale displayed quote, the edge and
       whether the trigger fires. Then the real evaluator replays the single_jump regime on held-out seeds;
       for N != golden STRESS_SINGLE_JUMP_CENTS the jump size is a scenario override (replay RP2).
  OP4. The re-quoted MM price in the shock rows follows sim S5 (round theo to ticks, +/- MM half spread).
  OP5. plan_actions candidates = tuning_result.json confirmed_top_k (then "recommended"), quantized through
       0x12, de-duplicated, minus the current config. They are filtered by a held-out replay (seeds 60-79):
       dropped if the tuning score fails (unexpected trip or too few normal fills).
  OP6. The twin forecast replays the SAME seeded scenario forward, so it is perfect foresight inside the
       twin. It shows what the gate will do; it is not a market prediction. TODO(FINAL).
"""

import json
import math

from ... import _paths  # noqa: F401
from ... import config as C
from . import configmap as CM
from . import replay as RP
from . import twin as TW
import frames as F
import golden as g

LABEL = "SIMULATED (software twin over golden/engine.py; twin timing, not FPGA latency)"


def _load_tuning(path=None):
    with open(path or _paths.TUNING_RESULT) as f:
        return json.load(f)


class OptionsPack:
    name = "options"
    forecast_step_s = 0.01
    what_if_horizon_s = 0.05

    def __init__(self, regime=None, replayer=None, tuning_result=None, stress_regimes=None, held_out=None):
        self.regime = regime or C.TWIN_DEFAULT_REGIME
        self.replayer = replayer or RP.Replayer(workers=1)
        self.tuning = tuning_result or _load_tuning()
        rec = self.tuning["recommended"]["params"]
        self.hedge_band_shares = int(rec["hedge_band_shares"])
        self.initial_register = CM.to_fields(CM.quantize({k: rec[k] for k in CM.TUNABLE_PARAMS}))
        self.stress_regimes = tuple(stress_regimes or C.STRESS_REGIMES)
        self.held_out = tuple(held_out or C.HELD_OUT_SEEDS)

    # ------------------------------------------------------------ params
    def params_from_register(self, fields):
        p = CM.from_fields(fields)
        p["hedge_band_shares"] = self.hedge_band_shares
        return p

    def current_params(self, state):
        return dict(state["session_params"])

    # ------------------------------------------------------------ Pack interface
    def initial_state(self, seed):
        return self._new_session(seed, self.regime, dict(self.initial_register), 0)

    def _new_session(self, seed, regime, register, rejects):
        lo, hi = C.TWIN_SEEDS
        if not (isinstance(seed, int) and lo <= seed <= hi):
            raise RP.SeedGuardError(f"the live twin runs seeds {lo}-{hi} only (60-79 are held out, 80+ are never "
                                    f"shown to agents); got {seed!r}")
        if regime not in RP.EV.REGIMES:
            raise ValueError(f"unknown regime {regime!r}")
        st = {"pack": self.name, "label": LABEL, "seed": seed, "regime": regime, "t_us": 0,
              "duration_us": TW.DURATION_US, "session_params": self.params_from_register(register),
              "config_register": register, "config_rejects": rejects, "kill_t_us": None, "last_config": None}
        return self._derive(st)

    def step(self, state, dt_s, commands=()):
        if dt_s < 0:
            raise ValueError("dt_s must be >= 0")
        st = json.loads(json.dumps(state))                  # never mutate the input (PK2)
        for cmd in commands or []:
            kind = cmd.get("kind")
            if kind == "kill":
                if st["kill_t_us"] is None:
                    st["kill_t_us"] = st["t_us"]
            elif kind == "config_frame":
                reg = F.ConfigRegister()
                reg.params, reg.config_rejects = dict(st["config_register"]), st["config_rejects"]
                e = self._engine(st)
                armed = bool(e.armed and not e.latched)
                ok = reg.apply(bytes.fromhex(cmd["payload_hex"]), armed)
                st["config_register"], st["config_rejects"] = dict(reg.params), reg.config_rejects
                st["last_config"] = {"applied": ok, "t_us": st["t_us"],
                                     "reason": None if ok else ("board armed: config is ignored while armed (FR4)"
                                                                if armed else "decoder refused a field")}
            elif kind == "new_session":
                st = self._new_session(cmd.get("seed", st["seed"]), cmd.get("regime", st["regime"]),
                                       st["config_register"], st["config_rejects"])
            else:
                raise ValueError(f"unknown command kind {kind!r}")
        st["t_us"] = min(st["duration_us"], st["t_us"] + int(round(dt_s * 1e6)))
        return self._derive(st)

    def limits(self, state=None):
        trips = {"net_delta": g.NET_DELTA_LIMIT, "loss_dollars": g.DRAWDOWN_LIMIT_DOLLARS, "tokens_used": 20,
                 "jump_cents": g.JUMP_LIMIT_CENTS, "snapshot_age_us": g.STALE_US, "heartbeat_age_us": g.HEARTBEAT_US,
                 "max_abs_position": g.MAX_POSITION, "net_vega_dollars": g.NET_VEGA_LIMIT,
                 "net_gamma": g.NET_GAMMA_LIMIT_X100 / 100.0}
        return {k: {"warn": v * C.LIMIT_WARN_FRAC, "high": v * C.LIMIT_HIGH_FRAC, "trip": v} for k, v in trips.items()}

    CHANNEL_RULES = {"net_delta": "R09", "loss_dollars": "R13", "tokens_used": "R12", "jump_cents": "R04",
                     "snapshot_age_us": "R01", "heartbeat_age_us": "R14", "max_abs_position": "R08",
                     "net_vega_dollars": "R11", "net_gamma": "R10"}

    def cost(self, command):
        kind = command.get("kind")
        if kind == "kill":
            ctx = command.get("context")
            if not ctx:
                return {"dollars": None, "minutes": None, "notes": "no position context given"}
            fee = g.FEE_TICKS_PER_CONTRACT
            d = 0.0
            for c in range(8):
                p = ctx["position"][c]
                q, tn = ctx["quotes"][c], ctx["theo_new_ticks"][c]
                if p and q:
                    px = q["bid"] if p > 0 else q["ask"]
                    d += abs(p) * (fee + abs(px - tn))                   # ticks = $ per contract
            return {"dollars": round(d, 4), "minutes": None,
                    "notes": "flatten cost at the displayed touch vs model, plus fees (SIMULATED). Re-arming is a "
                             "human action at the board; its time is not modeled (TODO(FINAL))."}
        if kind in ("config", "config_frame"):
            return {"dollars": 0.0, "minutes": None,
                    "notes": "a 0x12 frame has no trading cost; it is applied only while disarmed, so it needs a "
                             "disarm / re-arm at the board (time not modeled, TODO(FINAL))"}
        return {"dollars": None, "minutes": None, "notes": f"no cost model for {kind!r}"}

    # ------------------------------------------------------------ derived state
    def _engine(self, st):
        return TW.run_prefix(st["seed"], st["regime"], st["session_params"], st["t_us"], st["kill_t_us"])

    def _derive(self, st):
        e = self._engine(st)
        st["channels"] = TW.channels(e)
        st["summary"] = TW.summary(e)
        return st

    def engine_at(self, state):
        return self._engine(state)

    def kill_command(self, state):
        e = self._engine(state)
        tn = [x / 256.0 for x in e.theo_new()] if e.snap is not None and e.S_q4 is not None else [0.0] * 8
        quotes = [({"bid": q["bid"], "ask": q["ask"]} if q else None) for q in e.last_sane[:8]]
        pos = [e.pos_conf[c] + e.pos_pend[c] for c in range(8)]
        return {"kind": "kill", "context": {"position": pos, "quotes": quotes, "theo_new_ticks": tn}}

    # ------------------------------------------------------------ Engine hooks
    def what_if(self, state, change):
        kind = change.get("kind")
        seeds = RP.guard_seeds(change.get("seeds") or RP.seeds_from_range(*self.held_out), held_out=True)
        if kind == "jump":
            cents = int(change["cents"])
            table = self.shock_table(state, cents)
            override = None if cents == g.STRESS_SINGLE_JUMP_CENTS else {"single_jump_cents": float(cents)}
            rep = self.replayer.summary(self.current_params(state), seeds, ["single_jump"], override, held_out=True)
            return {"method": "golden reprice shock table + real evaluator replay (single_jump regime)",
                    "label": LABEL, "shock_table": table, "replay": rep}
        if kind == "config":
            regimes = list(change.get("regimes") or self.stress_regimes)
            base_p = self.current_params(state)
            alt_p = self.apply_changes(base_p, change["params"])
            base = self.replayer.summary(base_p, seeds, regimes, held_out=True)
            alt = self.replayer.summary(alt_p, seeds, regimes, held_out=True)
            return {"method": "held-out replay, current config vs changed config", "label": LABEL,
                    "baseline": base, "with_change": alt, "delta": compare(base, alt)}
        return None                                                  # generic forecast comparison

    def apply_changes(self, params, changes):
        probs = CM.merged_problems({k: params[k] for k in CM.TUNABLE_PARAMS}, changes)
        if probs:
            raise ValueError("; ".join(p["reason"] for p in probs))
        q = CM.quantize(dict({k: params[k] for k in CM.TUNABLE_PARAMS}, **changes))
        q["hedge_band_shares"] = params["hedge_band_shares"]
        return q

    def candidate_actions(self, state, goal=None):
        cur = {k: state["session_params"][k] for k in CM.TUNABLE_PARAMS}
        rows = sorted(self.tuning.get("confirmed_top_k", []), key=lambda r: -r["confirm"])
        rows.append(dict(self.tuning["recommended"], recommended=True))
        out, seen = [], set()
        for r in rows:
            q = CM.quantize({k: r["params"][k] for k in CM.TUNABLE_PARAMS})
            key = json.dumps(q, sort_keys=True)
            if key in seen or q == CM.quantize(cur):
                continue
            seen.add(key)
            changes = {k: v for k, v in q.items() if v != cur[k]}
            out.append({"kind": "config", "params": changes,
                        "source": f"optuna trial {r.get('trial')} (search {r.get('search'):.4f}, "
                                  f"confirm {r.get('confirm'):.4f}, seeds 0-59 / 60-79)"})
        return out

    def evaluate_action(self, state, action, goal=None):
        goal = goal or {}
        seeds = RP.guard_seeds(goal.get("seeds") or RP.seeds_from_range(*self.held_out), held_out=True)
        regimes = list(goal.get("regimes") or self.stress_regimes)
        p = self.current_params(state)
        if action is not None:
            p = self.apply_changes(p, action["params"])
        s = self.replayer.summary(p, seeds, regimes, held_out=True)
        return {"ok": not s["score_failed"] and s["unexpected_trips"] == 0, "score": s["score"],
                "metrics": {"per_regime": s["per_regime"], "unexpected_trips": s["unexpected_trips"],
                            "seeds": s["seeds"], "regimes": regimes, "params": p, "params_hash": s["params_hash"],
                            "determinism_hash": s["determinism_hash"], "label": s["label"]}}

    # ------------------------------------------------------------ shock table (OP3)
    def shock_table(self, state, cents):
        e = self._engine(state)
        snap = e.snap if e.snap is not None else g.build_snapshot()
        S_ref = snap["S_ref"]
        S_new = S_ref + cents * 16
        ds = g.ds_raw(S_new, S_ref)
        lam, beta = g.VOL_LAMBDA, g.VOL_BETA
        S0, S1 = S_ref / 1600.0, S_new / 1600.0
        thr = round(state["session_params"]["edge_threshold_ticks"] * 256)
        h = g.MM_HALF_SPREAD_TICKS
        rows, loads = [], []
        for c, k in enumerate(snap["contracts"]):
            K, is_call = snap["strikes"][c % 4], c < 4
            tn = g.reprice(k["theo_0"], k["delta_adj"], k["gamma_c"], ds)
            m0, m1 = math.log(K / S0), math.log(K / S1)
            sig = g.smile_sigma(m0) + lam * (g.smile_sigma(m1) - g.smile_sigma(m0)) + beta * (S1 - S0)
            exact = g.bs(S1, K, g.T_YEARS, g.RATE, g.DIV, sig, is_call)["price"] * 100.0
            q = e.opt_q[c]
            row = {"contract": c, "strike": K, "type": "call" if is_call else "put",
                   "theo_0_ticks": k["theo_0"] / 256.0, "theo_new_ticks": tn / 256.0,
                   "exact_ticks": round(exact, 6), "reprice_error_ticks": round(tn / 256.0 - exact, 6),
                   "stale_quote": ({"bid": q["bid"], "ask": q["ask"]} if q else None)}
            if q:
                eb, es = g.edge_buy(tn, q["ask"]), g.edge_sell(tn, q["bid"])
                row.update(edge_buy_ticks=eb / 256.0, edge_sell_ticks=es / 256.0,
                           fires=("buy" if eb > thr else "sell" if es > thr else None))
            theo_t = int(math.floor(exact + 0.5))                          # OP4 (sim S5)
            bid = max(1, theo_t - h)
            ask = max(bid + 1, theo_t + h)
            row["requote"] = {"bid": bid, "ask": ask}
            rows.append(row)
            if 0 < abs(cents) <= 255:
                d = 1 if cents > 0 else 0
                for is_ask, px in ((0, bid), (1, ask)):
                    loads.append({"addr": (d << 12) | (abs(cents) << 4) | (c << 1) | is_ask, "data": px & 0xFFFF})
        r04 = abs(S_new - S_ref) > g.JUMP_LIMIT_RAW
        return {"jump_cents": cents, "S_ref_cents": S_ref / 16.0, "S_new_cents": S_new / 16.0,
                "edge_threshold_ticks": thr / 256.0, "rows": rows,
                "r04_blocks": r04, "r04_limit_cents": g.JUMP_LIMIT_CENTS,
                "io_shock_rows": loads if loads else None,
                "io_shock_note": None if loads else "jump size not representable on SW[7:0] (1..255 cents)",
                "snapshot": "active twin snapshot" if e.snap is not None else "golden.build_snapshot() at SPOT (no "
                                                                              "snapshot yet)"}


def compare(base, alt):
    out = {"score": round(alt["score"] - base["score"], 6), "unexpected_trips": alt["unexpected_trips"] - base[
        "unexpected_trips"], "per_regime": {}}
    for r, b in base["per_regime"].items():
        a = alt["per_regime"][r]
        out["per_regime"][r] = {k: round(a[k] - b[k], 6) for k in ("mean_pnl", "p25_pnl", "mean_max_drawdown",
                                                                   "fills", "hard_trips")}
    return out
