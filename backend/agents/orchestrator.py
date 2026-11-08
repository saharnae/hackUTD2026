"""Orchestrator: observe -> detect -> forecast -> propose -> compare -> write -> (human decides) -> review.

Stops on any hard-rule trip:
  - the gate latched (a latching trip R03/R12-R15 in the 0x20 status): it stops advancing the twin and runs
    one incident cycle, then waits for the human;
  - any HardRuleError (an authority refusal or a seed-guard refusal) from any agent: it halts immediately.
A non-latching incident at "high" or above also runs one incident cycle and stops for the human.
"""

from .. import config as C
from ..errors import HardRuleError
from .monitor import Monitor
from .postmortem import PostMortem
from .proposal import Proposal
from .stress import StressTest
from .tools import ToolBox

ACTOR = "supervisor:orchestrator"


class Orchestrator:
    def __init__(self, backend, llm):
        self.b, self.llm = backend, llm
        self.tools = ToolBox(backend)
        self.monitor = Monitor(backend, llm, self.tools)
        self.stress = StressTest(backend, llm, self.tools)
        self.proposal = Proposal(backend, llm, self.tools)
        self.postmortem = PostMortem(backend, llm, self.tools)
        self.last_run = None

    def run(self, dt_s=0.01, max_steps=60, horizon_s=None):
        start = self.b.audit.append(ACTOR, "supervisor_run_started", {"dt_s": dt_s, "max_steps": max_steps})
        run = {"start_index": start["index"], "phases": [], "incidents": [], "proposals": [], "status": None,
               "stop_reason": None, "report": None, "label": "SIMULATED"}
        self.last_run = run
        try:
            for i in range(int(max_steps)):
                seen = self.monitor.observe(dt_s)
                run["phases"].append({"phase": "observe", "step": i, "t_us": (seen["obs"]["status"] or {}).get("now_us")})
                incidents = self.monitor.detect(seen["obs"])
                actionable = [x for x in incidents if x["severity"] >= 2]
                run["phases"].append({"phase": "detect", "step": i, "incidents": [x["id"] for x in incidents]})
                if not actionable:
                    if (seen["obs"].get("summary") or {}).get("session_over"):
                        run["status"], run["stop_reason"] = "session_over", "twin session ended with no incident"
                        break
                    continue
                hard = any(x["latched"] for x in actionable)
                run["incidents"] = actionable
                self.b.audit.append(ACTOR, "incident_detected", {"id": actionable[0]["id"], "rule": actionable[0]["rule"],
                                                                 "latched": hard, "t_us": actionable[0]["t_us"],
                                                                 "all": [x["id"] for x in actionable]})
                tri = self.monitor.triage(seen, actionable)
                run["phases"].append({"phase": "triage", "kills": sum(1 for c in tri["tool_calls"]
                                                                      if c["name"] == "request_kill"),
                                      "notes": tri["content"]})
                fc = self.b.forecast(ACTOR, horizon_s or C.FORECAST_HORIZON_S)
                run["phases"].append({"phase": "forecast", "crossings": fc["crossings"], "label": fc["label"]})
                prop = self.proposal.propose(seen, actionable, fc)
                run["phases"].append({"phase": "propose", "proposal_ids": prop["proposal_ids"],
                                      "candidates": len(prop["plan"]["candidates"])})
                cmp = self.stress.compare(prop["proposal_ids"], actionable[0])
                run["phases"].append({"phase": "compare", "proposal_ids": list(cmp["comparisons"]),
                                      "extra_runs": len(cmp["extra_runs"])})
                for pid in prop["proposal_ids"]:
                    self.proposal.write(pid, cmp["comparisons"][pid], cmp["notes"])
                run["phases"].append({"phase": "write", "proposal_ids": prop["proposal_ids"]})
                run["proposals"] = prop["proposal_ids"]
                run["status"] = "awaiting_human" if prop["proposal_ids"] else "no_proposal"
                run["stop_reason"] = ("hard-rule trip: gate latched " + str(actionable[0]["rule"])) if hard else \
                    f"incident {actionable[0]['id']} at severity {actionable[0]['severity']}"
                break
            else:
                run["status"], run["stop_reason"] = "no_incident", f"no incident in {max_steps} steps"
        except HardRuleError as e:
            run["status"], run["stop_reason"] = "halted", f"hard rule refused: {e.reason}"
            self.b.audit.append(ACTOR, "supervisor_halted", {"reason": e.reason, "detail": e.detail})
        self.b.audit.append(ACTOR, "supervisor_run_finished", {"status": run["status"], "stop_reason": run["stop_reason"],
                                                               "proposals": run["proposals"]})
        if run["status"] != "awaiting_human":
            run["report"] = self.review()["id"]                       # nothing for a human to decide: review now
        return run

    def review(self, since_index=None):
        """After the human decides (or when there is nothing to decide): post-mortem report."""
        if since_index is None:
            since_index = self.last_run["start_index"] if self.last_run else 0
        inc = (self.last_run or {}).get("incidents") or [{}]
        return self.postmortem.report(since_index, inc[0].get("id"))
