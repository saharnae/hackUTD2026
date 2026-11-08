"""Latency Arena backend (SIMULATED arena): FastAPI app, pack interface, audit log, authority, agents."""

from . import _paths  # noqa: F401  (sys.path for golden/, sim/, slowpath/, tools/)
from . import redaction as _redaction

_redaction.install()
