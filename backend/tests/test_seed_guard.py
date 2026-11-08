"""Held-out seed guard: held-out checks use seeds 60-79 only; agents never see seeds 80+."""

import pytest

import evaluator as EV
import options_model as om
from backend.agents.orchestrator import Orchestrator
from backend.agents.tools import ToolBox
from backend.errors import HardRuleError, SeedGuardError
from backend.packs.options import replay as RP
from .conftest import last_refusal

SUP = "supervisor:test"


@pytest.mark.parametrize("seeds,held_out,ok", [
    ([79], False, True), ([0, 59], False, True), ([80], False, False), ([79, 80], False, False),
    ([99], False, False), ([-1], False, False), ([60, 79], True, True), ([59, 60], True, False),
    ([60, 80], True, False), ([], False, False), (["x"], False, False)])
def test_guard_seeds(seeds, held_out, ok):
    if ok:
        assert RP.guard_seeds(seeds, held_out) == sorted(set(seeds))
    else:
        with pytest.raises(SeedGuardError):
            RP.guard_seeds(seeds, held_out)


@pytest.fixture
def scen_seeds(monkeypatch):
    """Record every scenario the sim builds and every seed the evaluator is asked to run."""
    seen = {"scenario": [], "evaluator": []}
    orig_init, orig_run = om.OptionsMarketScenario.__init__, EV.run

    def init(self, seed, *a, **kw):
        seen["scenario"].append(int(seed))
        return orig_init(self, seed, *a, **kw)

    def run(params, seeds, regimes, workers=None):
        seen["evaluator"].extend(seeds)
        return orig_run(params, seeds, regimes, workers)

    monkeypatch.setattr(om.OptionsMarketScenario, "__init__", init)
    monkeypatch.setattr(EV, "run", run)
    return seen


@pytest.mark.parametrize("call", [
    lambda b: b.replay(SUP, None, (75, 85)),
    lambda b: b.replay(SUP, None, (80, 80)),
    lambda b: b.replay(SUP, {"edge_threshold_ticks": 2.0}, (99, 99)),
    lambda b: b.what_if(SUP, {"kind": "jump", "cents": 40, "seeds": [85]}),
    lambda b: b.what_if(SUP, {"kind": "jump", "cents": 40, "seeds": [59]}),            # not held out
    lambda b: b.what_if(SUP, {"kind": "config", "params": {"min_move_cents": 3}, "seeds": [60, 90]}),
    lambda b: b.plan(SUP, {"seeds": [70, 80]}),
    lambda b: b.sim_step("human:alice", 0, True, 80),
    lambda b: b.sim_step("human:alice", 0, True, 60),                                   # twin: 0-59 only
], ids=["replay-75-85", "replay-80", "replay-99", "whatif-85", "whatif-59", "whatif-config-90", "plan-80",
        "twin-80", "twin-60"])
def test_refused_before_anything_runs_and_audited(backend, scen_seeds, call):
    scen_seeds["scenario"].clear()
    with pytest.raises(SeedGuardError):
        call(backend)
    assert all(s < 80 for s in scen_seeds["scenario"] + scen_seeds["evaluator"])
    r = last_refusal(backend)
    assert r["payload"]["kind"] == "seed_guard"
    assert backend.verify_audit()["ok"]


def test_tool_schema_and_tool_call_refuse_80(backend, scen_seeds):
    tb = ToolBox(backend)
    rep = [t for t in tb.schemas(["replay"])][0]["function"]["parameters"]["properties"]["seeds"]
    assert rep["properties"]["hi"]["maximum"] == 79
    with pytest.raises(SeedGuardError):
        tb.call("replay", {"seeds": {"lo": 79, "hi": 80}}, SUP)
    assert 80 not in scen_seeds["evaluator"]


def test_twin_refuses_held_out_and_oos_seeds(backend):
    for s in (60, 79, 80, 99):
        with pytest.raises(SeedGuardError):
            backend.pack.initial_state(s)


def test_full_loop_never_touches_seed_80_plus(make_backend, llm, scen_seeds):
    b = make_backend()
    o = Orchestrator(b, llm)
    run = o.run(0.01, 10)
    assert run["status"] == "awaiting_human"
    b.what_if(SUP, {"kind": "jump", "cents": 40})
    b.what_if(SUP, {"kind": "jump", "cents": 25})                    # scenario-override path (RP2)
    b.accept("alice", run["proposals"][0])
    o.review()
    seen = scen_seeds["scenario"] + scen_seeds["evaluator"]
    assert seen and max(seen) < 80
    assert set(scen_seeds["evaluator"]) <= set(range(60, 80))       # held-out checks: 60-79 only
    twin = [s for s in scen_seeds["scenario"] if s < 60]
    assert set(twin) <= {5}


def test_a_scripted_llm_asking_for_seed_80_halts_the_loop(make_backend):
    from backend.agents.llm import LLMResponse, ToolCall
    from backend.agents.policy import demo_fake_llm
    b = make_backend()
    llm = demo_fake_llm({"stress": [LLMResponse("", [ToolCall("replay", {"seeds": {"lo": 78, "hi": 82}})])]})
    run = Orchestrator(b, llm).run(0.01, 10)
    assert run["status"] == "halted" and "seeds" in run["stop_reason"]
    assert last_refusal(b)["payload"]["kind"] == "seed_guard"
    assert issubclass(SeedGuardError, HardRuleError)
