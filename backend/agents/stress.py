"""Stress-test agent: simulates the current config and each proposed fix on held-out seeds (60-79 only).

The comparison itself is deterministic code (Backend.compare_proposal -> pack.what_if(kind=config) ->
real evaluator). The LLM may ask for extra evidence (what_if / replay); those calls are seed-guarded too."""

from .base import Agent


class StressTest(Agent):
    role = "stress"
    system = ("You are the Stress-test agent. Proposed fixes have been replayed against the current config on "
              "held-out seeds. You may request extra what_if or replay runs (held-out seeds 60-79; seeds 80+ are "
              "never available). Summarize the evidence plainly. All numbers are SIMULATED.")

    def compare(self, proposal_ids, incident):
        comps = {pid: self.b.compare_proposal(self.actor, pid) for pid in proposal_ids}
        extra = self.converse({"incident": incident, "comparisons": comps}, ("what_if", "replay"))
        return {"comparisons": comps, "notes": extra["content"], "extra_runs": extra["tool_calls"]}
