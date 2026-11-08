"""Secret redaction. NEMOTRON_BASE_URL, NEMOTRON_MODEL and NVIDIA_API_KEY are never printed or logged.

install() wraps the logging record factory so every log record (any logger) has those values
replaced by [REDACTED] in its message and args. redact() is also applied to audit payloads,
API error messages and LLM-client errors. Values are read from the environment on each call
(they are never cached in a module global or written anywhere).
Named redaction.py, not secrets.py, so it does not shadow the stdlib module.
"""

import logging
import os

SECRET_ENV_VARS = ("NEMOTRON_BASE_URL", "NEMOTRON_MODEL", "NVIDIA_API_KEY")
MASK = "[REDACTED]"
_MIN_LEN = 4          # do not mask 1-3 character values (would mangle ordinary text)  TODO(FINAL)


def _values():
    vals = []
    for k in SECRET_ENV_VARS:
        v = os.environ.get(k)
        if v and len(v) >= _MIN_LEN:
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)       # longest first (a URL may contain the model)


def redact(obj):
    """Recursively replace secret values in str / dict / list / tuple. Other types pass through."""
    vals = _values()
    if not vals:
        return obj
    return _redact(obj, vals)


def _redact(obj, vals):
    if isinstance(obj, str):
        for v in vals:
            if v in obj:
                obj = obj.replace(v, MASK)
        return obj
    if isinstance(obj, dict):
        return {_redact(k, vals): _redact(v, vals) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_redact(x, vals) for x in obj)
    return obj


_installed = False


def install():
    """Idempotent: every LogRecord created anywhere gets msg/args redacted."""
    global _installed
    if _installed:
        return
    old = logging.getLogRecordFactory()

    def factory(*a, **kw):
        rec = old(*a, **kw)
        if _values():
            try:
                rec.msg = redact(rec.getMessage())
                rec.args = None
            except Exception:                              # never break logging
                rec.msg, rec.args = MASK, None
            if rec.exc_info and rec.exc_info[1] is not None:
                rec.exc_text = redact(logging.Formatter().formatException(rec.exc_info))
                rec.exc_info = None
        return rec

    logging.setLogRecordFactory(factory)
    _installed = True


def get_logger(name):
    install()
    return logging.getLogger(name)
