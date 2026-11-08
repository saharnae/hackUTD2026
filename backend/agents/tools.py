"""Nemotron tool schemas (JSON-schema function definitions) and their executor.

Tool names are TODO(FINAL) (backend/config.py TOOL_NAMES). Every tool calls the real backend: the twin
(or the serial source) through the pack and the domain-free engine. No tool returns canned data.
Every call goes through Backend -> authorize(actor, action); refusals are audited and raised.
"""

from .. import config as C
from ..packs.options import configmap as CM
from ..packs.options import replay as RP

REGIMES = list(RP.EV.REGIMES)
HELD_LO, HELD_HI = C.HELD_OUT_SEEDS


def _tunable_props():
    out = {}
    for name, t in CM.TUNABLE.items():
        out[name] = {"type": t["type"], "minimum": t["lo"], "maximum": t["hi"],
                     "description": f"0x12 field {t['field']} (scale {t['scale']}), default {t['default']}"}
    return out


_SEEDS = {"type": "object", "description": f"Inclusive seed range. Agents may use 0-{C.AGENT_SEED_LIMIT - 1}; "
                                           f"seeds >= {C.AGENT_SEED_LIMIT} are always refused.",
          "properties": {"lo": {"type": "integer", "minimum": 0, "maximum": C.AGENT_SEED_LIMIT - 1},
                         "hi": {"type": "integer", "minimum": 0, "maximum": C.AGENT_SEED_LIMIT - 1}},
          "required": ["lo", "hi"], "additionalProperties": False}
_REGIMES = {"type": "array", "items": {"type": "string", "enum": REGIMES}, "minItems": 1, "uniqueItems": True}
_PARAMS = {"type": "object", "properties": _tunable_props(), "additionalProperties": False, "minProperties": 1,
           "description": "Changes to soft strategy parameters only (the 0x12 allowlist). Gate limits cannot be "
                          "changed."}


def _fn(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": list(required),
                                                            "additionalProperties": False}}}


N = dict(zip(("get_status", "get_log", "what_if", "replay", "propose_config", "request_kill"), C.TOOL_NAMES))

TOOL_SCHEMAS = [
    _fn(N["get_status"], "Read the latest decoded 0x20 status frame plus limit channels (SIMULATED twin by default).",
        {}),
    _fn(N["get_log"], "Read recent decoded 0x21 order/fill log records.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}),
    _fn(N["what_if"], f"Simulate a change from the current twin state on held-out seeds {HELD_LO}-{HELD_HI}. "
                      "kind=jump runs the golden reprice shock table and the real evaluator (single_jump regime); "
                      "kind=config replays current vs changed parameters; kind=kill forecasts with a kill.",
        {"change": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["jump", "config", "kill"]},
            "cents": {"type": "integer", "minimum": -1023, "maximum": 1023, "description": "jump size (kind=jump)"},
            "params": _PARAMS,
            "regimes": _REGIMES}, "required": ["kind"], "additionalProperties": False}},
        ["change"]),
    _fn(N["replay"], "Replay the golden engine through the real evaluator for a seed range and regimes, with "
                     "the current parameters or allowlisted changes.",
        {"params": _PARAMS, "seeds": _SEEDS, "regimes": _REGIMES}, ["seeds"]),
    _fn(N["propose_config"], "Draft an 0x12 config change for a human to accept or deny. Does NOT send a frame.",
        {"params": _PARAMS, "rationale": {"type": "string", "maxLength": 2000}}, ["params", "rationale"]),
    _fn(N["request_kill"], "Send an 0x15 KILL frame now. Only reduces risk; re-arming needs a human at the board.",
        {"reason": {"type": "string", "maxLength": 500}}, ["reason"]),
]
TOOL_BY_NAME = {t["function"]["name"]: t for t in TOOL_SCHEMAS}


class ToolBox:
    def __init__(self, backend):
        self.b = backend
        self.drafts = []                                  # proposals drafted through this toolbox

    def schemas(self, names=None):
        return [t for t in TOOL_SCHEMAS if names is None or t["function"]["name"] in names]

    def call(self, name, args, actor, context=None):
        """Execute one tool call. Unknown names are refused by authorize() (default deny) and audited."""
        args = dict(args or {})
        if name == N["get_status"]:
            return self.b.status(actor)
        if name == N["get_log"]:
            return self.b.log(actor, args.get("limit", 50))
        if name == N["what_if"]:
            return self.b.what_if(actor, dict(args["change"]))
        if name == N["replay"]:
            s = args.get("seeds") or {"lo": HELD_LO, "hi": HELD_HI}
            return self.b.replay(actor, args.get("params"), (s["lo"], s["hi"]), args.get("regimes"))
        if name == N["propose_config"]:
            p = self.b.propose(actor, args.get("params"), args.get("rationale", ""),
                               incident=(context or {}).get("incident"))
            self.drafts.append(p["id"])
            return {"proposal_id": p["id"], "status": p["status"], "changes": p["changes"], "fields": p["fields"],
                    "note": "draft only: a human must accept it before any 0x12 frame is built"}
        if name == N["request_kill"]:
            r = self.b.request_kill(actor, args.get("reason", ""))
            return {"result": r["result"], "cost_estimate": r["cost_estimate"],
                    "summary": r["observation"]["summary"]}
        from ..authority import NEVER
        if name in NEVER:                                            # e.g. "arm", "clear_latch"
            self.b.authorize(actor, name, {"args": args})            # raises with the specific reason
        self.b.authority.refuse(actor, name, f"unknown tool {name!r}; available: {sorted(TOOL_BY_NAME)}",
                                {"args": args}, "unknown_tool")
