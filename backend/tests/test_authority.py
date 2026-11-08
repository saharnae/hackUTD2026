"""Authority: every refusal is raised AND written to the audit log; the allowlist is derived from the 0x12 decoder;
accepting a proposal (human) is the only path that produces a config frame."""

import pytest

import frames as F
from backend.agents.tools import ToolBox
from backend.authority import SUPERVISOR_ALLOWED
from backend.errors import AuthorityError
from backend.packs.options import configmap as CM
from .conftest import last_refusal

SUP = "supervisor:test"

REFUSALS = [
    # (actor, action, payload, expected kind)
    (SUP, "arm", None, "never_allowed"),
    (SUP, "clear_latch", None, "never_allowed"),
    (SUP, "set_hardware_limit", {"name": "net_delta_limit", "value": 40}, "never_allowed"),
    ("human:alice", "arm", None, "never_allowed"),
    ("human:alice", "clear_latch", None, "never_allowed"),
    ("human:alice", "set_hardware_limit", {"name": "jump_limit_cents"}, "never_allowed"),
    (SUP, "accept_proposal", {"proposal_id": "P0001"}, "not_permitted"),
    (SUP, "deny_proposal", {"proposal_id": "P0001"}, "not_permitted"),
    (SUP, "send_config_frame", {}, "not_permitted"),
    (SUP, "kill", {}, "not_permitted"),
    (SUP, "sim_step", {}, "not_permitted"),
    (SUP, "twin_new_session", {}, "not_permitted"),
    (SUP, "run_supervisor", {}, "not_permitted"),
    (SUP, "reboot_board", {}, "not_permitted"),
    ("human:alice", "propose_config", {"params": {"edge_threshold_ticks": 2.0}}, "not_permitted"),
    ("root", "read_status", None, "unknown_actor"),
    ("human:", "accept_proposal", {}, "unknown_actor"),
    ("human:   ", "accept_proposal", {}, "unknown_actor"),
    (None, "read_status", None, "unknown_actor"),
    (SUP, "propose_config", {"params": {"jump_limit_cents": 500}}, "hardware_limit"),
    (SUP, "propose_config", {"params": {"net_delta_limit": 40}}, "hardware_limit"),
    (SUP, "propose_config", {"params": {"max_position": 50, "edge_threshold_ticks": 2.0}}, "hardware_limit"),
    (SUP, "propose_config", {"params": {"heartbeat_us": 500000}}, "hardware_limit"),
    (SUP, "propose_config", {"params": {"hedge_band_shares": 500}}, "not_tunable"),
    (SUP, "propose_config", {"params": {"quote_width_bps": 4}}, "not_tunable"),
    (SUP, "propose_config", {"params": {"armed": 1}}, "not_tunable"),
    (SUP, "propose_config", {"params": {"edge_threshold_ticks": 5.0}}, "out_of_range"),
    (SUP, "propose_config", {"params": {"size_cap_base": 1}}, "out_of_range"),
    (SUP, "propose_config", {"params": {"time_stop_us": 50000}}, "out_of_range"),
    (SUP, "propose_config", {"params": {"min_move_cents": 2.5}}, "bad_type"),
    (SUP, "propose_config", {"params": {"edge_threshold_ticks": float("nan")}}, "bad_type"),
    (SUP, "propose_config", {"params": {"size_cap_base": True}}, "bad_type"),
    (SUP, "propose_config", {"params": {}}, "empty"),
    (SUP, "propose_config", {"params": {"size_cap_max": 3},
                             "current": {"edge_threshold_ticks": 2.0, "exit_edge_ticks": 1.0, "min_move_cents": 4,
                                         "max_orders_per_event": 2, "size_cap_base": 5, "size_cap_max": 8,
                                         "time_stop_us": 6833}}, "out_of_range"),
]


@pytest.mark.parametrize("actor,action,payload,kind", REFUSALS,
                         ids=[f"{a}-{b}-{d}" for a, b, _, d in REFUSALS])
def test_each_refusal_is_raised_and_audited(backend, actor, action, payload, kind):
    n0 = len(backend.audit.read_all())
    with pytest.raises(AuthorityError) as ei:
        backend.authorize(actor, action, payload)
    recs = backend.audit.read_all()
    assert len(recs) == n0 + 1
    r = recs[-1]
    assert r["action"] == "refused" and r["payload"]["requested"] == action and r["payload"]["kind"] == kind
    assert r["payload"]["reason"] == ei.value.reason and ei.value.detail["audit_index"] == r["index"]
    assert backend.verify_audit()["ok"]


def test_supervisor_allowed_actions_pass_and_are_audited(backend):
    for action in sorted(SUPERVISOR_ALLOWED - {"propose_config"}):
        backend.authorize(SUP, action, {})
        assert backend.audit.read_all()[-1]["action"] == action
    backend.authorize(SUP, "propose_config", {"params": {"edge_threshold_ticks": 2.0}})
    assert last_refusal(backend) is None


def test_allowlist_is_derived_from_the_0x12_decoder():
    assert set(CM.TUNABLE_PARAMS) == {"edge_threshold_ticks", "exit_edge_ticks", "min_move_cents",
                                      "max_orders_per_event", "size_cap_base", "size_cap_max", "time_stop_us"}
    for name, _, dflt, lo, hi in F.CONFIG_FIELDS:
        p, sc, _ = CM.FIELD_MAP[name]
        t = CM.TUNABLE[p]
        assert (t["lo"] * sc, t["hi"] * sc, t["default"] * sc) == (lo, hi, dflt)
    for hw in ("jump_limit_cents", "net_delta_limit", "net_vega_limit", "max_order_qty", "drawdown_limit"):
        assert hw in CM.HARDWARE_LIMITS and hw not in CM.TUNABLE
    assert "hedge_band_shares" not in CM.TUNABLE                      # CM1: not in the 0x12 frame


def test_tool_level_refusals_are_audited_and_create_nothing(backend):
    tb = ToolBox(backend)
    for name, args in (("arm", {}), ("clear_latch", {}), ("set_hardware_limit", {"jump_limit_cents": 999}),
                       ("send_config_frame", {}), ("accept_proposal", {"proposal_id": "P0001"})):
        with pytest.raises(AuthorityError):
            tb.call(name, args, SUP)
        assert last_refusal(backend)["payload"]["requested"] == name
    with pytest.raises(AuthorityError):
        tb.call("propose_config", {"params": {"net_vega_limit": 9999}, "rationale": "loosen"}, SUP)
    assert last_refusal(backend)["payload"]["kind"] == "hardware_limit"
    with pytest.raises(AuthorityError):
        tb.call("replay", {"params": {"max_order_qty": 50}, "seeds": {"lo": 60, "hi": 61}}, SUP)
    assert last_refusal(backend)["payload"]["kind"] == "hardware_limit"
    assert backend.proposals.list() == []


def _config_frames(backend):
    return [h for t, h in backend.source.frames_in if t == F.T_CONFIG]


def _ready_proposal(backend):
    p = backend.propose(SUP, {"edge_threshold_ticks": 2.5}, "test")
    backend.compare_proposal(SUP, p["id"])
    return backend.finalize_proposal(SUP, p["id"], {"summary": "t"})


def test_accept_is_the_only_config_frame_path(backend):
    backend.advance(SUP, 0.02)                                   # gate latched R03 -> board not armed
    p = _ready_proposal(backend)
    assert p["status"] == "pending_human" and _config_frames(backend) == []
    with pytest.raises(AuthorityError):
        backend.accept("", p["id"])                               # no named human
    with pytest.raises(AuthorityError):
        backend.authorize(SUP, "accept_proposal", {"proposal_id": p["id"]})
    assert _config_frames(backend) == []
    out = backend.accept("alice", p["id"])
    frames_sent = _config_frames(backend)
    assert out["status"] == "accepted" and len(frames_sent) == 1
    t, payload = F.decode(bytes.fromhex(frames_sent[0]))
    assert F.decode_config(payload) == p["fields"] and out["board_result"]["applied"] is True
    assert backend.source.state["config_register"] == p["fields"]


def test_deny_sends_nothing(backend):
    backend.advance(SUP, 0.02)
    p = _ready_proposal(backend)
    out = backend.deny("bob", p["id"], "not now")
    assert out["status"] == "denied" and _config_frames(backend) == []
    assert backend.audit.read_all()[-1]["action"] == "proposal_denied"


def test_http_refusals(backend, client_for):
    c = client_for(backend)
    backend.advance(SUP, 0.02)
    p = _ready_proposal(backend)
    code, body, _ = c.post(f"/proposals/{p['id']}/accept", {"approver": "   "})
    assert code == 403 and body["refused"]
    code, _, _ = c.post(f"/proposals/{p['id']}/accept", {})
    assert code == 422                                            # approver is required
    assert _config_frames(backend) == []
    code, body, _ = c.post(f"/proposals/{p['id']}/accept", {"approver": "alice"})
    assert code == 200 and body["proposal"]["status"] == "accepted" and len(_config_frames(backend)) == 1
    code, _, _ = c.post(f"/proposals/{p['id']}/accept", {"approver": "alice"})
    assert code == 409 and len(_config_frames(backend)) == 1      # no second frame
