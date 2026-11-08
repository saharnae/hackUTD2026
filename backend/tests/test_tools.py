"""Tool schemas, and every tool hits the real twin / engine / evaluator (no canned data)."""

import json

import evaluator as EV
import frames as F
import golden as g
from backend import config as C
from backend.agents.tools import TOOL_SCHEMAS, ToolBox
from backend.packs.options import configmap as CM
from backend.packs.options import frames_io as FIO

SUP = "supervisor:test"


def test_schemas_are_json_schema_function_definitions():
    names = [t["function"]["name"] for t in TOOL_SCHEMAS]
    assert tuple(names) == C.TOOL_NAMES
    for t in TOOL_SCHEMAS:
        assert t["type"] == "function"
        prm = t["function"]["parameters"]
        assert prm["type"] == "object" and prm["additionalProperties"] is False
        assert set(prm["required"]) <= set(prm["properties"])
        json.dumps(t)
    pc = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "propose_config")["function"]["parameters"]
    props = pc["properties"]["params"]["properties"]
    assert set(props) == set(CM.TUNABLE_PARAMS)
    assert props["edge_threshold_ticks"]["minimum"] == 1.0 and props["edge_threshold_ticks"]["maximum"] == 3.0
    for hw in CM.HARDWARE_LIMITS:
        assert hw not in props


def test_get_status_and_get_log_decode_real_frames(backend):
    tb = ToolBox(backend)
    backend.sim_step("human:alice", 0, True, 0, "normal")
    backend.advance(SUP, 0.2)                                           # armed after ~100 ms; sends at ~110.7 ms
    s = tb.call("get_status", {}, SUP)
    e = backend.pack.engine_at(backend.source.state)
    sends = [x for x in e.log if x[0] == "send"]
    assert s["status"]["now_us"] == 200000 == backend.source.state["t_us"]
    assert s["status"]["orders_sent"] == len(sends) and s["status"]["tokens"] == e.tokens
    assert s["status"]["state_bits"]["armed"] == int(e.armed and not e.latched)
    assert "SIMULATED" in s["source"]
    log = tb.call("get_log", {"limit": 200}, SUP)["records"]
    assert len([r for r in log if r["kind"] == "decision"]) == len(sends) > 0
    assert [(r["ts_us"], r["qty"], r["price"]) for r in log if r["kind"] == "decision"] ==         [(int(x[1]), x[6], x[7]) for x in sends]
    backend.sim_step("human:alice", 0, True, 5, "feed_fault")
    backend.advance(SUP, 0.02)                                          # R03 latches at ~13.2 ms
    s = tb.call("get_status", {}, SUP)
    assert s["status"]["state_bits"]["tripped"] == 1 and s["status"]["trip_reason"] == 3


def test_frame_codecs_round_trip():
    rec = {"rec_type": 2, "ts_us": 123456, "is_hedge": 1, "side": 1, "contract": 0, "qty": 700, "price": 73350,
           "reason": 9, "flags": 5, "fail_mask": 0x100, "client_id": 42, "tokens": 17, "orders_in_event": 3}
    t, p = F.decode(FIO.log_frame(rec))
    d = FIO.decode_log_record(p)
    assert t == F.T_LOG and {k: d[k] for k in rec} == rec


def test_what_if_jump_runs_golden_reprice_and_the_evaluator(backend):
    tb = ToolBox(backend)
    backend.advance(SUP, 0.012)
    w = tb.call("what_if", {"change": {"kind": "jump", "cents": 40}}, SUP)["result"]
    e = backend.pack.engine_at(backend.source.state)
    sn = e.snap
    for row, k in zip(w["shock_table"]["rows"], sn["contracts"]):
        assert row["theo_new_ticks"] * 256 == g.reprice(k["theo_0"], k["delta_adj"], k["gamma_c"], 40 * 16)
        assert abs(row["reprice_error_ticks"]) < 0.05
    assert w["shock_table"]["r04_blocks"] is False and len(w["shock_table"]["io_shock_rows"]) == 16
    params = backend.pack.current_params(backend.source.state)
    doc = EV.evaluate("confirm", params, "60-61", ["single_jump"], workers=1)
    assert w["replay"]["determinism_hash"] == doc["metrics"]["determinism_hash"]
    big = tb.call("what_if", {"change": {"kind": "jump", "cents": 400}}, SUP)["result"]
    assert big["shock_table"]["r04_blocks"] is True and big["replay"]["scenario_override"] == {"single_jump_cents": 400.0}


def test_replay_matches_the_real_evaluator(backend):
    tb = ToolBox(backend)
    r = tb.call("replay", {"seeds": {"lo": 60, "hi": 61}, "regimes": ["normal", "feed_fault"]}, SUP)
    params = backend.pack.current_params(backend.source.state)
    doc = EV.evaluate("confirm", params, "60-61", ["normal", "feed_fault"], workers=1)
    assert r["determinism_hash"] == doc["metrics"]["determinism_hash"] and r["params_hash"] == doc["params_hash"]
    assert r["per_regime"]["feed_fault"]["trip_reasons"] == {"R03": 2}
    r2 = tb.call("replay", {"params": {"edge_threshold_ticks": 2.0}, "seeds": {"lo": 0, "hi": 1},
                            "regimes": ["normal"]}, SUP)
    assert r2["params"]["edge_threshold_ticks"] == 2.0 and r2["split"] == "in_sample_search_0_59"


def test_propose_config_tool_only_drafts(backend):
    tb = ToolBox(backend)
    out = tb.call("propose_config", {"params": {"edge_threshold_ticks": 2.3}, "rationale": "r"}, SUP)
    p = backend.proposals.get(out["proposal_id"])
    assert p["status"] == "draft" and p["fields"]["edge_threshold"] == 589               # floor(2.3*256 + 0.5)
    assert p["params"]["edge_threshold_ticks"] == 589 / 256
    assert [t for t, _ in backend.source.frames_in] == []


def test_request_kill_tool_latches_r15(backend):
    backend.sim_step("human:alice", 0, True, 5, "normal")
    backend.advance(SUP, 0.03)
    out = ToolBox(backend).call("request_kill", {"reason": "test"}, SUP)
    assert out["result"]["frame"] == "0x15"
    s = backend.status(SUP)["status"]
    assert s["state_bits"]["tripped"] == 1 and s["trip_reason"] == 15 and s["state_bits"]["kill_pin"] == 1
    assert out["cost_estimate"]["dollars"] is not None
