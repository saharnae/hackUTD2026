"""Deterministic scripted responses for the FakeLLM (tests and the offline demo).

Each function reads the JSON payload the agent sent (messages[1]) and returns an LLMResponse. They make
no decisions an LLM could not make from the same payload; they never invent numbers (every number they
quote is copied from the payload)."""

import json

from .llm import FakeLLM, LLMResponse, ToolCall


def _payload(messages):
    return json.loads(messages[1]["content"])


def _tool_results(messages):
    return [m for m in messages if m["role"] == "tool"]


def monitor_triage(messages, tools):
    p = _payload(messages)
    if _tool_results(messages):
        return LLMResponse("Kill requested; waiting for the gate to report the latch.")
    latched = [i for i in p["incidents"] if i["latched"]]
    if latched:
        i = latched[0]
        return LLMResponse(f"Gate latched {i['rule']} ({i['rule_name']}); trading is already stopped. No kill needed.")
    blocking = [i for i in p["incidents"] if i["detail"].get("level") in ("high", "trip")]
    if blocking:
        i = blocking[0]
        return LLMResponse("", [ToolCall("request_kill", {"reason": f"{i['detail']['channel']} at its {i['rule']} limit "
                                                                     f"({i['detail']['value']})"})])
    return LLMResponse("Risk is elevated but below the gate limits; no kill.")


def proposal_pick(messages, tools):
    p = _payload(messages)
    if _tool_results(messages):
        return LLMResponse("Drafted one proposal for human review.")
    cands = p["candidates"]
    if not cands:
        return LLMResponse("No candidate passed the held-out replay; no proposal.")
    better = [c for c in cands if c["improves"]]
    if not better:
        return LLMResponse(f"No candidate beats the current config on the held-out replay (baseline score "
                           f"{p['baseline']['score']:.4f}, best candidate {cands[0]['score']:.4f}); no proposal.")
    c = better[0]
    why = (f"{c['source']}; held-out score {c['score']:.4f} vs baseline {p['baseline']['score']:.4f} "
           f"(SIMULATED, held-out seeds)")
    return LLMResponse("", [ToolCall("propose_config", {"params": c["params"], "rationale": why})])


def proposal_write(messages, tools):
    p = _payload(messages)
    c = p["comparison"]
    return LLMResponse(f"Proposal {p['proposal']['id']}: change {sorted(p['proposal']['changes'])}. Held-out seeds "
                       f"{c['seeds'][0]}-{c['seeds'][1]}, regimes {c['regimes']}: score {c['baseline_score']:.4f} -> "
                       f"{c['proposal_score']:.4f}, unexpected trips {c['unexpected_trips']}. SIMULATED; a human decides.")


def stress_notes(messages, tools):
    p = _payload(messages)
    return LLMResponse(f"Compared {len(p['comparisons'])} proposal(s) on held-out seeds; see the numbers.")


def postmortem_narrative(messages, tools):
    p = _payload(messages)
    acts = [f["action"] for f in p["facts"]]
    return LLMResponse(f"{len(p['facts'])} audit facts: {acts.count('incident_detected')} incident(s), "
                       f"{acts.count('kill_frame_sent')} kill frame(s), {acts.count('proposal_ready')} proposal(s) "
                       f"for review, {acts.count('config_frame_sent')} config frame(s) sent, "
                       f"{acts.count('proposal_denied')} denial(s), {acts.count('refused')} refusal(s). "
                       f"Audit chain ok: {p['audit_chain']['ok']}.")


ROLE_POLICY = {"monitor": monitor_triage, "proposal": proposal_pick, "proposal_write": proposal_write,
               "stress": stress_notes, "postmortem": postmortem_narrative}


def demo_fake_llm(script=None):
    """FakeLLM answering every role with ROLE_POLICY; script entries (per role) are used first."""
    return FakeLLM(script, fallback=ROLE_POLICY)
