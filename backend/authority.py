"""One choke point: authorize(actor, action, payload). Every refusal is written to the audit log, then raised.

Actors:
  "supervisor" or "supervisor:<agent>"   any agent (orchestrator, monitor, stress, proposal, postmortem)
  "human:<name>"                         a named person (dashboard / API). The name must be non-empty.

Supervisor MAY: read status, read the 0x21 log, read the audit log, simulate / forecast / what_if, replay,
  propose a config (an 0x12 DRAFT only, allowlisted tunables in range), request a kill (0x15).
Supervisor may NOT: arm, clear a latch, change a hardware limit, accept / deny a proposal, send a config
  frame, or anything not listed (default deny).
Nobody, through software: arm, clear a latch, change a hardware limit (physical / compile-time only).
Humans: the reads, simulate, sim_step, run_supervisor, kill, accept_proposal, deny_proposal.

ASSUMPTIONS (AT):
  AT1. The HTTP API has no authentication; "human:<name>" is whatever the caller sends. TODO(FINAL):
       bind to localhost / add auth before anything outside the demo.
  AT2. Allowed actions are audited too (action name + small payload), so the post-mortem sees every
       tool call. Read actions are audited as well (cheap at demo scale). TODO(FINAL).
"""

from .errors import AuthorityError
from .packs.options import configmap as CM

READS = {"read_status", "read_log", "read_audit"}
SIMULATE = {"simulate", "forecast", "what_if", "replay", "plan_actions"}
SUPERVISOR_ALLOWED = READS | SIMULATE | {"propose_config", "request_kill"}
HUMAN_ALLOWED = READS | SIMULATE | {"sim_step", "twin_new_session", "run_supervisor", "kill", "request_kill",
                                    "accept_proposal", "deny_proposal"}
NEVER = {
    "arm": "arming is a physical action at the board (SW9 rising after the arming conditions hold); no software path",
    "clear_latch": "clearing a latch is a physical action (KEY0 with ARM low); no software path",
    "set_hardware_limit": "gate limits R01-R15 are compile-time constants; nothing in software can change them",
}
HUMAN_ONLY_REASON = {
    "accept_proposal": "accepting a proposal is a human decision",
    "deny_proposal": "denying a proposal is a human decision",
    "send_config_frame": "a config frame is produced only by a human accepting a proposal",
    "sim_step": "advancing / resetting the live twin from the API is a human action",
    "twin_new_session": "a twin power cycle stands in for the operator re-arming the board",
    "run_supervisor": "starting the supervisor loop is a human action",
    "kill": "the agent kill path is request_kill",
}


def parse_actor(actor):
    if not isinstance(actor, str):
        return None, None
    if actor == "supervisor" or actor.startswith("supervisor:"):
        return "supervisor", actor.partition(":")[2] or None
    if actor.startswith("human:") and actor[6:].strip():
        return "human", actor[6:].strip()
    return None, None


class Authority:
    def __init__(self, audit):
        self.audit = audit

    def refuse(self, actor, action, reason, payload=None, kind="refused"):
        rec = self.audit.append(actor, "refused", {"requested": action, "reason": reason, "kind": kind,
                                                   "payload": _small(payload)})
        raise AuthorityError(reason, {"requested": action, "audit_index": rec["index"], "kind": kind})

    def authorize(self, actor, action, payload=None):
        role, _ = parse_actor(actor)
        if role is None:
            self.refuse(actor, action, f"unknown actor {actor!r} (use 'supervisor:<agent>' or 'human:<name>')",
                        payload, "unknown_actor")
        if action in NEVER:
            self.refuse(actor, action, NEVER[action], payload, "never_allowed")
        allowed = SUPERVISOR_ALLOWED if role == "supervisor" else HUMAN_ALLOWED
        if action not in allowed:
            reason = HUMAN_ONLY_REASON.get(action) if role == "supervisor" else None
            self.refuse(actor, action, reason or f"action {action!r} is not permitted for {role}", payload,
                        "not_permitted")
        if action == "propose_config":
            params = (payload or {}).get("params")
            current = (payload or {}).get("current")
            probs = CM.check_change(params)
            if not probs and current:
                probs = CM.merged_problems(current, params)                  # cross-field rules
            if probs:
                kinds = sorted({p["kind"] for p in probs})
                self.refuse(actor, action, "; ".join(p["reason"] for p in probs),
                            {"params": params}, "hardware_limit" if "hardware_limit" in kinds else kinds[0])
        self.audit.append(actor, action, _small(payload))
        return True


def _small(payload, limit=2000):
    """Audit payloads stay small: big values are summarized, never dropped silently."""
    import json
    if payload is None:
        return {}
    try:
        s = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return {"unserializable": type(payload).__name__}
    if len(s) <= limit:
        return json.loads(s)
    return {"truncated": True, "chars": len(s), "head": s[:limit]}
