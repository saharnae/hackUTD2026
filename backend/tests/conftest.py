import asyncio
import itertools
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.runtime import Backend  # noqa: E402
from backend.agents.policy import demo_fake_llm  # noqa: E402

SMALL_HELD_OUT = (60, 61)                      # held-out seeds used by the tests (subset of 60-79, for speed)
SMALL_REGIMES = ("single_jump", "feed_fault")


def fixed_clock():
    n = itertools.count()
    return lambda: f"2026-11-08T00:00:{next(n) % 60:02d}.000000+00:00"


@pytest.fixture
def audit_path(tmp_path):
    return str(tmp_path / "audit.jsonl")


@pytest.fixture
def make_backend(audit_path):
    def _make(seed=5, regime="feed_fault", **kw):
        kw.setdefault("held_out_seeds", SMALL_HELD_OUT)
        kw.setdefault("stress_regimes", SMALL_REGIMES)
        return Backend(audit_path=kw.pop("audit_path", audit_path), seed=seed, regime=regime,
                       clock=fixed_clock(), **kw)
    return _make


@pytest.fixture
def backend(make_backend):
    return make_backend()


@pytest.fixture
def llm():
    return demo_fake_llm()


class ASGIClient:
    """Minimal in-process ASGI client (httpx is not installed, so FastAPI's TestClient is unavailable)."""

    def __init__(self, app):
        self.app = app

    def request(self, method, path, body=None):
        path, _, query = path.partition("?")
        raw = json.dumps(body).encode() if body is not None else b""

        async def run():
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                     "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                     "root_path": "", "client": ("127.0.0.1", 1), "server": ("testserver", 80),
                     "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                                 (b"content-length", str(len(raw)).encode())]}
            msgs = [{"type": "http.request", "body": raw, "more_body": False}]
            out = {"status": None, "body": b""}

            async def receive():
                return msgs.pop(0) if msgs else {"type": "http.disconnect"}

            async def send(m):
                if m["type"] == "http.response.start":
                    out["status"] = m["status"]
                elif m["type"] == "http.response.body":
                    out["body"] += m.get("body", b"")

            await self.app(scope, receive, send)
            return out

        out = asyncio.run(run())
        text = out["body"].decode()
        return out["status"], (json.loads(text) if text else None), text

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})


@pytest.fixture
def client_for():
    def _c(backend, llm=None):
        from backend.app import create_app
        return ASGIClient(create_app(backend, llm or demo_fake_llm()))
    return _c


def last_refusal(backend):
    recs = [r for r in backend.audit.read_all() if r["action"] == "refused"]
    return recs[-1] if recs else None
