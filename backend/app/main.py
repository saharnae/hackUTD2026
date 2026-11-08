"""FastAPI server for the Nemotron supervisor. Agent events stream as Server-Sent Events."""
from __future__ import annotations

import asyncio
import json
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from typing import Optional  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from .adapters import get_adapter  # noqa: E402
from .supervisor import decide, run_supervisor  # noqa: E402

app = FastAPI(title="Latency Arena Supervisor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def sse(job):
    """Run job(emit) in the background and stream its events."""
    q: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data: dict):
        await q.put(f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n")

    async def worker():
        try:
            await job(emit)
        except Exception as e:
            await emit("error", {"message": f"{type(e).__name__}: {e}"})
        finally:
            await q.put(None)

    async def stream():
        task = asyncio.create_task(worker())
        try:
            while (item := await q.get()) is not None:
                yield item
        finally:
            task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/health")
async def health():
    a = get_adapter()
    return {"ok": True, "mock": os.getenv("AGENT_MOCK") == "1", "adapter": a.name,
            "model": os.getenv("NEMOTRON_MODEL", "nvidia/nemotron-3-nano")}


@app.get("/api/state")
async def state():
    return get_adapter().observe()


class Inject(BaseModel):
    kind: str


@app.post("/api/inject")
async def inject(req: Inject):
    a = get_adapter()
    if not hasattr(a, "inject"):
        return {"ok": False, "reason": f"{a.name} has no demo inject hook"}
    return a.inject(req.kind)


@app.post("/api/supervisor/run")
async def run():
    return sse(run_supervisor)


class Decision(BaseModel):
    accept: bool
    action_id: Optional[str] = None
    by: str = "engineer"


@app.post("/api/proposals/{pid}/decide")
async def decide_route(pid: str, d: Decision):
    return sse(lambda emit: decide(pid, d.accept, d.action_id, d.by, emit))


@app.get("/api/audit")
async def audit():
    return get_adapter().audit_log(50)
