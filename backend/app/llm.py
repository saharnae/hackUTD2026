"""Nemotron client (OpenAI-compatible NVIDIA API) + an offline mock for demo/dev."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NEMOTRON_MODEL = os.getenv("NEMOTRON_MODEL", "nvidia/nemotron-3-super-120b-a12b")
ENABLE_THINKING = os.getenv("NEMOTRON_THINKING", "0") == "1"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMReply:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> dict:
        """The assistant message to append to history."""
        msg: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in self.tool_calls
            ]
        return msg


_THINK = re.compile(r"<think>.*?</think>", re.S)
_JSON_TOOL = re.compile(r"\{\s*\"tool\"\s*:\s*\"(\w+)\"\s*,\s*\"arguments\"\s*:\s*(\{.*?\})\s*\}", re.S)


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (handles ```json fences)."""
    text = _THINK.sub("", text or "")
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON in reply")
    return json.JSONDecoder().raw_decode(text[start:])[0]


class NemotronLLM:
    def __init__(self):
        from openai import AsyncOpenAI
        key = os.getenv("NVIDIA_API_KEY")
        self_hosted = "integrate.api.nvidia.com" not in NVIDIA_BASE_URL
        if not key and not self_hosted:
            raise RuntimeError("NVIDIA_API_KEY is not set (or run with AGENT_MOCK=1)")
        key = key or "not-needed"          # a self-hosted NIM (e.g. on Brev) ignores the key
        self.client = AsyncOpenAI(base_url=NVIDIA_BASE_URL, api_key=key)
        self.model = NEMOTRON_MODEL

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.3, max_tokens: int = 2048) -> LLMReply:
        kwargs = dict(model=self.model, messages=messages, temperature=temperature,
                      top_p=0.95, max_tokens=max_tokens,
                      extra_body={"chat_template_kwargs": {"enable_thinking": ENABLE_THINKING}})
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await self.client.chat.completions.create(**kwargs)
        m = resp.choices[0].message
        reply = LLMReply(content=_THINK.sub("", m.content or "").strip(),
                         reasoning=getattr(m, "reasoning_content", None) or "")
        for tc in m.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            reply.tool_calls.append(ToolCall(tc.id, tc.function.name, args))
        # Fallback: some deployments return the call as JSON text instead of tool_calls.
        if not reply.tool_calls and tools:
            jm = _JSON_TOOL.search(reply.content)
            if jm:
                reply.tool_calls.append(ToolCall("call_txt_0", jm.group(1), json.loads(jm.group(2))))
                reply.content = ""
        return reply


class MockLLM:
    """Scripted stand-in for Nemotron so the supervisor + UI run with no key or network."""
    model = "mock-nemotron"

    async def chat(self, messages, tools=None, temperature=0.3, max_tokens=2048) -> LLMReply:
        import asyncio
        await asyncio.sleep(float(os.getenv("MOCK_DELAY", "0.4")))
        sys = messages[0]["content"]
        results = {}                                   # tool name -> list of parsed results
        called = []
        for m in messages:
            if m.get("role") == "assistant":
                called += [tc["function"]["name"] + json.dumps(json.loads(tc["function"]["arguments"]), sort_keys=True)
                           for tc in m.get("tool_calls", [])]
            if m.get("role") == "tool":
                pass
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        calls = [tc for m in messages if m.get("role") == "assistant" for tc in m.get("tool_calls", [])]
        for tc, tm in zip(calls, tool_msgs):
            try:
                results.setdefault(tc["function"]["name"], []).append(json.loads(tm["content"]))
            except Exception:
                pass

        def call(name, args=None):
            key = name + json.dumps(args or {}, sort_keys=True)
            if key in called:
                return None
            return LLMReply(tool_calls=[ToolCall(f"mock_{len(called)}", name, args or {})])

        if "MONITOR" in sys:
            r = call("observe") or call("detect")
            if r:
                return r
            inc = (results.get("detect") or [[]])[-1]
            if not inc:
                return LLMReply(content="All clear: no incidents detected.")
            return LLMReply(content="; ".join(f"{i['kind']} (severity {i['severity']}): {i['detail']}" for i in inc))
        if "STRESS-TEST" in sys:
            r = call("simulate", {"action_id": "baseline"})
            if r:
                return r
            o = results["simulate"][-1]
            return LLMReply(content=f"Baseline (SIMULATED, in-sample seeds 0-79): mean max drawdown "
                                    f"{o['in_sample'].get('mean_max_drawdown')}, mean gate trips {o['in_sample'].get('mean_gate_trips')}. "
                                    f"Seeds 80-99 held out.")
        if "PROPOSAL" in sys:
            r = call("list_actions")
            if r:
                return r
            acts = results["list_actions"][-1]
            for a in acts:
                r = call("check_action", {"action_id": a["id"]})
                if r:
                    return r
            checks = results.get("check_action", [])
            passing = [a["id"] for a, c in zip(acts, checks) if c.get("pass")]
            for aid in passing:
                r = call("simulate", {"action_id": aid})
                if r:
                    return r
            task = next(m["content"] for m in messages if m["role"] == "user")
            if '"severity": 3' in task:
                r = call("request_kill", {"reason": "Severity-3 gate trip: stop quoting while a human reviews."})
                if r:
                    return r
            sims = {a: s for a, s in zip(passing, results.get("simulate", []))}
            fixes = [a for a in passing if a != "kill"]
            fixes.sort(key=lambda a: -sims[a]["in_sample"]["mean_max_drawdown"])
            return LLMReply(content=json.dumps({"recommended": fixes[0] if fixes else "kill", "ranking": fixes,
                "rationale": "Smallest simulated in-sample drawdown among fixes that passed the hard-rule check. "
                             "Seeds 80-99 are held out until the final evaluation; all results SIMULATED."}))
        if "POST-MORTEM" in sys:
            r = call("report_template") or call("audit_log", {"limit": 30})
            if r:
                return r
            log = results["audit_log"][-1]
            lines = "\n".join(f"- t={e['t']}s `{e['hash']}` **{e['event']}** {json.dumps(e['data'])}" for e in log[-8:])
            return LLMReply(content=f"## Summary\nIncident handled by the supervisor (mock run, SIMULATED).\n\n"
                                    f"## Timeline (from audit log)\n{lines}\n\n## Root cause (suspected)\n"
                                    f"See the incident detail; confirm against real 0x21 log frames.\n\n"
                                    f"## Action taken and who approved\nSee the decision entry above.\n\n"
                                    f"## Follow-ups\n- Run the one-time final evaluation on seeds 80-99 before any out-of-sample claim.")
        return LLMReply(content="ok")


def get_llm():
    return MockLLM() if os.getenv("AGENT_MOCK") == "1" else NemotronLLM()
