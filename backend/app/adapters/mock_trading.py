"""Mock trading adapter: stands in for Varish's real TradingAdapter until it lands.

Fakes the FPGA status (0x20) / log (0x21) frames, a gate trip, a stale feed, and a
simulator. MOCK DATA: its numbers are random multipliers and must never be shown on stage as a result.
Varish's real TradingAdapter takes rule ids, limits and actions from the gate.
"""
from __future__ import annotations

import hashlib
import os
import json
import random
import time
from typing import Optional

from ..interface import Action, ApplyResult, Incident, Outcome

HARDWARE_LIMITS = {"max_delta", "max_notional", "max_orders_per_s"}


class MockTradingAdapter:
    name = "mock_trading"

    def __init__(self):
        self.t0 = time.time()
        self.config = {"edge_threshold_bps": 12, "min_move_bps": 5, "size_cap": 10, "hedge_band": 8,
                       "stale_feed_ms": 500, "max_delta": 20, "max_notional": 250_000, "max_orders_per_s": 40}
        self.gate = {"armed": True, "latched": False, "trip_reason": None, "killed": False}
        self.feed_age_ms = 120
        self.bad_frames_1m = 0
        self.token_bucket = 18
        self._px = 584.20          # MOCK price walk so the live chart moves
        self._open_px = 584.20
        self._pnl = 420.0
        self._log: list[dict] = []
        self._audit("boot", {"config": dict(self.config)})

    # ---- demo hooks (not part of the contract) ----
    def inject(self, kind: str) -> dict:
        if kind == "gate_trip":
            self.gate.update(latched=True, trip_reason="R09 net delta 27 > limit 20")
            self.token_bucket, self.bad_frames_1m = 3, 4
        elif kind == "stale_feed":
            self.feed_age_ms, self.bad_frames_1m = 1450, 17
        elif kind == "reset":
            self.__init__()
            return {"ok": True, "kind": kind}
        self._audit("inject", {"kind": kind})
        return {"ok": True, "kind": kind}

    # ---- contract ----
    def observe(self) -> dict:
        drift = -0.04 if self.gate["latched"] else 0.01
        if not self.gate["killed"]:
            self._px = round(self._px + random.gauss(drift, 0.12), 2)
            self._pnl = round(self._pnl + random.gauss(-6 if self.gate["latched"] else 1.5, 9), 2)
        return {
            "last_price": self._px, "open_price": self._open_px,
            "source": "MOCK DATA (fake 0x20/0x21 frames, not the board)",
            "clock_s": round(time.time() - self.t0, 1),
            "gate": dict(self.gate),
            "token_bucket": self.token_bucket,
            "bad_frames_1m": self.bad_frames_1m,
            "feed_age_ms": self.feed_age_ms,
            "position": {"symbol": "SPY", "net_delta": 27 if self.gate["latched"] else 12,
                         "open_orders": 0 if self.gate["killed"] else 6},
            "pnl_today": self._pnl - (2260.0 if self.gate["latched"] else 0.0),
            "config": dict(self.config),
        }

    def detect(self, state: dict) -> list[Incident]:
        out, at = [], state["clock_s"]
        g = state["gate"]
        if g["latched"]:
            out.append(Incident("inc-gate", "gate_trip", 3, {"trip_reason": g["trip_reason"],
                                "net_delta": state["position"]["net_delta"]}, at))
        if state["feed_age_ms"] > state["config"]["stale_feed_ms"]:
            out.append(Incident("inc-feed", "stale_feed", 2, {"feed_age_ms": state["feed_age_ms"],
                                "limit_ms": state["config"]["stale_feed_ms"]}, at))
        if state["bad_frames_1m"] > 10:
            out.append(Incident("inc-frames", "bad_frames", 2, {"bad_frames_1m": state["bad_frames_1m"]}, at))
        if state["token_bucket"] < 5:
            out.append(Incident("inc-tokens", "low_token_bucket", 1, {"tokens": state["token_bucket"]}, at))
        return sorted(out, key=lambda i: -i.severity)

    def actions(self, incident: Incident) -> list[Action]:
        c = self.config
        acts = [Action("kill", "Request kill (frame 0x15): cancel all orders, stop trading",
                       {"frame": "0x15"}, True)]
        if incident.kind in ("gate_trip", "low_token_bucket"):
            acts += [
                Action("raise_edge", f"Raise edge threshold {c['edge_threshold_bps']} -> {c['edge_threshold_bps'] + 6} bps",
                       {"frame": "0x12", "edge_threshold_bps": c["edge_threshold_bps"] + 6}, False),
                Action("cut_size_cap", f"Cut size cap {c['size_cap']} -> {max(1, c['size_cap'] - 4)}",
                       {"frame": "0x12", "size_cap": max(1, c["size_cap"] - 4)}, False),
                Action("tighten_hedge", f"Tighten hedge band {c['hedge_band']} -> {max(1, c['hedge_band'] - 3)}",
                       {"frame": "0x12", "hedge_band": max(1, c["hedge_band"] - 3)}, False),
            ]
        if incident.kind in ("stale_feed", "bad_frames"):
            acts += [
                Action("raise_min_move", f"Raise minimum move {c['min_move_bps']} -> {c['min_move_bps'] + 3} bps",
                       {"frame": "0x12", "min_move_bps": c["min_move_bps"] + 3}, False),
                Action("raise_edge", f"Raise edge threshold {c['edge_threshold_bps']} -> {c['edge_threshold_bps'] + 6} bps",
                       {"frame": "0x12", "edge_threshold_bps": c["edge_threshold_bps"] + 6}, False),
            ]
        # deliberately refused on stage to show the authority limit
        if os.getenv("DEMO_REFUSAL_ACTION", "true").lower() != "false" and incident.kind == "gate_trip":
            acts.append(Action("raise_delta", f"Raise max delta {c['max_delta']} -> {c['max_delta'] + 30}",
                               {"frame": "0x12", "max_delta": c["max_delta"] + 30}, False))
        return acts

    def simulate(self, state: dict, action: Optional[Action], seeds: list[int]) -> Outcome:
        # effect multipliers on (pnl, drawdown, trips) vs. baseline; purely synthetic
        eff = {None: (1.0, 1.0, 1.0), "kill": (0.0, 0.15, 0.0), "raise_edge": (0.8, 0.6, 0.45),
               "cut_size_cap": (0.7, 0.55, 0.4), "tighten_hedge": (0.9, 0.7, 0.5),
               "raise_min_move": (0.9, 0.65, 0.5), "raise_delta": (1.15, 1.6, 1.3)}[action.id if action else None]
        per_seed = []
        for s in seeds:
            r = random.Random(s * 7919 + 13)
            base_pnl, base_dd, base_trips = r.gauss(900, 1400), abs(r.gauss(3200, 900)), r.randint(1, 5)
            per_seed.append({"seed": s, "pnl": round(base_pnl * eff[0], 1),
                             "max_drawdown": round(-base_dd * eff[1], 1),
                             "gate_trips": round(base_trips * eff[2], 2)})

        def agg(group):
            if not group:
                return {"evaluated": False, "note": "no seeds in this split"}
            n = len(group)
            return {"evaluated": True, "seeds": n,
                    "mean_pnl": round(sum(x["pnl"] for x in group) / n, 1),
                    "mean_max_drawdown": round(sum(x["max_drawdown"] for x in group) / n, 1),
                    "mean_gate_trips": round(sum(x["gate_trips"] for x in group) / n, 2)}
        ins = [x for x in per_seed if x["seed"] < 80]
        oos = [x for x in per_seed if x["seed"] >= 80]
        return Outcome(metrics={"seeds_run": len(per_seed)}, in_sample=agg(ins),
                       out_of_sample=agg(oos) if oos else {"evaluated": False, "note": "held out until final evaluation"},
                       source="MOCK DATA (random multipliers, not a result)")

    def invariants(self) -> list[str]:
        return ["Supervisor may request a kill (0x15) without a human; it only reduces risk.",
                "Config changes (0x12) need a named human approver.",
                "No action may arm the gate or clear a latched trip.",
                "No action may change a hardware limit (max_delta, max_notional, max_orders_per_s).",
                "No action may raise exposure (size cap up)."]

    def check(self, action: Action) -> tuple[bool, str]:
        p = action.params
        if p.get("arm") or p.get("clear_latch"):
            return False, "Refused: only a human may arm the gate or clear a latched trip."
        touched = HARDWARE_LIMITS.intersection(p)
        if touched:
            return False, f"Refused (authority limit): cannot change hardware limit {', '.join(sorted(touched))}."
        if p.get("size_cap", 0) > self.config["size_cap"]:
            return False, "Refused: raises exposure (size cap up)."
        return True, "Pass: within R01-R15 and supervisor authority."

    def apply(self, action: Action, approved_by: Optional[str]) -> ApplyResult:
        ok, why = self.check(action)
        if not ok:
            self._audit("refused", {"action": action.id, "reason": why, "by": approved_by})
            return ApplyResult(False, why)
        if not action.reduces_risk and not approved_by:
            self._audit("refused", {"action": action.id, "reason": "needs human approval"})
            return ApplyResult(False, "Refused: this change needs a named human approver.")
        if action.id == "kill":
            self.gate["killed"] = True
        else:
            for k, v in action.params.items():
                if k in self.config:
                    self.config[k] = v
        self._audit("applied", {"action": action.id, "params": action.params, "by": approved_by or "supervisor"})
        return ApplyResult(True, "Applied (SIMULATED).", {"frame": action.params.get("frame")})

    def report_template(self) -> dict:
        return {"proposal": ["Incident", "Current state", "Forecast (baseline)", "Candidate fixes",
                             "Recommendation", "Hard rules"],
                "post_mortem": ["Summary", "Timeline (from audit log)", "Root cause (suspected)",
                                "Action taken and who approved", "Follow-ups"]}

    def audit_log(self, limit: int = 50) -> list[dict]:
        return self._log[-limit:]

    def _audit(self, event: str, data: dict):
        prev = self._log[-1]["hash"] if self._log else "0" * 16
        entry = {"t": round(time.time() - self.t0, 2), "event": event, "data": data, "prev": prev}
        entry["hash"] = hashlib.sha256((prev + json.dumps(entry, sort_keys=True, default=str)).encode()).hexdigest()[:16]
        self._log.append(entry)
