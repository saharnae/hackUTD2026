"""Pack interface (options pack) and the domain-free Engine."""

import inspect
import json

import pytest

import frames as F
from backend import config as C
from backend.datasource import HardwareWriteDisabled, SerialSource, make_source
from backend.packs import base as BASE
from backend.packs import Engine
from backend.packs.options import OptionsPack
from backend.packs.options import configmap as CM


@pytest.fixture(scope="module")
def pack():
    return OptionsPack(regime="normal", stress_regimes=("single_jump",), held_out=(60, 61))


def test_step_is_pure_and_deterministic(pack):
    s0 = pack.initial_state(5)
    snap = json.dumps(s0, sort_keys=True)
    a = pack.step(s0, 0.2, [])
    b = OptionsPack(regime="normal").step(OptionsPack(regime="normal").initial_state(5), 0.2, [])
    assert json.dumps(s0, sort_keys=True) == snap                       # input untouched
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["t_us"] == 200000 and a["summary"]["armed"]           # armed after 2 heartbeats (E13/E14)
    assert pack.step(s0, 0.2, [])["channels"] == a["channels"]
    assert pack.initial_state(6)["seed"] == 6


def test_limits_and_cost(pack):
    s = pack.step(pack.initial_state(5), 0.03, [])
    lim = pack.limits(s)
    assert set(lim) == set(s["channels"]) == set(OptionsPack.CHANNEL_RULES)
    for v in lim.values():
        assert v["warn"] <= v["high"] <= v["trip"]
    assert lim["jump_cents"]["trip"] == 370 and lim["net_delta"]["trip"] == 20
    k = pack.cost(pack.kill_command(s))
    assert set(k) == {"dollars", "minutes", "notes"} and k["dollars"] >= 0
    assert pack.cost({"kind": "config"})["dollars"] == 0.0


def test_config_frame_ignored_while_armed(pack):
    s = pack.step(pack.initial_state(5), 0.2, [])
    assert s["summary"]["armed"]
    payload = CM.config_payload(dict(CM.from_fields(s["config_register"]), edge_threshold_ticks=2.0))
    s2 = pack.step(s, 0, [{"kind": "config_frame", "payload_hex": payload.hex()}])
    assert s2["last_config"]["applied"] is False and s2["config_rejects"] == 1
    assert s2["config_register"] == s["config_register"]


def test_new_session_uses_the_register(pack):
    s = pack.step(pack.initial_state(5), 0.03, [{"kind": "kill"}])
    s = pack.step(s, 0.0, [])
    assert s["summary"]["tripped"] and s["summary"]["trip_reason"] == 15
    payload = CM.config_payload(dict(CM.from_fields(s["config_register"]), edge_threshold_ticks=2.0))
    s = pack.step(s, 0, [{"kind": "config_frame", "payload_hex": payload.hex()}])
    assert s["last_config"]["applied"] is True                           # latched -> not armed
    s = pack.step(s, 0, [{"kind": "new_session"}])
    assert s["t_us"] == 0 and s["session_params"]["edge_threshold_ticks"] == 2.0 and not s["summary"]["tripped"]


def test_forecast_and_what_if_kill_generic(pack):
    eng = Engine(pack)
    s = pack.step(pack.initial_state(5), 0.02, [])
    f = eng.forecast(s, 0.03)
    assert len(f["points"]) == 3 and f["points"][-1]["t_s"] == 0.03
    w = eng.what_if(s, {"kind": "kill"})["result"]
    assert w["method"] == "forecast comparison"


class TankPack:
    """Toy non-financial pack: a tank filling at `rate`; commands set the rate."""
    name = "tank"
    forecast_step_s = 1.0

    def initial_state(self, seed):
        return {"t": 0.0, "rate": 1.0 + seed, "channels": {"level": 0.0}}

    def step(self, state, dt_s, commands):
        rate = state["rate"]
        for c in commands:
            rate = c["rate"]
        lvl = state["channels"]["level"] + rate * dt_s
        return {"t": state["t"] + dt_s, "rate": rate, "channels": {"level": lvl}}

    def limits(self, state):
        return {"level": {"warn": 5.0, "high": 8.0, "trip": 10.0}}

    def cost(self, command):
        return {"dollars": 0.0, "minutes": 0.0, "notes": ""}

    def candidate_actions(self, state, goal):
        return [{"kind": "rate", "rate": r} for r in (3.0, 0.5, 1.5)]

    def evaluate_action(self, state, action, goal):
        rate = state["rate"] if action is None else action["rate"]
        final = state["channels"]["level"] + rate * goal["horizon"]
        return {"ok": final < 10.0, "score": -final, "metrics": {"final": final}}


def test_engine_is_domain_free():
    src = inspect.getsource(BASE)
    for word in ("golden", "option", "SPY", "frames", "0x12", "disaster"):
        assert word not in src.split('"""', 2)[2], word                   # code after the module docstring
    p = TankPack()
    eng = Engine(p)
    s = p.initial_state(1)                                               # rate 2
    f = eng.forecast(s, 6.0)
    assert [(c["level"], c["t_s"]) for c in f["crossings"]] == [("warn", 3.0), ("high", 4.0), ("trip", 5.0)]
    assert eng.what_if(s, {"kind": "rate", "rate": 3.0})["result"]["with_change"]["level"] == 3.0
    plan = eng.plan_actions(s, {"horizon": 4.0})
    assert [c["action"]["rate"] for c in plan["candidates"]] == [0.5, 1.5]
    assert [c["action"]["rate"] for c in plan["filtered_out"]] == [3.0]
    assert all(c["improves"] for c in plan["candidates"])


def test_serial_is_off_by_default_and_never_writes():
    assert C.USE_SERIAL is False and C.SERIAL_WRITE_ENABLED is False
    with pytest.raises(RuntimeError):
        make_source(OptionsPack(), use_serial=True)                      # no port configured
    s = object.__new__(SerialSource)
    s.write_enabled = False
    with pytest.raises(HardwareWriteDisabled):
        s.send_frame(F.encode(F.T_KILL, b""))


def test_unmodeled_status_fields_are_reported(make_backend):
    b = make_backend()
    obs = b.status("supervisor:test")
    assert "last_latency_ns" in obs["unmodeled_status_fields"]
    assert obs["status"]["last_latency_ns"] == 0
