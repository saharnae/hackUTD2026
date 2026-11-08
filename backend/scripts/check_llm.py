"""Smoke-test the Nemotron endpoint, auto-pick a working Nemotron model, and save it to .env.
Run from backend/:  python -m scripts.check_llm
"""
import json
import os
import re

from dotenv import load_dotenv
from openai import OpenAI, NotFoundError, APIStatusError

load_dotenv()
BASE = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
WANT = os.getenv("NEMOTRON_MODEL", "")
c = OpenAI(base_url=BASE, api_key=os.getenv("NVIDIA_API_KEY") or "not-needed")
NOTHINK = {"chat_template_kwargs": {"enable_thinking": False}}
TOOL = {"type": "function", "function": {"name": "observe", "description": "Read the trading system status",
        "parameters": {"type": "object", "properties": {}, "required": []}}}

print(f"Endpoint: {BASE}")
try:
    ids = sorted(m.id for m in c.models.list().data)
except APIStatusError as e:
    raise SystemExit(f"!! Could not list models ({e.status_code}). Check NVIDIA_API_KEY in .env.")
nemo = [i for i in ids if "nemotron" in i.lower()]
print("Nemotron models on this key:")
for i in nemo:
    print("  -", i)


def rank(i):
    s = i.lower()
    return (0 if WANT and i == WANT else 1,
            0 if "super" in s else 1 if "ultra" in s else 2 if "nano" in s else 3,
            1 if any(x in s for x in ("vl", "omni", "embed", "rerank", "safety", "reward", "parse")) else 0)


def works(model):
    try:
        r = c.chat.completions.create(model=model, max_tokens=40, extra_body=NOTHINK,
                                      messages=[{"role": "user", "content": "Reply with exactly: Nemotron online"}])
        txt = (r.choices[0].message.content or "").strip()
        r = c.chat.completions.create(model=model, max_tokens=200, tools=[TOOL], tool_choice="auto", extra_body=NOTHINK,
                                      messages=[{"role": "user", "content": "Check the trading system status now."}])
        m = r.choices[0].message
        return txt, bool(m.tool_calls), (m.content or "")[:200]
    except (NotFoundError, APIStatusError) as e:
        print(f"  x {model}: {getattr(e, 'status_code', '?')} {str(e)[:120]}")
        return None


chosen = None
for model in sorted(nemo, key=rank)[:8]:
    print(f"\nTrying {model} ...")
    res = works(model)
    if res:
        txt, tools, raw = res
        print(f"  Chat: {txt}")
        print("  Tool calling OK" if tools else f"  No native tool_calls (agent will use JSON fallback). Reply: {json.dumps(raw)}")
        chosen = model
        if tools:
            break

if not chosen:
    raise SystemExit("\n!! No Nemotron model answered. Send this output to Claude.")
if chosen != WANT:
    p = ".env"
    s = open(p).read()
    s = re.sub(r"^NEMOTRON_MODEL=.*$", f"NEMOTRON_MODEL={chosen}", s, count=1, flags=re.M)
    open(p, "w").write(s)
    print(f"\nSaved NEMOTRON_MODEL={chosen} to .env")
print(f"\nREADY: using {chosen}")
