"""Scripted disaster demo (SIMULATED). Same supervisor pattern as the options arena: agents propose,
care rules decide, humans hold authority, every decision lands in P3's hash-chained audit log.

    python -m backend.packs.disaster.demo [--seed 0] [--audit-log PATH]

Script:
  1. The commander presses the shock button: the storm clock starts.
  2. The agents run (heartbeats, triage scores, an overlooked-auditor welfare-check list at 2 h).
  3. An injected AI proposal tries to downgrade a household with a known life-safety need -> the care
     rules REFUSE it (C1) and the refusal is in the audit log. The agent also tries to accept its own
     proposal -> P3's Authority refuses it.
  4. Kill: the agent link is cut -> the rule queue (FCFS + C1-C4) takes over (C5); new agent
     proposals are refused.
  5. The commander accepts the auditor's pending, still-valid welfare-check list.
  6. Metrics after 24 h: first-come-first-served baseline vs supervised (full runs) and this demo run.
All numbers are simulated results on synthetic data; nothing here is a claim about a real response.
"""

import argparse
import os
import sys

from ... import config as C
from ...audit import AuditLog
from . import agents as A
from . import supervisor as S
from . import world as W
from .pack import DisasterPack

COMMANDER = "human:commander"
KEYS = ("first_contacts", "top10_reached", "mean_ttfc_top10_min", "p90_ttfc_top10_min", "overlooked_high_need",
        "overlooked_silent", "silent_reached", "crew_utilization")


def _until(sup, st, t_h, agents=True, dt=300):
    reg = W.registry(st["seed"])
    tri = A.TriageAgent()
    while st["t_s"] - st["storm_t"] < t_h * 3600:
        cmds = [{"kind": "agent_heartbeat", "actor": tri.actor}, tri.propose(st, reg)] if agents else []
        st = sup.step(st, 0, cmds)
        st = sup.step(st, dt, [])
    return st


def run_demo(seed, audit, compare=True):
    pack = DisasterPack("supervised")
    sup = S.DisasterSupervisor(pack, audit)
    reg = W.registry(seed)
    out = {"label": "SIMULATED", "seed": seed}

    st = sup.step(pack.initial_state(seed), 0, [{"kind": "shock", "actor": COMMANDER}])           # 1
    out["shock"] = {"storm_t": st["storm_t"], "event": st["events"][0]}
    st = _until(sup, st, 2.0)                                                                      # 2
    aud = A.OverlookedAuditor()
    st = sup.step(st, 0, [{"kind": "agent_heartbeat", "actor": A.TriageAgent.actor}, aud.propose(st, reg)])
    welfare = next(p for p in st["proposals"] if p["type"] == "welfare_checks" and p["status"] == "pending")
    out["auditor"] = {"proposal_id": welfare["id"], "households": len(welfare["payload"]["households"]),
                      "flagged_zones": [z["zone"] for z in welfare["payload"]["evidence"]]}

    target = next(i for i in st["hh"]["queue"] if st["hh"]["stated"][i] == 3)                      # 3
    n_audit = audit.verify_chain()["count"]
    st = sup.step(st, 0, [{"kind": "propose", "actor": "supervisor:triage",
                           "proposal": {"type": "set_priority", "household": target, "priority": "routine"}}])
    refusal = next(e for e in st["events"] if e["action"] == "propose:set_priority")
    rec = [r for r in audit.read_all()[n_audit:] if r["action"] == "refused"]
    out["downgrade"] = {"household": target, "event": refusal, "audit_record": rec[0] if rec else None,
                        "still_band_0": pack._band(st, target, st["t_s"]) == 0}
    n_audit = audit.verify_chain()["count"]
    st = sup.step(st, 0, [{"kind": "accept_proposal", "actor": "supervisor:triage", "id": welfare["id"]}])
    out["agent_self_accept"] = {"refused": welfare["id"] in [p["id"] for p in st["proposals"]
                                                              if p["status"] == "pending"],
                                "audit": [r for r in audit.read_all()[n_audit:] if r["action"] == "refused"]}

    st = sup.step(st, 0, [{"kind": "kill", "actor": COMMANDER}])                                   # 4
    st = sup.step(st, 300, [])
    after = sup.step(st, 0, [{"kind": "propose", "actor": "supervisor:triage",
                              "proposal": {"type": "scores", "scores": {"0": 1.0}}}])
    out["kill"] = {"mode": st["mode"], "fallback_events": [e for e in st["events"] if e["action"] == "fallback"],
                   "agent_proposal_after_kill": next(e for e in after["events"])}
    st = after

    st = sup.step(st, 0, [{"kind": "accept_proposal", "actor": COMMANDER, "id": welfare["id"]}])    # 5
    out["commander_accept"] = next(e for e in st["events"] if e["action"] == "accept_proposal")
    st = _until(sup, st, 24.0, agents=False)
    out["demo_run"] = {k: st["summary"][k] for k in KEYS}
    out["mode_at_end"] = st["mode"]

    if compare:                                                                                    # 6
        for pol in ("baseline", "supervised"):
            s2 = S.run(seed, pol, audit)
            out[pol] = {k: s2["summary"][k] for k in KEYS}
    out["audit"] = audit.verify_chain()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="SIMULATED disaster-response demo")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--audit-log", default=C.AUDIT_LOG_PATH, help="defaults to P3's audit log (backend/config.py)")
    a = ap.parse_args(argv)
    audit = AuditLog(a.audit_log)
    r = run_demo(a.seed, audit)
    p = lambda s: print(f"[SIMULATED] {s}")  # noqa: E731
    p(f"disaster pack demo, seed {a.seed}: {W.N_HOUSEHOLDS} synthetic households, {W.N_ZONES} zones, "
      f"{W.N_CREWS} crews")
    p(f"1 shock button: storm clock started at t = {r['shock']['storm_t']} s")
    p(f"2 overlooked auditor: proposal #{r['auditor']['proposal_id']}, {r['auditor']['households']} welfare checks, "
      f"flagged zones {r['auditor']['flagged_zones']} (pending a human)")
    d = r["downgrade"]
    p(f"3 AI downgrade of household {d['household']} (known life-safety need): {d['event']['result'].upper()} "
      f"by {d['event']['rule']}: {d['event']['reason']}; audit record #{d['audit_record']['index']}; "
      f"still in band 0: {d['still_band_0']}")
    p(f"  agent tried to accept its own proposal: refused = {r['agent_self_accept']['refused']}")
    k = r["kill"]
    p(f"4 kill: mode = {k['mode']}; agent proposal after kill: {k['agent_proposal_after_kill']['result']} "
      f"({k['agent_proposal_after_kill']['rule']})")
    c = r["commander_accept"]
    p(f"5 commander accepted proposal #{c['proposal_id']}: {c['result']}")
    p("6 metrics after 24 h (simulated):")
    p(f"  {'metric':22s} {'baseline FCFS':>14s} {'supervised':>11s} {'this demo':>10s}")
    for key in KEYS:
        p(f"  {key:22s} {r['baseline'][key]:>14} {r['supervised'][key]:>11} {r['demo_run'][key]:>10}")
    p(f"audit chain: ok = {r['audit']['ok']}, {r['audit']['count']} records, head {r['audit']['head_hash'][:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
