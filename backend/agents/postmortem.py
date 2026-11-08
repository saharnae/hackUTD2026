"""Post-mortem agent: reads the audit log and writes an incident report.

Facts are extracted deterministically from the audit records (and the chain is verified); the LLM only
adds the narrative. The report is stored in the backend and its id is written to the audit log."""

from .base import Agent

FACT_ACTIONS = ("supervisor_run_started", "incident_detected", "kill_frame_sent", "proposal_drafted",
                "proposal_compared", "proposal_ready", "config_frame_sent", "proposal_denied", "refused",
                "supervisor_halted", "supervisor_run_finished")


class PostMortem(Agent):
    role = "postmortem"
    system = ("You are the Post-mortem agent. Write a short, factual incident report from the audit facts given. "
              "Do not invent numbers. Everything is SIMULATED; twin timing is not FPGA latency.")

    def report(self, since_index=0, incident_id=None):
        recs = [r for r in self.b.audit_records(self.actor, 0) if r["index"] >= since_index]
        chain = self.b.verify_audit()
        facts = [{"index": r["index"], "actor": r["actor"], "action": r["action"], "payload": r["payload"]}
                 for r in recs if r["action"] in FACT_ACTIONS]
        incidents = [f["payload"] for f in facts if f["action"] == "incident_detected"]
        decisions = [f for f in facts if f["action"] in ("config_frame_sent", "proposal_denied")]
        res = self.converse({"facts": facts, "audit_chain": chain}, ())
        rid = f"IR{len(self.b.reports) + 1:04d}"
        report = {"id": rid, "label": "SIMULATED incident report",
                  "incident_id": incident_id or (incidents[0].get("id") if incidents else None),
                  "incidents": incidents, "audit_range": [since_index, recs[-1]["index"] if recs else since_index],
                  "refusals": [f for f in facts if f["action"] == "refused"],
                  "kills": [f for f in facts if f["action"] == "kill_frame_sent"],
                  "proposals": [f for f in facts if f["action"] in ("proposal_drafted", "proposal_ready")],
                  "decisions": decisions, "audit_chain": chain, "narrative": res["content"]}
        return self.b.add_report(self.actor, report)
