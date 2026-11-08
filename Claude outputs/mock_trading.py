"""Mock trading adapter: stands in for Varish's real TradingAdapter until it lands.

Fakes the FPGA status (0x20) / log (0x21) frames, a gate trip, a stale feed, and a
simulator. Everything is SIMULATED and labelled that way.
"""
from __future__ import annotations

import hashlib
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
        self.config = {"order_size": 10, "quote_width_bps": 8, "stale_feed_ms": 500,
                       "max_delta": 50, "max_notional": 250_000, "max_orders_per_s": 40}
        self.gate = {"armed": True, "latched": False, "trip_reason": None, "killed": False}
        self.feed_age_ms = 120
        self.bad_frames_1m = 0
        self.token_bucket = 18
        self._log: list[dict] = []
        self._audit("boot", {"config": dict(self.config)})

    # ---- demo hooks (not part of the contract) ----
    def inject(self, kind: str) -> dict:
        if kind == "gate_trip":
            self.gate.update(latched=True, trip_reason="R07 net delta 61 > limit 50")
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
        return {
            "source": "SIMULATED (mock adapter: twin for gate, fake 0x20/0x21 frames)",
            "clock_s": round(time.time() - self.t0, 1),
            "gate": dict(self.gate),
            "token_bucket": self.token_bucket,
            "bad_frames_1m": self.bad_frames_1m,
            "feed_age_ms": self.feed_age_ms,
            "position": {"symbol": "EOG options", "net_delta": 61 if self.gate["latched"] else 34,
                         "open_orders": 0 if self.gate["killed"] else 6},
            "pnl_today": -1840.0 if self.gate["latched"] else 420.0,
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
        acts = [Action("kill", "Request kill (frame 0x15): cancel all orders, stop quoting",
                       {"frame": "0x15"}, True)]
        if incident.kind in ("gate_trip", "low_token_bucket"):
            acts += [
                Action("size_down", f"Cut order size {c['order_size']} -> {max(1, c['order_size'] - 4)}",
                       {"frame": "0x12", "order_size": max(1, c["order_size"] - 4)}, False),
                Action("widen_quotes", f"Widen quotes {c['quote_width_bps']} -> {c['quote_width_bps'] + 4} bps",
                       {"frame": "0x12", "quote_width_bps": c["quote_width_bps"] + 4}, False),
                Action("raise_delta", f"Raise max delta {c['max_delta']} -> {c['max_delta'] + 30}",
                       {"frame": "0x12", "max_delta": c["max_delta"] + 30}, False),
            ]
        if incident.kind in ("stale_feed", "bad_frames"):
            acts += [
                Action("tighten_stale", f"Stale-feed timeout {c['stale_feed_ms']} -> 300 ms",
                       {"frame": "0x12", "stale_feed_ms": 300}, False),
                Action("widen_quotes", f"Widen quotes {c['quote_width_bps']} -> {c['quote_width_bps'] + 4} bps",
                       {"frame": "0x12", "quote_width_bps": c["quote_width_bps"] + 4}, False),
            ]
        return acts

    def simulate(self, state: dict, action: Optional[Action], seeds: list[int]) -> Outcome:
        # effect multipliers on (pnl, drawdown, trips) vs. baseline; purely synthetic
        eff = {None: (1.0, 1.0, 1.0), "kill": (0.0, 0.15, 0.0), "size_down": (0.7, 0.55, 0.4),
               "widen_quotes": (0.85, 0.7, 0.6), "raise_delta": (1.15, 1.6, 1.3),
               "tighten_stale": (0.95, 0.65, 0.5)}[action.id if action else None]
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
        return Outcome(metrics={"seeds_run": len(per_seed)}, in_sample=agg(ins), out_of_sample=agg(oos),
                       source="SIMULATED (mock simulator, not FPGA timing)")

    def invariants(self) -> list[str]:
        return ["Supervisor may request a kill (0x15) without a human; it only reduces risk.",
                "Config changes (0x12) need a named human approver.",
                "No action may arm the gate or clear a latched trip.",
                "No action may change a hardware limit (max_delta, max_notional, max_orders_per_s).",
                "No action may raise exposure (order size up)."]

    def check(self, action: Action) -> tuple[bool, str]:
        p = action.params
        if p.get("arm") or p.get("clear_latch"):
            return False, "Refused: only a human may arm the gate or clear a latched trip."
        touched = HARDWARE_LIMITS.intersection(p)
        if touched:
            return False, f"Refused (R07 authority): cannot change hardware limit {', '.join(sorted(touched))}."
        if p.get("order_size", 0) > self.config["order_size"]:
            return False, "Refused: raises exposure (order size up)."
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
