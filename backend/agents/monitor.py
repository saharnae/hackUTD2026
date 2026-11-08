"""Monitor: reads the 0x20 status and 0x21 log frames, detects incidents, and lets the LLM triage
(its only side-effecting tool is request_kill, which only reduces risk)."""

from ..packs import level_of
from ..packs.options import configmap as CM
from ..packs.options.pack import OptionsPack
from .base import Agent

SEVERITY = {"warn": 1, "high": 2, "trip": 2}       # trip of a non-latching channel = the gate is blocking


class Monitor(Agent):
    role = "monitor"
    system = ("You are the Monitor for a SIMULATED options risk gate. You receive decoded 0x20 status and 0x21 "
              "log frames and detected incidents. Trading is already stopped if the gate latched. Call request_kill "
              "only if risk is rising and the gate has not latched. You cannot arm, clear a latch or change a limit.")

    def observe(self, dt_s):
        obs = self.b.advance(self.actor, dt_s)               # twin time passes; reads 0x20 + 0x21
        log = self.b.log(self.actor, 50)
        return {"obs": obs, "log": log["records"]}

    def detect(self, obs):
        out = []
        st = obs.get("status") or {}
        bits = st.get("state_bits", {})
        if bits.get("tripped"):
            rule = CM.rule_id(st.get("trip_reason"))
            out.append({"id": f"inc-trip-{rule}", "kind": "gate_trip", "severity": 3, "latched": True,
                        "rule": rule, "rule_name": CM.RULE_NAMES.get(rule), "t_us": st.get("now_us"),
                        "detail": {"trip_reason": st.get("trip_reason"), "tokens": st.get("tokens"),
                                   "equity_dollars": st.get("equity_dollars"), "feed_seq": st.get("feed_seq"),
                                   "ref_seq": st.get("ref_seq"), "kill_pin": bits.get("kill_pin")}})
        lims = obs.get("limits") or {}
        for ch, lim in sorted(lims.items()):
            v = (obs.get("channels") or {}).get(ch)
            lv = level_of(v, lim)
            if lv:
                rule = OptionsPack.CHANNEL_RULES.get(ch)
                out.append({"id": f"inc-{ch}", "kind": "limit_" + lv, "severity": SEVERITY[lv], "latched": False,
                            "rule": rule, "rule_name": CM.RULE_NAMES.get(rule), "t_us": st.get("now_us"),
                            "detail": {"channel": ch, "value": v, "level": lv, "limit": lim}})
        return sorted(out, key=lambda x: (-x["severity"], x["id"]))

    def triage(self, observed, incidents):
        payload = {"incidents": incidents, "status": observed["obs"]["status"],
                   "channels": observed["obs"]["channels"], "recent_log": observed["log"][-10:],
                   "source": observed["obs"]["source"]}
        return self.converse(payload, ("get_status", "get_log", "request_kill"))
