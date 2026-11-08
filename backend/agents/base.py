"""Shared agent loop: ask the LLM, execute its tool calls through the ToolBox (authorize() choke point),
feed the results back, stop when it answers without tool calls or after max_turns."""

import json

from ..redaction import redact

MAX_TOOL_RESULT_CHARS = 20_000


def dumps(x):
    return json.dumps(x, sort_keys=True, default=str)


class Agent:
    role = "agent"
    max_turns = 4
    system = ""

    def __init__(self, backend, llm, toolbox):
        self.b, self.llm, self.tools = backend, llm, toolbox
        self.actor = f"supervisor:{self.role}"

    def converse(self, payload, tool_names=(), context=None, role=None):
        msgs = [{"role": "system", "content": self.system},
                {"role": "user", "content": dumps(payload)}]
        schemas = self.tools.schemas(list(tool_names)) if tool_names else []
        calls, content = [], ""
        for _ in range(self.max_turns):
            resp = self.llm.chat(msgs, schemas, role=role or self.role)
            content = redact(resp.content or "")
            msgs.append({"role": "assistant", "content": content,
                         "tool_calls": [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in resp.tool_calls]})
            if not resp.tool_calls:
                break
            for t in resp.tool_calls:
                result = self.tools.call(t.name, t.arguments, self.actor, context)   # HardRuleError propagates
                calls.append({"name": t.name, "arguments": t.arguments, "result": result})
                text = dumps(result)
                if len(text) > MAX_TOOL_RESULT_CHARS:
                    text = text[:MAX_TOOL_RESULT_CHARS] + "...(truncated)"
                msgs.append({"role": "tool", "name": t.name, "tool_call_id": t.id, "content": text})
        return {"content": content, "tool_calls": calls}
