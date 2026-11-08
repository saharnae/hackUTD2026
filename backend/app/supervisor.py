"""Nemotron supervisor: orchestrator + 4 subagents (monitor, stress-test, proposal, post-mortem).

The core knows nothing about trading or disasters. It only calls the adapter's methods,
exposed to Nemotron as tools. Nothing non-risk-reducing is applied without a human click.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Awaitable, Callable

from .adapters import get_adapter
from .interface import IN_SAMPLE, OUT_OF_SAMPLE, to_json
from .llm import extract_json, get_llm

Emit = Callable[[str, dict], Awaitable[None]]
PROPOSALS: dict[str, dict] = {}
# Seeds 80-99 are evaluated ONCE, in a final evaluation, never during proposals.
PROPOSAL_SEEDS = IN_SAMPLE


# ---------------- tools (thin wrappers over the adapter) ----------------
def _fn(name, desc, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(required)}}}


AID = {"action_id": {"type": "string", "description": "id from list_actions"}}
SCHEMAS = {
    "observe": _fn("observe", "Read the live system state (status + log frames)."),
    "detect": _fn("detect", "Turn the current state into incidents, most severe first."),
    "simulate": _fn("simulate", "Run the current state forward on in-sample seeds 0-79, with an action or "
                    "with none (baseline). Out-of-sample seeds 80-99 are held out and are NOT evaluated here.",
                    {"action_id": {"type": "string", "description": "omit or 'baseline' for no action"}}),
    "list_actions": _fn("list_actions", "Candidate fixes for the current incident."),
    "check_action": _fn("check_action", "Hard-rule gate for one action. Final; cannot be bypassed.", AID, ["action_id"]),
    "request_kill": _fn("request_kill", "Request a kill. Only reduces risk, so it runs without a human. "
                        "Use for severity-3 incidents.", {"reason": {"type": "string"}}, ["reason"]),
    "audit_log": _fn("audit_log", "Hash-chained audit log, newest last.", {"limit": {"type": "integer"}}),
    "report_template": _fn("report_template", "Section layout for proposals and post-mortems."),
}


class Ctx:
    def __init__(self, adapter):
        self.a = adapter
        self.state: dict = {}
        self.incidents = []
        self.incident = None
        self.actions: dict = {}
        self.checks: dict = {}
        self.outcomes: dict = {}
        self.auto_actions: list = []

    def run(self, name: str, args: dict):
        a = self.a
        if name == "observe":
            self.state = a.observe()
            return self.state
        if name == "detect":
            self.incidents = a.detect(self.state or a.observe())
            return to_json(self.incidents)
        if name == "simulate":
            aid = args.get("action_id") or "baseline"
            act = None if aid == "baseline" else self.actions.get(aid)
            if aid != "baseline" and act is None:
                return {"error": f"unknown action_id {aid}; call list_actions first"}
            out = to_json(a.simulate(self.state, act, PROPOSAL_SEEDS))
            self.outcomes[aid] = out
            return out
        if name == "list_actions":
            acts = a.actions(self.incident)
            self.actions = {x.id: x for x in acts}
            return to_json(acts)
        if name == "check_action":
            act = self.actions.get(args.get("action_id"))
            if not act:
                return {"error": "unknown action_id"}
            ok, why = a.check(act)
            self.checks[act.id] = {"pass": ok, "reason": why}
            return self.checks[act.id]
        if name == "request_kill":
            kill = next((x for x in a.actions(self.incident) if x.reduces_risk), None)
            if not kill:
                return {"error": "no risk-reducing kill action in this scenario"}
            res = to_json(a.apply(kill, None))   # allowed without a human (contract rule 4)
            self.auto_actions.append({"action": to_json(kill), "result": res, "reason": args.get("reason")})
            return res
        if name == "audit_log":
            return a.audit_log(int(args.get("limit", 30)))
        if name == "report_template":
            return a.report_template()
        return {"error": f"unknown tool {name}"}


# ---------------- subagent runner ----------------
MONITOR = """You are the MONITOR subagent of a system supervisor. Call observe, then detect.
Then reply with 1-2 sentences naming each incident and its key numbers. Never invent numbers."""
STRESS = """You are the STRESS-TEST subagent. Call simulate with no action to get the baseline forecast
for the current incident on in-sample seeds. Reply with 1-2 sentences, labelled SIMULATED. Do not report
out-of-sample results: seeds 80-99 are held out until the one-time final evaluation."""
PROPOSAL = """You are the PROPOSAL subagent. Call list_actions, then check_action on EVERY action,
then simulate every action that passed. If the incident is severity 3, also call request_kill.
request_kill is an emergency stop, NOT a fix: never put "kill" or any safer-only action in "recommended"
or "ranking". Recommend the best config change (frame 0x12) that passed its check, ranked by in-sample
drawdown and gate trips. Never recommend a failed check.
Finish with ONLY JSON: {"recommended":"<action_id>","ranking":["<id>",...],"rationale":"<2 sentences>"}"""
POSTMORTEM = """You are the POST-MORTEM subagent. Call report_template and audit_log, then write a
short markdown incident report using the post_mortem sections as ## headings. Cite audit hashes.
Label results SIMULATED. Never invent events not in the log."""

TOOLSETS = {"monitor": ["observe", "detect"], "stress_test": ["simulate"],
            "proposal": ["list_actions", "check_action", "simulate", "request_kill"],
            "post_mortem": ["report_template", "audit_log"]}
PROMPTS = {"monitor": MONITOR, "stress_test": STRESS, "proposal": PROPOSAL, "post_mortem": POSTMORTEM}


async def run_subagent(llm, agent: str, task: str, ctx: Ctx, emit: Emit, max_rounds: int = 24) -> str:
    await emit("agent_start", {"agent": agent, "task": task})
    msgs = [{"role": "system", "content": PROMPTS[agent]}, {"role": "user", "content": task}]
    tools = [SCHEMAS[t] for t in TOOLSETS[agent]]
    text = ""
    for _ in range(max_rounds):
        reply = await llm.chat(msgs, tools=tools, temperature=0.2)
        msgs.append(reply.as_message())
        if not reply.tool_calls:
            text = reply.content
            break
        for tc in reply.tool_calls:
            uid = f"{agent}-{uuid.uuid4().hex[:8]}"          # unique across subagents for the UI
            await emit("tool_call", {"agent": agent, "id": uid, "name": tc.name, "args": tc.arguments})
            t = time.time()
            if tc.name not in TOOLSETS[agent]:
                result = {"error": f"{agent} may not call {tc.name}"}
            else:
                try:
                    # adapter calls can take ~10 s (simulate); keep the event stream flowing
                    result = await asyncio.to_thread(ctx.run, tc.name, tc.arguments or {})
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
            await emit("tool_result", {"agent": agent, "id": uid, "name": tc.name,
                                       "ms": round((time.time() - t) * 1000), "result": result})
            msgs.append({"role": "tool", "tool_call_id": tc.id,
                         "content": json.dumps(result, default=str)[:4000]})
    await emit("agent_done", {"agent": agent, "summary": text})
    return text


async def _ensure(ctx: Ctx, emit: Emit, name: str, args: dict):
    """Orchestrator fills any gap the agent left, as a visible tool call."""
    cid = f"orc-{uuid.uuid4().hex[:6]}"
    await emit("tool_call", {"agent": "orchestrator", "id": cid, "name": name, "args": args})
    res = await asyncio.to_thread(ctx.run, name, args)
    await emit("tool_result", {"agent": "orchestrator", "id": cid, "name": name, "ms": 0, "result": res})


# ---------------- orchestrator ----------------
async def run_supervisor(emit: Emit) -> dict:
    llm, adapter = get_llm(), get_adapter()
    ctx = Ctx(adapter)
    await emit("status", {"stage": "monitor", "model": llm.model, "adapter": adapter.name,
                          "invariants": adapter.invariants()})

    # 1-2 Observe + detect
    await run_subagent(llm, "monitor", "Check the system now.", ctx, emit)
    if not ctx.state:
        await _ensure(ctx, emit, "observe", {})
    if not ctx.incidents:
        ctx.incidents = adapter.detect(ctx.state)
    if not ctx.incidents:
        res = {"healthy": True, "state": ctx.state}
        await emit("final", res)
        return res
    ctx.incident = ctx.incidents[0]
    await emit("incident", {"incident": to_json(ctx.incident), "all": to_json(ctx.incidents)})
    inc_txt = json.dumps(to_json(ctx.incident))

    # 3 Forecast
    await emit("status", {"stage": "stress_test"})
    await run_subagent(llm, "stress_test", f"Incident: {inc_txt}. Forecast the baseline.", ctx, emit)
    if "baseline" not in ctx.outcomes:
        await _ensure(ctx, emit, "simulate", {"action_id": "baseline"})

    # 4-5 Propose + compare
    await emit("status", {"stage": "proposal"})
    text = await run_subagent(llm, "proposal", f"Incident: {inc_txt}. Propose and test fixes.", ctx, emit)
    if not ctx.actions:
        await _ensure(ctx, emit, "list_actions", {})
    for aid in ctx.actions:
        if aid not in ctx.checks:
            await _ensure(ctx, emit, "check_action", {"action_id": aid})
        if ctx.checks[aid]["pass"] and aid not in ctx.outcomes:
            await _ensure(ctx, emit, "simulate", {"action_id": aid})
    try:
        rec = extract_json(text)
    except Exception:
        rec = {}
    passing = [a for a in ctx.actions if ctx.checks[a]["pass"]]
    fixes = [a for a in passing if not ctx.actions[a].reduces_risk]     # human decides config changes
    if rec.get("recommended") not in (fixes or passing):   # check is final (contract rule 3)
        def dd(a):
            o = ctx.outcomes.get(a) or {}
            return -(o.get("in_sample", {}).get("mean_max_drawdown") or -1e18)
        best = sorted(fixes, key=dd)[0] if fixes else (passing[0] if passing else None)
        rec["recommended"] = best
        rec["rationale"] = (f"Orchestrator: {best} has the smallest simulated in-sample drawdown among config "
                            f"changes that passed the hard-rule check. " + (rec.get("rationale") or "")).strip()
    rec["ranking"] = [a for a in rec.get("ranking", []) if a in fixes] or fixes

    # 6 Write the proposal
    pid = uuid.uuid4().hex[:8]
    proposal = {
        "id": pid, "adapter": adapter.name, "incident": to_json(ctx.incident),
        "state": ctx.state, "baseline": ctx.outcomes.get("baseline"),
        "candidates": [{"action": to_json(ctx.actions[a]), "check": ctx.checks[a],
                        "outcome": ctx.outcomes.get(a)} for a in ctx.actions],
        "recommended": rec.get("recommended"), "ranking": rec.get("ranking", []),
        "rationale": rec.get("rationale", ""), "auto_actions": ctx.auto_actions,
        "invariants": adapter.invariants(), "label": "SIMULATED",
    }
    PROPOSALS[pid] = {"proposal": proposal, "ctx": ctx}
    await emit("proposal", proposal)
    await emit("status", {"stage": "awaiting_human"})
    return proposal


# ---------------- 7-8 human decision + post-mortem ----------------
async def decide(pid: str, accept: bool, action_id: str | None, by: str, emit: Emit) -> dict:
    rec = PROPOSALS.get(pid)
    if not rec:
        await emit("error", {"message": "unknown proposal"})
        return {}
    ctx: Ctx = rec["ctx"]
    aid = action_id or rec["proposal"]["recommended"]
    already = {x["action"]["id"] for x in ctx.auto_actions if x["result"].get("ok")}
    if accept and aid in already:
        result = {"ok": True, "reason": f"{aid} was already applied by the supervisor; nothing re-sent."}
    elif accept and aid in ctx.actions:
        result = to_json(ctx.a.apply(ctx.actions[aid], by))
    else:
        result = {"ok": True, "reason": f"Denied by {by}; nothing changed."}
    await emit("decision", {"accepted": accept, "action_id": aid, "by": by, "result": result})

    await emit("status", {"stage": "post_mortem"})
    llm = get_llm()
    report = await run_subagent(llm, "post_mortem",
                                f"Write the post-mortem for incident {json.dumps(rec['proposal']['incident'])}. "
                                f"Human decision: {'accepted' if accept else 'denied'} {aid} by {by}.", ctx, emit)
    out = {"report": report, "audit_tail": ctx.a.audit_log(8)}
    await emit("final", out)
    return out
