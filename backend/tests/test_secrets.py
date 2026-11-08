"""Secrets (NEMOTRON_BASE_URL, NEMOTRON_MODEL, NVIDIA_API_KEY) never appear in logs, the audit log, API
responses, reports or error messages. Sentinel values only; no real key is read."""

import logging

import pytest

from backend.agents.llm import LLMConfigError, LLMResponse, NemotronClient, ToolCall
from backend.agents.orchestrator import Orchestrator
from backend.agents.policy import demo_fake_llm
from backend.redaction import MASK, get_logger, redact

SENT = {"NEMOTRON_BASE_URL": "https://sentinel-endpoint.invalid/v9/zz",
        "NEMOTRON_MODEL": "sentinel-model-q7x",
        "NVIDIA_API_KEY": "nvapi-SENTINEL-0123456789abcdef"}


@pytest.fixture
def secrets_env(monkeypatch):
    for k, v in SENT.items():
        monkeypatch.setenv(k, v)
    return SENT


def assert_clean(text):
    for v in SENT.values():
        assert v not in text, f"secret leaked: {v[:8]}..."


def test_redact_nested(secrets_env):
    obj = {"a": f"key={SENT['NVIDIA_API_KEY']}", "b": [SENT["NEMOTRON_BASE_URL"], {"c": SENT["NEMOTRON_MODEL"]}]}
    out = redact(obj)
    assert_clean(repr(out))
    assert out["a"] == f"key={MASK}" and out["b"][1]["c"] == MASK


def test_client_errors_and_repr_are_redacted(secrets_env, monkeypatch):
    c = NemotronClient()
    assert_clean(repr(c))
    with pytest.raises(NotImplementedError) as ei:
        c.chat([], [])
    assert_clean(str(ei.value))

    def leaky(**kw):
        raise ConnectionError(f"POST {kw['base_url']} model={kw['model']} auth=Bearer {kw['api_key']} failed")
    with pytest.raises(RuntimeError) as ei:
        NemotronClient(transport=leaky).chat([{"role": "user", "content": "x"}], [])
    assert_clean(str(ei.value))
    assert MASK in str(ei.value)

    def echo(**kw):
        return {"content": f"I am {kw['model']} at {kw['base_url']}", "tool_calls": []}
    r = NemotronClient(transport=echo).chat([], [])
    assert_clean(r.content)

    monkeypatch.delenv("NVIDIA_API_KEY")
    with pytest.raises(LLMConfigError) as ei:
        NemotronClient().chat([], [])
    assert "NVIDIA_API_KEY" in str(ei.value)
    assert_clean(str(ei.value))


def test_logs_are_redacted(secrets_env, caplog):
    log = get_logger("backend.test")
    other = logging.getLogger("some.other.library")
    with caplog.at_level(logging.DEBUG):
        log.info("calling %s with key %s", SENT["NEMOTRON_BASE_URL"], SENT["NVIDIA_API_KEY"])
        other.warning(f"model {SENT['NEMOTRON_MODEL']}")
        try:
            raise ValueError(f"bad key {SENT['NVIDIA_API_KEY']}")
        except ValueError:
            log.exception("failure")
    text = caplog.text + "".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert_clean(text)
    assert MASK in text


def test_full_loop_leaks_nothing(secrets_env, make_backend, client_for, capsys, caplog):
    # an LLM that tries to put the key into a proposal rationale and a kill reason
    def leaky_pick(messages, tools):
        from backend.agents.policy import proposal_pick
        r = proposal_pick(messages, tools)
        for tc in r.tool_calls:
            tc.arguments["rationale"] = f"{tc.arguments['rationale']} token={SENT['NVIDIA_API_KEY']}"
        return r
    llm = demo_fake_llm({"proposal": [leaky_pick],
                         "monitor": [LLMResponse(f"using {SENT['NEMOTRON_MODEL']}",
                                                 [ToolCall("request_kill", {"reason": SENT["NEMOTRON_BASE_URL"]})])]})
    b = make_backend()
    c = client_for(b, llm)
    with caplog.at_level(logging.DEBUG):
        code, run, raw_run = c.post("/supervisor/run", {"dt_s": 0.01, "max_steps": 10})
        assert code == 200 and run["status"] == "awaiting_human", run
        pid = run["proposals"][0]
        bodies = [raw_run]
        for path in ("/status", "/log", "/proposals", "/audit", "/audit/verify", "/reports"):
            bodies.append(c.get(path)[2])
        bodies.append(c.post(f"/proposals/{pid}/accept", {"approver": "alice"})[2])
        bodies.append(c.post("/proposals/P9999/deny", {"approver": "alice"})[2])
        bodies.append(c.post("/kill", {"operator": SENT["NVIDIA_API_KEY"]})[2])   # even as a "name"
    with open(b.audit.path, encoding="ascii") as f:
        audit_text = f.read()
    out = capsys.readouterr()
    for t in bodies + [audit_text, out.out, out.err, caplog.text]:
        assert_clean(t)
    assert MASK in audit_text                                   # the leak attempt was masked, not dropped
    assert b.verify_audit()["ok"]
