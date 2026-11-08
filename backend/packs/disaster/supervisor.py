"""DisasterSupervisor (SIMULATED): runs the disaster pack and writes every decision to P3's
hash-chained audit log (backend.audit.AuditLog) - care rule C7.

Human decisions (accept / deny a proposal, kill) are also authorized through P3's
backend.authority.Authority, so an agent trying to accept its own proposal is refused and audited
there before the pack sees it.

Audit records:
  refused care-rule decisions -> action "refused", payload {"requested", "reason", "kind": "care_rule",
                                 "rule", ...} (same shape as Authority.refuse)
  everything else             -> action "disaster.<action>", payload = the event
"""

from ...authority import Authority, parse_actor
from ...errors import AuthorityError
from . import agents as A
from . import world as W
from .pack import DisasterPack

AUTH_ACTION = {"accept_proposal": "accept_proposal", "deny_proposal": "deny_proposal"}


class DisasterSupervisor:
    def __init__(self, pack, audit):
        self.pack, self.audit, self.authority = pack, audit, Authority(audit)

    def _authorized(self, cmd):
        kind, actor = cmd.get("kind"), cmd.get("actor", "")
        role, _ = parse_actor(actor)
        try:
            if kind in AUTH_ACTION:
                self.authority.authorize(actor, AUTH_ACTION[kind], {"pack": "disaster", "id": cmd.get("id")})
            elif kind == "kill":
                self.authority.authorize(actor, "kill" if role == "human" else "request_kill", {"pack": "disaster"})
        except AuthorityError:
            return False                                  # refused and audited by P3's Authority
        return True

    def step(self, state, dt_s, commands=()):
        cmds = [c for c in (commands or []) if self._authorized(c)]
        new = self.pack.step(state, dt_s, cmds)
        for ev in new["events"]:
            if ev["result"] == "refused":
                self.audit.append(ev["actor"], "refused", {"requested": ev["action"], "reason": ev["reason"],
                                                           "kind": "care_rule", "rule": ev["rule"],
                                                           "pack": "disaster", "t_s": ev["t_s"],
                                                           "proposal_id": ev.get("proposal_id")})
            else:
                self.audit.append(ev["actor"], f"disaster.{ev['action']}", dict(ev, pack="disaster"))
        return new


def run(seed, policy, audit, dt_s=300, horizon_s=None, commander="commander"):
    """One full simulated response. Supervised: triage scores every step, auditor rounds at 2 h / 4 h,
    a scripted commander accepts pending proposals. Baseline: FCFS, no agent."""
    pack = DisasterPack(policy)
    sup = DisasterSupervisor(pack, audit)
    reg = W.registry(seed)
    st = sup.step(pack.initial_state(seed), 0, [{"kind": "shock", "actor": f"human:{commander}"}])
    tri, aud, cmd = A.TriageAgent(), A.OverlookedAuditor(), A.Commander(commander)
    horizon = horizon_s or st["horizon_s"]
    rounds = list(A.AUDIT_ROUNDS_H)
    while st["t_s"] - st["storm_t"] < horizon:
        cmds = []
        if policy == "supervised":
            cmds.append({"kind": "agent_heartbeat", "actor": tri.actor})
            el_h = (st["t_s"] - st["storm_t"]) / 3600
            if rounds and el_h >= rounds[0]:
                rounds.pop(0)
                p = aud.propose(st, reg)
                if p:
                    cmds.append(p)
            cmds.append(tri.propose(st, reg))
        st = sup.step(st, 0, cmds)
        st = sup.step(st, dt_s, cmd.decide(st) if policy == "supervised" else [])
    return st
