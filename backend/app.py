"""FastAPI app (SIMULATED arena). Run: uvicorn backend.app:app --port 8000   (from the repo root)

Endpoints (proposed; TODO(FINAL)):
  GET  /status                      decoded 0x20 + limit channels
  GET  /log?limit=50                decoded 0x21 records
  POST /sim/step                    {dt_s, new_session?, seed?, regime?, operator}   human: advance / power-cycle the twin
  POST /supervisor/run              {dt_s?, max_steps?, operator}                    human starts the agent loop
  GET  /proposals                   all proposals
  POST /proposals/{id}/accept       {approver}  human; the ONLY path that builds and sends a 0x12 frame
  POST /proposals/{id}/deny         {approver, reason?}  human; nothing is sent
  POST /kill                        {operator, reason?}  human; sends 0x15
  GET  /audit/verify                hash-chain check
  Extras (TODO(FINAL)): GET /audit?limit=, GET /reports
HTTP has no authentication (authority AT1): bind to localhost.
"""

from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import _paths  # noqa: F401
from . import config as C
from .agents.llm import make_llm
from .agents.orchestrator import Orchestrator
from .errors import AuthorityError, NotFound, SeedGuardError
from .redaction import get_logger, redact
from .runtime import Backend, StateError

log = get_logger("backend.app")
DASHBOARD = "human:dashboard"            # reads from the dashboard  TODO(FINAL)


class RedactedJSONResponse(JSONResponse):
    """Backstop: every JSON body is passed through redact() before it leaves the process."""

    def render(self, content):
        return super().render(redact(jsonable_encoder(content)))


class StepBody(BaseModel):
    dt_s: float = Field(0.01, ge=0, le=1.0)
    new_session: bool = False
    seed: Optional[int] = None
    regime: Optional[str] = None
    operator: str = "dashboard"


class RunBody(BaseModel):
    dt_s: float = Field(0.01, gt=0, le=0.1)
    max_steps: int = Field(60, ge=1, le=600)
    operator: str = "dashboard"


class DecideBody(BaseModel):
    approver: str = Field(..., min_length=1)
    reason: str = ""


class KillBody(BaseModel):
    operator: str = Field(..., min_length=1)
    reason: str = ""


def create_app(backend=None, llm=None):
    b = backend or Backend(workers=C.BACKEND_WORKERS)
    orch = Orchestrator(b, llm or make_llm())
    app = FastAPI(title="Latency Arena backend (SIMULATED)", default_response_class=RedactedJSONResponse)
    app.state.backend, app.state.orchestrator = b, orch

    @app.exception_handler(AuthorityError)
    async def _auth(_, e):
        return RedactedJSONResponse(status_code=403, content={"refused": True, "reason": redact(e.reason), "detail": e.detail})

    @app.exception_handler(SeedGuardError)
    async def _seed(_, e):
        return RedactedJSONResponse(status_code=403, content={"refused": True, "reason": redact(e.reason), "kind": "seed_guard"})

    @app.exception_handler(NotFound)
    async def _nf(_, e):
        return RedactedJSONResponse(status_code=404, content={"error": redact(str(e))})

    @app.exception_handler(StateError)
    async def _st(_, e):
        return RedactedJSONResponse(status_code=409, content={"error": redact(str(e))})

    @app.exception_handler(ValueError)
    async def _ve(_, e):
        return RedactedJSONResponse(status_code=400, content={"error": redact(str(e))})

    @app.get("/status")
    def status():
        return b.status(DASHBOARD)

    @app.get("/log")
    def get_log(limit: int = 50):
        return b.log(DASHBOARD, limit)

    @app.post("/sim/step")
    def sim_step(body: StepBody = Body(default_factory=StepBody)):
        return b.sim_step(f"human:{body.operator}", body.dt_s, body.new_session, body.seed, body.regime)

    @app.post("/supervisor/run")
    def supervisor_run(body: RunBody = Body(default_factory=RunBody)):
        b.authorize(f"human:{body.operator}", "run_supervisor", {"dt_s": body.dt_s, "max_steps": body.max_steps})
        return orch.run(body.dt_s, body.max_steps)

    @app.get("/proposals")
    def proposals():
        return {"proposals": b.proposals.list()}

    @app.post("/proposals/{pid}/accept")
    def accept(pid: str, body: DecideBody):
        p = b.accept(body.approver, pid)
        return {"proposal": p, "report": orch.review()}

    @app.post("/proposals/{pid}/deny")
    def deny(pid: str, body: DecideBody):
        p = b.deny(body.approver, pid, body.reason)
        return {"proposal": p, "report": orch.review()}

    @app.post("/kill")
    def kill(body: KillBody):
        return b.human_kill(body.operator, body.reason)

    @app.get("/audit/verify")
    def audit_verify():
        return b.verify_audit()

    @app.get("/audit")
    def audit(limit: int = 200):
        return {"records": b.audit_records(DASHBOARD, limit)}

    @app.get("/reports")
    def reports():
        return {"reports": b.reports}

    return app


def __getattr__(name):                     # `uvicorn backend.app:app` builds the app lazily
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
