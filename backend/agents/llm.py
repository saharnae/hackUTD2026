"""LLM seam. Agents only see LLMClient.chat(messages, tools, role) -> LLMResponse.

FakeLLM: deterministic scripted responses (tests, and the offline demo default).
NemotronClient: the real client. It reads NEMOTRON_BASE_URL, NEMOTRON_MODEL and NVIDIA_API_KEY from the
  environment when called. The endpoint / request format is owned by the CS teammate and is NOT guessed
  here: pass a `transport` callable, otherwise chat() raises NotImplementedError. TODO(FINAL).
  Its values are never printed or logged; every error message is redacted.

Message format (internal, provider-neutral): {"role": "system"|"user"|"assistant"|"tool", "content": str,
  "tool_calls": [...] (assistant), "name"/"tool_call_id" (tool)}. Tools are JSON-schema function
  definitions (backend/agents/tools.py). Mapping these to the wire format is the transport's job.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..redaction import SECRET_ENV_VARS, redact


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]
    id: str = ""


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)


class LLMClient:
    def chat(self, messages: List[dict], tools: List[dict], role: Optional[str] = None) -> LLMResponse:
        raise NotImplementedError


class FakeLLM(LLMClient):
    """script: {role: [LLMResponse | callable(messages, tools) -> LLMResponse, ...]}.
    Each call to chat(role=r) pops the next entry for r. When a role's queue is empty, fallback[r]
    (a callable, used on every call) answers; with no fallback the reply is an empty response (no tool
    calls), so an agent always terminates. calls records (role, messages)."""

    def __init__(self, script=None, fallback=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.fallback = dict(fallback or {})
        self.calls = []
        self._n = 0

    def chat(self, messages, tools, role=None):
        self.calls.append((role, json.loads(json.dumps(messages, default=str))))
        q = self.script.get(role) or []
        if q:
            item = q.pop(0)
        elif role in self.fallback:
            item = self.fallback[role]
        else:
            return LLMResponse("")
        resp = item(messages, tools) if callable(item) else item
        for tc in resp.tool_calls:                                   # deterministic ids
            if not tc.id:
                self._n += 1
                tc.id = f"call_{self._n:04d}"
        return resp


class LLMConfigError(RuntimeError):
    pass


class NemotronClient(LLMClient):
    """Minimal real adapter. TODO(FINAL): the CS teammate supplies `transport` (and the endpoint format)."""

    def __init__(self, transport: Optional[Callable[..., dict]] = None):
        self.transport = transport

    @staticmethod
    def configured():
        return all(os.environ.get(k) for k in SECRET_ENV_VARS)

    def __repr__(self):
        return f"NemotronClient(configured={self.configured()}, transport={'set' if self.transport else 'unset'})"

    def chat(self, messages, tools, role=None):
        missing = [k for k in SECRET_ENV_VARS if not os.environ.get(k)]
        if missing:
            raise LLMConfigError(f"Nemotron is not configured: missing environment variable(s) {missing}")
        if self.transport is None:
            raise NotImplementedError("TODO(FINAL): Nemotron transport not wired; the endpoint format is owned by "
                                      "the CS teammate (pass transport=...)")
        try:
            raw = self.transport(base_url=os.environ["NEMOTRON_BASE_URL"], model=os.environ["NEMOTRON_MODEL"],
                                 api_key=os.environ["NVIDIA_API_KEY"], messages=messages, tools=tools)
        except Exception as e:                                        # redact anything the transport echoes
            raise RuntimeError(redact(f"Nemotron call failed: {type(e).__name__}: {e}")) from None
        return LLMResponse(content=redact(raw.get("content") or ""),
                           tool_calls=[ToolCall(t["name"], t.get("arguments") or {}, t.get("id", ""))
                                       for t in raw.get("tool_calls") or []])


def make_llm(kind=None):
    from .. import config as C
    kind = kind or C.LLM_BACKEND
    if kind == "nemotron":
        return NemotronClient()
    from .policy import demo_fake_llm
    return demo_fake_llm()
