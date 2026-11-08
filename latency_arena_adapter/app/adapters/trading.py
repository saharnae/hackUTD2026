"""TradingAdapter: the real ScenarioAdapter for the Nemotron agent core. SIMULATED arena.

It WRAPS the existing Latency Arena backend (backend/runtime.py Backend, backend/packs/options, authority.py,
audit.py, datasource.py TwinSource, packs/options/configmap.py). Nothing here re-implements the twin, the
gate, the evaluator, the authority or the audit chain. The backend is loaded under the alias `la_backend`
(see _la_backend.py) so it cannot clash with the teammate's own `backend` package.

ASSUMPTIONS (TA, all TODO(FINAL) unless noted):
  TA1. observe() keys are the mock's. Sources:
         clock_s, gate.*, token_bucket, pnl_today      decoded 0x20 status frame (twin)
         position.net_delta, feed_age_ms, config       twin state (engine), NOT in any frame: on the real
                                                       board these would be null (net_delta = signed
                                                       sum(pos * delta_adj) + shares/100, the R09 quantity)
         bad_frames_1m, position.open_orders           null: no frame field and no honest twin value
                                                       (UNSOURCED_KEYS). 0x20 frames_bad is cumulative, not
                                                       per minute, and the twin does not model it.
       pnl_today = 0x20 equity_x6400 / 6400 = mark-to-model equity since the twin session started (one
       ~600 ms SIMULATED session, not a trading day).
  TA2. Action ids: the mock file uses size_down / widen_quotes / raise_delta / tighten_stale. Kept:
       size_down (-> size_cap_base, size_cap_max) and raise_delta (refused demo). Added: raise_edge
       (-> edge_threshold_ticks) and raise_min_move (-> min_move_cents). Dropped: widen_quotes (market-making
       knob, no 0x12 field) and tighten_stale (the stale limit is R01, a hardware limit). The mock has no
       tighten_hedge; hedge_band_shares has no 0x12 field either (configmap CM1).
  TA3. Step sizes: raise_edge +0.5 tick, raise_min_move +1 cent, size_down base -1 / max -2; all clipped to
       the 0x12 decoder ranges, and an action is omitted when it cannot move.
  TA4. raise_delta demo value = 2 x NET_DELTA_LIMIT (R09). It is ALWAYS refused; only offered on gate_trip
       and when DEMO_REFUSAL_ACTION is "true" (default "true").
  TA5. simulate(): fresh seeded start of each seed < 80 with state["config"] (falls back to the live config)
       plus the action's overrides, through Backend.replay (seed guard + real evaluator + cache by params
       hash, regime, seed). It is NOT a replay from the live state. A kill is simulated as the same fresh
       start with the engine's kill input raised at the twin clock of `state` (engine.run_session kill_at_us).
       Regimes: SIM_REGIMES (default normal + feed_fault; env LA_ADAPTER_REGIMES). Seeds >= 80 never run.
  TA6. check(): exposure increases refused = size_cap_base or size_cap_max above the current value.
       Whether max_orders_per_event up also counts is open.
  TA7. inject("gate_trip") power-cycles the twin onto the feed_fault regime of the current seed (a human
       operator path, actor human:demo_operator) and advances until the engine latches R03 on a real
       sequence gap / duplicate. inject("stale_feed") is NOT supported: the twin replays a fixed seeded
       stream and its heartbeats are always valid (engine E14), so a genuine stale feed cannot be produced
       through the backend; it returns ok False and says so.
  TA8. audit_log() entries: {t, event, data, prev, hash} = backend record {ts, action, {actor, index,
       payload}, prev_hash, hash}. t is the backend's ISO-8601 UTC timestamp (the mock used seconds).
  TA9. Drawdowns are positive dollars (engine convention); the mock's were negative numbers.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Optional, Tuple

from ..interface import Action, ApplyResult, Incident, Outcome
from . import _la_backend as LB

RT = LB.sub("runtime")
CM = LB.sub("packs.options.configmap")
RP = LB.sub("packs.options.replay")
TW = LB.sub("packs.options.twin")
LV = LB.sub("packs.base")
ERR = LB.sub("errors")
import golden as g  # noqa: E402  (on sys.path via backend/_paths.py)

ACTOR = "supervisor:trading_adapter"
OPERATOR = "human:demo_operator"                    # inject() is a presenter hook  TODO(FINAL)
SYMBOL = "SPY"
SOURCE = "SIMULATED (software twin over golden/engine.py, decoded 0x20/0x21 frames; twin timing, not FPGA latency)"
SIM_SOURCE = "SIMULATED (golden engine, twin timing not FPGA latency)"
OOS = {"evaluated": False, "note": "reserved for the one-shot final evaluation"}
OOS_FIRST = 80
UNSOURCED_KEYS = ("bad_frames_1m", "position.open_orders")
TWIN_ONLY_KEYS = ("position.net_delta", "feed_age_ms", "config")
DEFAULT_REGIMES = ("normal", "feed_fault")           # TA5  TODO(FINAL)
DEFAULT_WORKERS = 4                                  # TODO(FINAL)
EDGE_STEP, MIN_MOVE_STEP, BASE_STEP, MAX_STEP = 0.5, 1, 1, 2     # TA3  TODO(FINAL)
DEMO_DELTA = 2 * g.NET_DELTA_LIMIT                   # TA4  TODO(FINAL)
RULE_TEXT = {
    "R01": "now - snapshot flip time > 250,000 us", "R02": "ref_seq ahead of the feed or > 64 behind",
    "R03": "sequence gap, repeat or bad CRC since the last resync (latching)",
    "R04": f"|S - S_ref| > {g.JUMP_LIMIT_CENTS} cents", "R05": "quote not sane", "R06": "price outside the band",
    "R07": "order qty above the max", "R08": f"|position| > {g.MAX_POSITION} per contract",
    "R09": f"|net delta| > {g.NET_DELTA_LIMIT}", "R10": f"|net gamma| > {g.NET_GAMMA_LIMIT_X100 / 100}",
    "R11": f"|net vega| > ${g.NET_VEGA_LIMIT} per vol point",
    "R12": f"> {g.MAX_GATE_ORDERS_PER_EVENT} orders in one event or token bucket empty (latching)",
    "R13": f"equity below -${g.DRAWDOWN_LIMIT_DOLLARS} (latching)",
    "R14": f"no valid heartbeat for {g.HEARTBEAT_US} us (latching)", "R15": "kill pin or KILL frame (latching)",
}
RULE_LIMIT = {"R01": g.STALE_US, "R02": g.SYNC_MAX_BEHIND, "R04": g.JUMP_LIMIT_CENTS, "R08": g.MAX_POSITION,
              "R09": g.NET_DELTA_LIMIT, "R10": g.NET_GAMMA_LIMIT_X100 / 100.0, "R11": g.NET_VEGA_LIMIT,
              "R12": g.MAX_GATE_ORDERS_PER_EVENT, "R13": g.DRAWDOWN_LIMIT_DOLLARS, "R14": g.HEARTBEAT_US}
CHANNEL_OF_RULE = {"R01": "snapshot_age_us", "R04": "jump_cents", "R08": "max_abs_position", "R09": "net_delta",
                   "R10": "net_gamma", "R11": "net_vega_dollars", "R12": "tokens_used", "R13": "loss_dollars",
                   "R14": "heartbeat_age_us"}


def _env_true(name, default="true"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _kill_session(args):
    """Module level so a process pool can pickle it. One fresh seeded session with the kill input raised."""
    seed, regime, params, kill_at_us = args
    import engine as EN
    import options_model as om
    r = EN.run_session(om.OptionsMarketScenario(seed, regime), params, kill_at_us)
    r["seed"] = seed
    return regime, seed, r


def _sentence(reason):
    reason = str(reason).strip()
    reason = reason[0].upper() + reason[1:] if reason else reason
    return reason if reason.endswith(".") else reason + "."


class TradingAdapter:
    name = "trading"

    def __init__(self, backend=None, seed: Optional[int] = None, regime: Optional[str] = None,
                 audit_path: Optional[str] = None, workers: Optional[int] = None,
                 sim_regimes: Optional[Tuple[str, ...]] = None):
        env_reg = os.environ.get("LA_ADAPTER_REGIMES")
        self.sim_regimes = tuple(sim_regimes or (env_reg.split(",") if env_reg else DEFAULT_REGIMES))
        self.workers = int(workers or os.environ.get("LA_ADAPTER_WORKERS") or DEFAULT_WORKERS)
        if backend is None:
            path = audit_path or os.environ.get("LA_ADAPTER_AUDIT_LOG") or os.path.join(
                LB.repo_root(), "latency_arena_adapter", "audit_trading.jsonl")
            backend = RT.Backend(audit_path=path, seed=seed, regime=regime, workers=self.workers,
                                 held_out_seeds=(60, 79), stress_regimes=self.sim_regimes)
        self.b = backend
        st = self.b.source.state
        self._seed0, self._regime0 = st["seed"], st["regime"]
        self._last = None
        self._kill_cache: Dict[tuple, dict] = {}
        self.engine_seeds_dispatched: List[int] = []           # every seed this adapter sends to an engine
        self.b.audit.append(ACTOR, "boot", {"adapter": "TradingAdapter", "source": SOURCE, "seed": self._seed0,
                                            "regime": self._regime0, "sim_regimes": list(self.sim_regimes)})

    # ------------------------------------------------------------------ helpers
    def _tunables(self):
        return self.b.current_tunables()

    def _config(self):
        cfg = dict(self._tunables())
        cfg["hedge_band_shares"] = self.b.pack.hedge_band_shares
        return cfg

    # ------------------------------------------------------------------ contract
    def observe(self) -> dict:
        obs = self.b.status(ACTOR)                                 # decodes 0x20 (and queues 0x21)
        self.b.log(ACTOR, 50)
        self._last = obs
        st, s = obs["status"], self.b.source.state
        bits = st["state_bits"]
        rule = CM.rule_id(st["trip_reason"])
        e = self.b.pack.engine_at(s)
        net_delta = round(e.net_delta_shares() / 100.0, 6) if e.snap is not None else None
        sc = TW.scenario(s["seed"], s["regime"])
        times = [m["t"] for m in sc.messages if m["t"] <= s["t_us"]]
        feed_age_ms = round((s["t_us"] - times[-1]) / 1000.0, 3) if times else None
        return {
            "source": SOURCE,
            "clock_s": st["now_us"] / 1e6,
            "gate": {"armed": bool(bits["armed"]), "latched": bool(bits["tripped"]),
                     "trip_reason": f"{rule} {CM.RULE_NAMES.get(rule)}" if rule else None,
                     "killed": bool(bits["kill_pin"])},
            "token_bucket": st["tokens"],
            "bad_frames_1m": None,
            "feed_age_ms": feed_age_ms,
            "position": {"symbol": SYMBOL, "net_delta": net_delta, "open_orders": None},
            "pnl_today": round(st["equity_dollars"], 6),
            "config": self._config(),
        }

    def detect(self, state: dict) -> List[Incident]:
        at = float(state["clock_s"])
        obs = self._last or self.b.status(ACTOR)
        ch, lim, st = obs["channels"], obs["limits"], obs["status"]
        out = []
        gate = state["gate"]
        if gate["latched"]:
            rule = (gate["trip_reason"] or "").split(" ")[0] or CM.rule_id(st["trip_reason"])
            c = CHANNEL_OF_RULE.get(rule)
            out.append(Incident("inc-gate", "gate_trip", 3, {
                "rule": rule, "rule_name": CM.RULE_NAMES.get(rule), "rule_text": RULE_TEXT.get(rule),
                "limit": RULE_LIMIT.get(rule), "value": ch.get(c) if c else None, "channel": c,
                "latching": True, "killed": gate["killed"], "source": "0x20 trip_reason (golden gate_eval in the twin)"},
                at))
        for rule, kind, iid in (("R01", "stale_feed", "inc-feed"), ("R14", "stale_heartbeat", "inc-heartbeat")):
            c = CHANNEL_OF_RULE[rule]
            lv = LV.level_of(ch.get(c), lim[c])
            if lv in ("high", "trip"):
                out.append(Incident(iid, kind, 2, {"rule": rule, "rule_name": CM.RULE_NAMES.get(rule),
                                                   "channel": c, "value": ch.get(c), "level": lv,
                                                   "limit": RULE_LIMIT[rule]}, at))
        if st.get("frames_bad"):
            out.append(Incident("inc-frames", "bad_frames", 2, {"frames_bad_total": st["frames_bad"],
                                                                "source": "0x20 frames_bad (cumulative, saturating)"}, at))
        lv = LV.level_of(ch.get("tokens_used"), lim["tokens_used"])
        if lv:
            out.append(Incident("inc-tokens", "low_token_bucket", 1 if lv == "warn" else 2,
                                {"rule": "R12", "tokens": state["token_bucket"], "level": lv, "bucket": 20}, at))
        for c in ("net_delta", "loss_dollars", "jump_cents", "max_abs_position", "net_vega_dollars", "net_gamma"):
            lv = LV.level_of(ch.get(c), lim[c])
            if lv:
                rule = self.b.pack.CHANNEL_RULES[c]
                out.append(Incident(f"inc-{c}", f"limit_{lv}", 1 if lv == "warn" else 2,
                                    {"rule": rule, "rule_name": CM.RULE_NAMES.get(rule), "channel": c,
                                     "value": ch.get(c), "level": lv, "limit": lim[c][lv],
                                     "gate_limit": RULE_LIMIT.get(rule)}, at))
        return sorted(out, key=lambda i: (-i.severity, i.id))

    def actions(self, incident: Incident) -> List[Action]:
        acts = [Action("kill", "Request kill (frame 0x15): stop all trading now; re-arming needs a human at the board",
                       {"frame": "0x15"}, True)]
        if incident.kind not in ("gate_trip", "low_token_bucket") and not incident.kind.startswith("limit_"):
            return acts                                            # stale / bad frames: no 0x12 knob fixes these
        cur = self._tunables()
        t = CM.TUNABLE
        e_new = CM.quantize(dict(cur, edge_threshold_ticks=min(t["edge_threshold_ticks"]["hi"],
                                                               cur["edge_threshold_ticks"] + EDGE_STEP)))
        if e_new["edge_threshold_ticks"] > cur["edge_threshold_ticks"]:
            acts.append(Action("raise_edge", f"Raise the edge threshold {cur['edge_threshold_ticks']} -> "
                                             f"{e_new['edge_threshold_ticks']} ticks (fewer, larger-edge trades)",
                               {"frame": "0x12", "edge_threshold_ticks": e_new["edge_threshold_ticks"]}, False))
        mm = min(t["min_move_cents"]["hi"], cur["min_move_cents"] + MIN_MOVE_STEP)
        if mm > cur["min_move_cents"]:
            acts.append(Action("raise_min_move", f"Raise the minimum underlying move {cur['min_move_cents']} -> {mm} "
                                                 "cents before re-evaluating", {"frame": "0x12", "min_move_cents": mm},
                               False))
        base = max(t["size_cap_base"]["lo"], cur["size_cap_base"] - BASE_STEP)
        mx = max(base, cur["size_cap_max"] - MAX_STEP)
        if base < cur["size_cap_base"] or mx < cur["size_cap_max"]:
            acts.append(Action("size_down", f"Cut order size caps {cur['size_cap_base']}/{cur['size_cap_max']} -> "
                                            f"{base}/{mx} contracts",
                               {"frame": "0x12", "size_cap_base": base, "size_cap_max": mx}, False))
        if incident.kind == "gate_trip" and _env_true("DEMO_REFUSAL_ACTION"):
            acts.append(Action("raise_delta", f"DEMONSTRATION of the authority limit (will be refused): raise the "
                                              f"R09 net-delta limit {g.NET_DELTA_LIMIT} -> {DEMO_DELTA}",
                               {"frame": "0x12", "net_delta_limit": DEMO_DELTA}, False))
        return acts

    # ------------------------------------------------------------------ simulate
    def simulate(self, state: dict, action: Optional[Action], seeds: List[int]) -> Outcome:
        seeds = sorted({int(s) for s in seeds})
        run = [s for s in seeds if 0 <= s < OOS_FIRST]
        skipped = [s for s in seeds if s >= OOS_FIRST]             # NEVER run
        cfg = {k: v for k, v in (state.get("config") or {}).items() if k in CM.TUNABLE} or self._tunables()
        is_kill = bool(action and action.params.get("frame") == "0x15")
        changes = dict(cfg)
        if action and not is_kill:
            changes.update({k: v for k, v in action.params.items() if k != "frame"})
        metrics = {"method": "fresh-start replay: each seed < 80 starts a new seeded session with the current "
                             "config plus the action's overrides; it is NOT a replay from the live state",
                   "action": action.id if action else None, "regimes": list(self.sim_regimes),
                   "seeds_requested": len(seeds), "seeds_run": len(run), "seeds_not_run": len(skipped),
                   "seeds_not_run_reason": "seeds >= 80 are out-of-sample, reserved for the one-shot final evaluation",
                   "timing": "twin timing, not FPGA latency"}
        if is_kill:
            metrics["kill_at_us"] = int(round(float(state.get("clock_s", 0)) * 1e6))
            metrics["method"] += "; kill = the engine's kill input raised at the observed twin clock"
            metrics["trip_note"] = ("hard_trips / mean_gate_trips / unexpected_trips include the requested R15 kill "
                                    "latch in every session (tune.score counts it as a trip)")
        try:
            parts = {"search": [s for s in run if s <= 59], "confirm": [s for s in run if s >= 60]}
            sums = {k: [self._replay(changes, rng, is_kill, metrics.get("kill_at_us")) for rng in _ranges(v)]
                    for k, v in parts.items()}
        except ERR.AuthorityError as e:
            note = {"evaluated": False, "refused": True, "note": _sentence("Refused: " + e.reason)}
            return Outcome(metrics=dict(metrics, refused=True), in_sample=note, out_of_sample=dict(OOS),
                           source=SIM_SOURCE)
        sub = {k: _agg(v) for k, v in sums.items()}
        allsum = [x for v in sums.values() for x in v]
        ins = _agg(allsum)
        ins["search"], ins["confirm"] = sub["search"], sub["confirm"]
        if allsum:
            metrics.update(params=allsum[0]["params"], params_hash=allsum[0]["params_hash"],
                           determinism_hash=_combine([x["determinism_hash"] for x in allsum]),
                           score={k: (round(sum(x["score"] * x["n_seeds"] for x in v) / sum(x["n_seeds"] for x in v), 6)
                                      if v else None) for k, v in sums.items()},
                           score_rule=allsum[0]["score_rule"])
        return Outcome(metrics=metrics, in_sample=ins, out_of_sample=dict(OOS), source=SIM_SOURCE)

    def _replay(self, changes, rng, is_kill, kill_at_us):
        lo, hi = rng
        if not is_kill:
            self.engine_seeds_dispatched.extend(range(lo, hi + 1))
            return self.b.replay(ACTOR, changes, (lo, hi), list(self.sim_regimes))
        # kill: same fresh start, engine kill input raised at kill_at_us (not expressible through evaluator.run)
        self.b.authorize(ACTOR, "replay", {"params": changes, "seeds": [lo, hi], "kill_at_us": kill_at_us})
        seeds = self.b._seed_guarded(ACTOR, "replay", lambda: RP.guard_seeds(range(lo, hi + 1)))
        self.b._check_params(ACTOR, "replay", changes)
        params = self.b.pack.apply_changes(self.b.pack.current_params(self.b.source.state), changes)
        ph = RP.params_hash(params)
        jobs = [(s, r, params, kill_at_us) for r in self.sim_regimes for s in seeds
                if (ph, r, s, kill_at_us) not in self._kill_cache]
        self.engine_seeds_dispatched.extend(j[0] for j in jobs)
        if jobs:
            if self.workers > 1 and len(jobs) > 1:
                with ProcessPoolExecutor(max_workers=self.workers) as ex:
                    out = list(ex.map(_kill_session, jobs, chunksize=max(1, len(jobs) // (4 * self.workers))))
            else:
                out = [_kill_session(j) for j in jobs]
            for regime, s, res in out:
                self._kill_cache[(ph, regime, s, kill_at_us)] = res
        results = {r: [self._kill_cache[(ph, r, s, kill_at_us)] for s in seeds] for r in self.sim_regimes}
        return RP.summarize(params, seeds, list(self.sim_regimes), results)

    # ------------------------------------------------------------------ rules
    def invariants(self) -> List[str]:
        hw = sorted({f"{v['rule']} {v['name']}" for v in CM.HARDWARE_LIMITS.values()})
        return [
            "The supervisor may request a kill (frame 0x15) without a human; it only reduces risk and latches R15.",
            "Config changes (frame 0x12) need a named human approver; the frame is built only when that human accepts.",
            "No action may arm the gate (SW9 rising at the board) or clear a latched trip (KEY0 with ARM low).",
            "No action may change a hardware limit; the gate limits are compile-time constants: " + ", ".join(hw) + ".",
            "Only the 0x12 soft strategy parameters may change, inside the decoder's accepted ranges: "
            + ", ".join(f"{k} {v['lo']}-{v['hi']}" for k, v in CM.TUNABLE.items()) + ".",
            "No action may raise exposure: the order size caps (size_cap_base, size_cap_max) may only go down.",
            "Simulations run seeds 0-79 only; seeds 80-99 are reserved for the one-shot final evaluation.",
            "Every refusal and every applied action is written to the hash-chained audit log.",
        ]

    def check(self, action: Action) -> Tuple[bool, str]:
        p = {k: v for k, v in (action.params or {}).items() if k != "frame"}
        frame = (action.params or {}).get("frame")
        try:
            for never in ("arm", "clear_latch"):
                if action.id == never or p.get(never):
                    self.b.authorize(ACTOR, never, {"action": action.id})            # always refuses (audited)
            if frame == "0x15":
                if p:
                    self.b.authority.refuse(ACTOR, "request_kill", "a kill takes no parameters",
                                            {"action": action.id, "params": p}, "bad_params")
                self.b.authorize(ACTOR, "request_kill", {"action": action.id, "step": "check"})
                return True, "Pass: a kill (0x15) only reduces risk, so the supervisor may send it without a human."
            if frame != "0x12":
                self.b.authority.refuse(ACTOR, "apply", f"unknown frame {frame!r}; only 0x15 (kill) and 0x12 (config)",
                                        {"action": action.id}, "unknown_frame")
            cur = self._tunables()
            if not CM.check_change(p):
                up = [k for k in ("size_cap_base", "size_cap_max") if k in p and p[k] > cur[k]]
                if up:
                    self.b.authority.refuse(ACTOR, "propose_config",
                                            "it raises exposure (" + ", ".join(f"{k} {cur[k]} -> {p[k]}" for k in up) + ")",
                                            {"action": action.id, "params": p}, "raises_exposure")
            self.b.authorize(ACTOR, "propose_config", {"params": p, "current": cur, "action": action.id, "step": "check"})
            return True, "Pass: soft 0x12 parameters inside the decoder's accepted ranges; a named human must approve."
        except ERR.AuthorityError as e:
            return False, _sentence("Refused: " + e.reason)

    def apply(self, action: Action, approved_by: Optional[str]) -> ApplyResult:
        frame = (action.params or {}).get("frame")
        ok, why = self.check(action)
        if not ok:
            return ApplyResult(False, why, {"frame": frame})
        if frame == "0x15":
            r = self.b.request_kill(ACTOR, f"adapter action {action.id}")
            summ = r["observation"]["summary"] or {}
            self.b.audit.append(ACTOR, "applied", {"action": action.id, "params": action.params, "by": "supervisor"})
            rule = CM.rule_id(summ.get("trip_reason"))
            return ApplyResult(True, f"Applied (SIMULATED): KILL frame 0x15 sent to the twin; kill input set, gate "
                                     f"latched (first cause {rule}).",
                               {"frame": "0x15", "t_us": r["result"].get("t_us"), "kill": bool(summ.get("kill")),
                                "trip_reason": rule, "cost_estimate": r["cost_estimate"]})
        who = (approved_by or "").strip()
        if not who:
            self.b.audit.append(ACTOR, "refused", {"requested": "apply", "action": action.id, "kind": "needs_human",
                                                   "reason": "needs a named human approver", "payload": action.params})
            return ApplyResult(False, "Refused: this change needs a named human approver.", {"frame": "0x12"})
        changes = {k: v for k, v in action.params.items() if k != "frame"}
        prop = self.b.propose(ACTOR, changes, f"adapter action {action.id}: {action.label}")
        cmp = self.b.compare_proposal(ACTOR, prop["id"])                     # held-out 60-79 replay
        self.b.finalize_proposal(ACTOR, prop["id"], {"summary": action.label, "held_out_comparison": cmp,
                                                     "label": "SIMULATED; no profit claim"})
        acc = self.b.accept(who, prop["id"])                                 # the only 0x12 frame path
        res = acc["board_result"] or {}
        detail = {"frame": "0x12", "proposal_id": prop["id"], "fields": acc["fields"], "approved_by": who,
                  "board_applied": res.get("applied"), "frame_hex": acc.get("frame_hex")}
        if not res.get("applied"):
            self.b.audit.append(ACTOR, "refused", {"requested": "apply", "action": action.id, "kind": "board_ignored",
                                                   "reason": res.get("reason")})
            return ApplyResult(False, _sentence("Not applied: the board ignored the 0x12 frame: " + str(res.get("reason"))),
                               detail)
        self.b.audit.append(ACTOR, "applied", {"action": action.id, "params": action.params, "by": who,
                                               "proposal_id": prop["id"]})
        return ApplyResult(True, "Applied (SIMULATED): 0x12 config frame sent to the twin; it takes effect at the next "
                                 "session (after a re-arm at the board).", detail)

    def report_template(self) -> dict:
        return {"proposal": ["Incident", "Current state", "Forecast (baseline)", "Candidate fixes",
                             "Recommendation", "Hard rules"],
                "post_mortem": ["Summary", "Timeline (from audit log)", "Root cause (suspected)",
                                "Action taken and who approved", "Follow-ups"],
                "labels": {"market": "SIMULATED arena", "timing": "twin timing, not FPGA latency",
                           "results": "no profit claim"},
                "data_gaps": {"null_always": list(UNSOURCED_KEYS), "twin_only_not_in_frames": list(TWIN_ONLY_KEYS)}}

    def audit_log(self, limit: int = 50) -> List[dict]:
        recs = self.b.audit.read_all()
        recs = recs[-limit:] if limit else recs
        return [{"t": r["ts"], "event": r["action"], "data": {"actor": r["actor"], "index": r["index"],
                                                              "payload": r["payload"]},
                 "prev": r["prev_hash"], "hash": r["hash"]} for r in recs]

    def verify_chain(self) -> dict:
        return self.b.verify_audit()

    # ------------------------------------------------------------------ demo hook
    def inject(self, kind: str) -> dict:
        s = self.b.source.state
        if kind == "gate_trip":
            self.b.sim_step(OPERATOR, 0.0, True, s["seed"], "feed_fault")
            while True:
                st = self.b.source.state
                if st["summary"]["tripped"] or st["summary"]["session_over"]:
                    break
                self.b.sim_step(OPERATOR, 0.005)
            summ = self.b.source.state["summary"]
            out = {"ok": bool(summ["tripped"]), "kind": kind, "rule": CM.rule_id(summ["trip_reason"]),
                   "trip_t_us": summ["trip_t_us"], "seed": s["seed"], "regime": "feed_fault",
                   "how": "twin power-cycled onto the feed_fault stream of the same seed; the engine's feed-integrity "
                          "check latched R03 on a real sequence gap / duplicate (not a flag flip)"}
        elif kind == "stale_feed":
            out = {"ok": False, "kind": kind,
                   "reason": "not supported by the twin: it replays a fixed seeded stream and its heartbeats are always "
                             "valid (engine E14), so no genuine stale feed can be produced through the backend"}
        elif kind == "reset":
            self.b.sim_step(OPERATOR, 0.0, True, self._seed0, self._regime0)
            out = {"ok": True, "kind": kind, "seed": self._seed0, "regime": self._regime0}
        else:
            out = {"ok": False, "kind": kind, "reason": "unknown inject kind (gate_trip, stale_feed, reset)"}
        self.b.audit.append(OPERATOR, "inject", out)
        return out


# ---------------------------------------------------------------------- aggregation
def _ranges(seeds):
    out, start, prev = [], None, None
    for s in seeds:
        if start is None:
            start = prev = s
        elif s == prev + 1:
            prev = s
        else:
            out.append((start, prev))
            start = prev = s
    if start is not None:
        out.append((start, prev))
    return out


def _agg(summaries):
    if not summaries:
        return {"evaluated": False, "note": "no seeds in this split"}
    n = sum(x["n_seeds"] for x in summaries)
    regs = summaries[0]["regimes"]
    per = {}
    for r in regs:
        w = [(x["per_regime"][r], x["n_seeds"]) for x in summaries]
        per[r] = {"mean_pnl": round(sum(p["mean_pnl"] * k for p, k in w) / n, 6),
                  "mean_max_drawdown": round(sum(p["mean_max_drawdown"] * k for p, k in w) / n, 6),
                  "hard_trips": sum(p["hard_trips"] for p, _ in w), "fills": sum(p["fills"] for p, _ in w),
                  "expected_trip_regime": w[0][0]["expected_trip_regime"]}
    trips = sum(v["hard_trips"] for v in per.values())
    return {"evaluated": True, "seeds": n, "seed_ranges": [x["seeds"] for x in summaries], "regimes": list(regs),
            "mean_pnl": round(sum(v["mean_pnl"] for v in per.values()) / len(per), 6),
            "mean_max_drawdown": round(sum(v["mean_max_drawdown"] for v in per.values()) / len(per), 6),
            "mean_gate_trips": round(trips / (n * len(per)), 6),
            "unexpected_trips": sum(x["unexpected_trips"] for x in summaries),
            "per_regime": per, "units": "dollars, SIMULATED; drawdown is positive dollars"}


def _combine(hashes):
    import hashlib
    return hashlib.sha256(";".join(hashes).encode()).hexdigest()


def to_json_safe(x):
    """For tests: the interface's to_json plus a json.dumps round trip."""
    from ..interface import to_json
    return json.loads(json.dumps(to_json(x)))
