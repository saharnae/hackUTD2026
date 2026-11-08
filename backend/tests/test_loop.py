"""Full supervisor loop on a twin-driven trip, driven only by the FakeLLM (offline)."""

import frames as F
from backend.agents.llm import LLMResponse, ToolCall
from backend.agents.orchestrator import Orchestrator
from backend.agents.policy import demo_fake_llm
from .conftest import SMALL_HELD_OUT, last_refusal

PHASES = ["observe", "detect", "observe", "detect", "triage", "forecast", "propose", "compare", "write"]


def _cfg_frames(b):
    return [h for t, h in b.source.frames_in if t == F.T_CONFIG]


def test_full_loop_twin_trip_accept_via_api(make_backend, client_for):
    b = make_backend(seed=5, regime="feed_fault")            # seed 5 feed_fault: real R03 latch at ~13.2 ms
    llm = demo_fake_llm()
    c = client_for(b, llm)
    code, run, _ = c.post("/supervisor/run", {"dt_s": 0.01, "max_steps": 20})
    assert code == 200
    # observe, detect, forecast, propose, compare, write ... then stop for the human
    assert [p["phase"] for p in run["phases"]] == PHASES
    assert run["status"] == "awaiting_human" and run["stop_reason"].startswith("hard-rule trip")
    inc = run["incidents"][0]
    assert inc["kind"] == "gate_trip" and inc["rule"] == "R03" and inc["latched"]
    # the trip is real: the twin's engine latched R03 through golden gate logic
    st = b.source.state["summary"]
    assert st["tripped"] and st["trip_reason"] == 3 and 13000 < st["trip_t_us"] < 14000
    code, status, _ = c.get("/status")
    assert status["status"]["state_bits"]["tripped"] == 1 and status["status"]["trip_reason"] == 3
    # the loop stopped advancing the twin at the trip
    assert b.source.state["t_us"] == 20000
    # one proposal, compared on held-out seeds, waiting for a human; no frame yet
    code, props, _ = c.get("/proposals")
    (p,) = props["proposals"]
    assert p["status"] == "pending_human" and p["created_by"] == "supervisor:proposal"
    cmp = p["comparison"]
    assert cmp["seeds"] == list(SMALL_HELD_OUT) and cmp["regimes"] == ["single_jump", "feed_fault"]
    assert "SIMULATED" in cmp["label"] and p["write_up"]["summary"]
    assert _cfg_frames(b) == []
    # human decides
    code, out, _ = c.post(f"/proposals/{p['id']}/accept", {"approver": "alice"})
    assert code == 200 and out["proposal"]["status"] == "accepted"
    assert len(_cfg_frames(b)) == 1 and out["proposal"]["board_result"]["applied"] is True
    # review
    rep = out["report"]
    assert rep["incident_id"] == "inc-trip-R03" and rep["audit_chain"]["ok"]
    assert [d["action"] for d in rep["decisions"]] == ["config_frame_sent"]
    assert rep["narrative"] and rep["proposals"]
    code, ver, _ = c.get("/audit/verify")
    assert ver["ok"]
    acts = [r["action"] for r in b.audit.read_all()]
    for a in ("supervisor_run_started", "incident_detected", "proposal_drafted", "proposal_compared",
              "proposal_ready", "supervisor_run_finished", "config_frame_sent", "report_written"):
        assert a in acts
    assert acts.index("supervisor_run_finished") < acts.index("config_frame_sent")


def test_deny_path_sends_nothing_and_reviews(make_backend, llm):
    b = make_backend()
    o = Orchestrator(b, llm)
    run = o.run(0.01, 20)
    b.deny("bob", run["proposals"][0], "keep current config")
    rep = o.review()
    assert _cfg_frames(b) == [] and [d["action"] for d in rep["decisions"]] == ["proposal_denied"]


def test_non_latching_jump_block_kill_requested(make_backend, llm):
    """flash_crash: 5 x -150 c drives |S - S_ref| toward R04 (370 c, non-latching); the Monitor asks for a kill."""
    b = make_backend(seed=5, regime="flash_crash")
    run = Orchestrator(b, llm).run(0.01, 60)
    inc = run["incidents"][0]
    assert inc["id"] == "inc-jump_cents" and inc["rule"] == "R04" and not inc["latched"]
    assert inc["detail"]["level"] in ("high", "trip") and inc["detail"]["value"] >= 0.8 * 370
    kills = [f for t, f in b.source.frames_in if t == F.T_KILL]
    assert len(kills) == 1
    assert b.source.state["summary"]["kill"] and b.source.state["summary"]["trip_reason"] == 15
    assert run["phases"][-5]["phase"] == "triage" and run["phases"][-5]["kills"] == 1


def test_forbidden_tool_call_halts_the_loop(make_backend):
    b = make_backend()
    llm = demo_fake_llm({"monitor": [LLMResponse("", [ToolCall("clear_latch", {})])]})
    run = Orchestrator(b, llm).run(0.01, 20)
    assert run["status"] == "halted" and "KEY0" in run["stop_reason"]
    assert last_refusal(b)["payload"]["requested"] == "clear_latch"
    assert b.proposals.list() == [] and _cfg_frames(b) == []
    assert run["report"] and b.reports[-1]["refusals"]


def test_hardware_limit_proposal_halts_the_loop(make_backend):
    b = make_backend()
    llm = demo_fake_llm({"proposal": [LLMResponse("", [ToolCall("propose_config", {
        "params": {"jump_limit_cents": 900}, "rationale": "loosen the jump guard"})])]})
    run = Orchestrator(b, llm).run(0.01, 20)
    assert run["status"] == "halted" and "hardware limit" in run["stop_reason"]
    assert last_refusal(b)["payload"]["kind"] == "hardware_limit"
    assert b.proposals.list() == []


def test_loop_is_deterministic(make_backend, tmp_path):
    def once(i):
        b = make_backend(audit_path=str(tmp_path / f"a{i}.jsonl"))
        run = Orchestrator(b, demo_fake_llm()).run(0.01, 20)
        p = b.proposals.get(run["proposals"][0])
        return run["phases"], p["changes"], p["comparison"], [(r["actor"], r["action"], r["payload"])
                                                              for r in b.audit.read_all()]
    assert once(1) == once(2)


def test_no_incident_quiet_session(make_backend, llm):
    b = make_backend(seed=5, regime="quiet")
    run = Orchestrator(b, llm).run(0.05, 3)
    assert run["status"] == "no_incident" and run["report"] and b.proposals.list() == []


def test_policy_proposes_nothing_when_no_candidate_beats_the_current_config():
    import json
    from backend.agents.policy import proposal_pick
    payload = {"baseline": {"score": -7.5, "ok": True},
               "candidates": [{"params": {"min_move_cents": 3}, "source": "t", "score": -11.6, "improves": False}]}
    r = proposal_pick([{"role": "system", "content": ""}, {"role": "user", "content": json.dumps(payload)}], [])
    assert r.tool_calls == [] and "no proposal" in r.content and "-7.5000" in r.content


def test_no_proposal_run_still_reviews(make_backend):
    from backend.agents.policy import demo_fake_llm
    b = make_backend()
    llm = demo_fake_llm({"proposal": [LLMResponse("nothing beats the current config")]})
    run = Orchestrator(b, llm).run(0.01, 20)
    assert run["status"] == "no_proposal" and run["report"] and b.proposals.list() == []
    assert b.reports[-1]["incident_id"] == "inc-trip-R03"
