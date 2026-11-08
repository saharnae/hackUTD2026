"""Proposal agent: drafts an 0x12 change for a human. It never builds or sends a frame.

Candidates come from Engine.plan_actions (top Optuna trials, filtered by a held-out replay); the LLM picks
and calls propose_config (authorize() checks the allowlist and ranges). Then it writes the proposal up."""

from .base import Agent


class Proposal(Agent):
    role = "proposal"
    system = ("You are the Proposal agent. Draft at most one 0x12 config change for a human to accept or deny, "
              "using only the allowlisted soft parameters. Gate limits cannot be changed. Prefer a candidate "
              "that passed the held-out replay. Say clearly that results are SIMULATED.")

    def propose(self, observed, incidents, forecast):
        plan = self.b.plan(self.actor)
        payload = {"incidents": incidents, "forecast_crossings": forecast["crossings"],
                   "current_params": self.b.current_tunables(),
                   "baseline": {"score": plan["baseline"]["score"], "ok": plan["baseline"]["ok"]},
                   "candidates": [{"params": c["action"]["params"], "source": c["action"]["source"],
                                   "score": c["evaluation"]["score"], "improves": c["improves"]}
                                  for c in plan["candidates"]],
                   "filtered_out": len(plan["filtered_out"])}
        before = len(self.tools.drafts)
        res = self.converse(payload, ("propose_config", "what_if", "replay"),
                            context={"incident": incidents[0]["id"] if incidents else None})
        return {"proposal_ids": self.tools.drafts[before:], "plan": payload, "content": res["content"]}

    def write(self, pid, comparison, notes=""):
        p = self.b.proposals.get(pid)
        res = self.converse({"proposal": {k: p[k] for k in ("id", "changes", "current", "rationale")},
                             "comparison": comparison, "stress_notes": notes}, (), role="proposal_write")
        write_up = {"summary": res["content"], "changes": p["changes"], "current": p["current"],
                    "held_out_comparison": comparison, "label": "SIMULATED; no profit claim",
                    "human_action": f"POST /proposals/{pid}/accept or /deny (a named human decides)"}
        return self.b.finalize_proposal(self.actor, pid, write_up)
