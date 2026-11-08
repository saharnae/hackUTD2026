"""TradingAdapter (latency_arena_adapter/app/adapters/trading.py), offline. SIMULATED arena.

The agents' sequence runs once per module (fixture `seq`) in-process (workers=1) so every engine call
is visible to the seed instrumentation. One simulate regime (feed_fault) keeps it to ~1 minute.
"""

import json
import os
import re

import pytest

from latency_arena_adapter.app.adapters import trading as T
from latency_arena_adapter.app.interface import Action, ApplyResult, Incident, Outcome, to_json

import evaluator as EV
import frames as F
import options_model as om

MOCK_OBSERVE_KEYS = {"source", "clock_s", "gate", "token_bucket", "bad_frames_1m", "feed_age_ms", "position",
                     "pnl_today", "config"}
MOCK_GATE_KEYS = {"armed", "latched", "trip_reason", "killed"}
MOCK_POSITION_KEYS = {"symbol", "net_delta", "open_orders"}
SEEDS = list(range(100))


def make(tmp, name, **kw):
    kw.setdefault("workers", 1)
    kw.setdefault("sim_regimes", ("feed_fault",))
    return T.TradingAdapter(audit_path=os.path.join(str(tmp), name), seed=5, **kw)


@pytest.fixture(scope="module")
def seq(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("adapter")
    seen = {"scenario": [], "evaluator": []}
    mp = pytest.MonkeyPatch()
    orig_init, orig_run = om.OptionsMarketScenario.__init__, EV.run

    def init(self, seed, *a, **kw):
        seen["scenario"].append(int(seed))
        return orig_init(self, seed, *a, **kw)

    def run(params, seeds, regimes, workers=None):
        seen["evaluator"].extend(seeds)
        return orig_run(params, seeds, regimes, workers)

    mp.setattr(om.OptionsMarketScenario, "__init__", init)
    mp.setattr(EV, "run", run)
    try:
        a = make(tmp, "audit.jsonl")
        out = {"adapter": a, "seen": seen}
        out["obs0"] = a.observe()
        out["inject"] = a.inject("gate_trip")
        out["state"] = a.observe()
        out["incidents"] = a.detect(out["state"])
        out["actions"] = a.actions(out["incidents"][0])
        out["checks"] = {x.id: a.check(x) for x in out["actions"]}
        out["sim"] = {"baseline": a.simulate(out["state"], None, SEEDS)}
        for x in out["actions"]:
            out["sim"][x.id] = a.simulate(out["state"], x, SEEDS)
        byid = {x.id: x for x in out["actions"]}
        out["apply_kill"] = a.apply(byid["kill"], None)
        out["apply_cfg_noapprover"] = a.apply(byid["raise_edge"], None)
        out["apply_cfg_alice"] = a.apply(byid["raise_edge"], "alice")
        out["apply_hw"] = a.apply(Action("raise_delta_direct", "change a hardware limit",
                                         {"frame": "0x12", "jump_limit_cents": 900}, False), "alice")
        out["apply_demo"] = a.apply(byid["raise_delta"], "alice")
        out["apply_arm"] = a.apply(Action("arm", "arm the gate", {"frame": "0x16", "arm": True}, False), "alice")
        out["apply_clear"] = a.apply(Action("clear_latch", "clear the latch", {"frame": "0x16", "clear_latch": True},
                                            False), "alice")
        out["invariants"] = a.invariants()
        out["template"] = a.report_template()
        out["audit"] = a.audit_log(50)
        out["audit_all"] = a.audit_log(0)
        out["inject_stale"] = a.inject("stale_feed")
        yield out
    finally:
        mp.undo()


# ---------------------------------------------------------------- 1. the agents' sequence
def test_observe_has_the_mock_keys_and_real_values(seq):
    for o in (seq["obs0"], seq["state"]):
        assert set(o) == MOCK_OBSERVE_KEYS
        assert set(o["gate"]) == MOCK_GATE_KEYS and set(o["position"]) == MOCK_POSITION_KEYS
        assert o["position"]["symbol"] == "SPY"
        assert "SIMULATED" in o["source"] and "twin" in o["source"]
        assert o["bad_frames_1m"] is None and o["position"]["open_orders"] is None      # no real source
        assert set(o["config"]) >= {"edge_threshold_ticks", "min_move_cents", "size_cap_base", "size_cap_max"}
    st = seq["state"]
    assert st["gate"]["latched"] and st["gate"]["trip_reason"] == "R03 feed integrity"
    a = seq["adapter"]
    assert seq["state"]["clock_s"] == 0.015                                  # twin clock when observed after inject
    e = a.b.pack.engine_at(a.b.source.state)
    assert st["token_bucket"] == max(0, min(20, e.tokens))


def test_inject_gate_trip_is_a_real_engine_latch(seq):
    inj = seq["inject"]
    assert inj["ok"] and inj["rule"] == "R03" and 13000 < inj["trip_t_us"] < 14000
    e = seq["adapter"].b.pack.engine_at(seq["adapter"].b.source.state)
    assert e.latched and e.latch_reasons[0]["reason"] == 3           # first latch cause (kill later adds none)
    assert e.feed_bad                                                 # the engine saw a real gap/duplicate
    assert seq["inject_stale"]["ok"] is False and "not supported" in seq["inject_stale"]["reason"]


def test_detect_carries_the_real_rule(seq):
    incs = seq["incidents"]
    assert incs and isinstance(incs[0], Incident)
    i = incs[0]
    assert (i.id, i.kind, i.severity) == ("inc-gate", "gate_trip", 3)
    assert i.detail["rule"] == "R03" and i.detail["rule_name"] == "feed integrity" and i.detail["latching"]
    assert [x.severity for x in incs] == sorted([x.severity for x in incs], reverse=True)


def test_actions_use_only_the_allowlist(seq):
    from latency_arena_adapter.app.adapters.trading import CM
    acts = {x.id: x for x in seq["actions"]}
    assert list(acts) == ["kill", "raise_edge", "raise_min_move", "size_down", "raise_delta"]
    assert acts["kill"].params == {"frame": "0x15"} and acts["kill"].reduces_risk is True
    for aid in ("raise_edge", "raise_min_move", "size_down"):
        p = {k: v for k, v in acts[aid].params.items() if k != "frame"}
        assert acts[aid].params["frame"] == "0x12" and not acts[aid].reduces_risk
        assert set(p) <= set(CM.TUNABLE_PARAMS) and CM.check_change(p) == []
    assert "DEMONSTRATION" in acts["raise_delta"].label
    assert all(x.id not in ("widen_quotes", "tighten_stale", "tighten_hedge") for x in seq["actions"])


def test_check_each_action(seq):
    c = seq["checks"]
    for aid in ("kill", "raise_edge", "raise_min_move", "size_down"):
        assert c[aid][0] is True, c[aid]
    assert c["raise_delta"][0] is False and "R09" in c["raise_delta"][1] and "hardware limit" in c["raise_delta"][1]


def test_simulate_baseline_and_each_action(seq):
    for aid, o in seq["sim"].items():
        assert isinstance(o, Outcome) and o.source == "SIMULATED (golden engine, twin timing not FPGA latency)"
        assert o.out_of_sample == {"evaluated": False, "note": "reserved for the one-shot final evaluation"}
        assert o.metrics["seeds_requested"] == 100 and o.metrics["seeds_not_run"] == 20
        if aid == "raise_delta":
            assert o.in_sample["evaluated"] is False and o.in_sample["refused"]
            continue
        ins = o.in_sample
        assert ins["evaluated"] and ins["seeds"] == 80 and o.metrics["seeds_run"] == 80
        for k in ("mean_pnl", "mean_max_drawdown", "mean_gate_trips"):
            assert isinstance(ins[k], float)
        assert ins["search"]["seeds"] == 60 and ins["search"]["seed_ranges"] == [[0, 59]]
        assert ins["confirm"]["seeds"] == 20 and ins["confirm"]["seed_ranges"] == [[60, 79]]
        assert "NOT a replay from the live state" in o.metrics["method"]
    assert seq["sim"]["kill"].in_sample["mean_gate_trips"] == 1.0           # every killed session latched
    assert seq["sim"]["baseline"].metrics["params_hash"] != seq["sim"]["raise_edge"].metrics["params_hash"]


def test_apply_paths(seq):
    k, n, y = seq["apply_kill"], seq["apply_cfg_noapprover"], seq["apply_cfg_alice"]
    assert isinstance(k, ApplyResult) and k.ok and k.detail["frame"] == "0x15" and k.detail["kill"] is True
    assert k.detail["trip_reason"] == "R03"            # already latched R03; the first latch cause is kept
    assert not n.ok and n.reason == "Refused: this change needs a named human approver."
    assert y.ok and y.detail["frame"] == "0x12" and y.detail["approved_by"] == "alice" and y.detail["board_applied"]
    sent = [(t, h) for t, h in seq["adapter"].b.source.frames_in]
    assert [t for t, _ in sent] == [F.T_KILL, F.T_CONFIG]                   # exactly one kill, one config frame
    assert F.decode_config(F.decode(bytes.fromhex(sent[1][1]))[1])["edge_threshold"] == 768   # 3.0 ticks Q8
    for key, needle in (("apply_hw", "R04"), ("apply_demo", "R09"), ("apply_arm", "SW9"), ("apply_clear", "KEY0")):
        r = seq[key]
        assert not r.ok and needle in r.reason, (key, r.reason)


def test_check_refusals(tmp_path):
    a = make(tmp_path, "c.jsonl")
    cur = a.observe()["config"]
    cases = {
        "hardware": ({"frame": "0x12", "net_vega_limit": 5000}, "hardware limit"),
        "unknown": ({"frame": "0x12", "quote_width_bps": 12}, "allowlist"),
        "hedge": ({"frame": "0x12", "hedge_band_shares": 300}, "allowlist"),
        "range": ({"frame": "0x12", "edge_threshold_ticks": 3.5}, "outside the accepted range"),
        "size_up": ({"frame": "0x12", "size_cap_base": cur["size_cap_base"] + 1}, "raises exposure"),
        "size_max_up": ({"frame": "0x12", "size_cap_max": cur["size_cap_max"] + 1}, "raises exposure"),
        "frame": ({"frame": "0x14", "qty": 10}, "unknown frame"),
        "kill_params": ({"frame": "0x15", "qty": 1}, "no parameters"),
    }
    for name, (params, needle) in cases.items():
        ok, why = a.check(Action(name, name, params, False))
        assert not ok and needle in why and why.startswith("Refused: ") and why.endswith("."), (name, why)
    for aid in ("arm", "clear_latch"):
        ok, why = a.check(Action(aid, aid, {}, False))
        assert not ok
    refused = [e for e in a.audit_log(0) if e["event"] == "refused"]
    assert len(refused) == len(cases) + 2                                  # every refusal audited
    assert a.verify_chain()["ok"]


def test_invariants_and_template_use_real_names(seq):
    text = " ".join(seq["invariants"])
    for name in ("R04 jump guard", "R09 net delta", "R13 drawdown", "0x15", "0x12", "SW9", "KEY0", "80-99"):
        assert name in text
    assert "max_notional" not in text and "max_orders_per_s" not in text
    t = seq["template"]
    assert set(t) >= {"proposal", "post_mortem"} and "bad_frames_1m" in t["data_gaps"]["null_always"]


# ---------------------------------------------------------------- 2. JSON safety
def test_every_return_value_is_json_safe(seq):
    vals = [seq[k] for k in ("obs0", "inject", "state", "incidents", "actions", "invariants", "template", "audit",
                             "inject_stale", "apply_kill", "apply_cfg_noapprover", "apply_cfg_alice", "apply_hw",
                             "apply_demo", "apply_arm", "apply_clear")]
    vals += list(seq["sim"].values()) + [list(c) for c in seq["checks"].values()]
    for v in vals:
        json.dumps(to_json(v), allow_nan=False)


# ---------------------------------------------------------------- 3. seed guard
def test_engine_never_sees_seed_80_plus(seq):
    seen = seq["seen"]["scenario"] + seq["seen"]["evaluator"] + seq["adapter"].engine_seeds_dispatched
    assert seen and max(seen) <= 79
    assert set(range(0, 80)) <= set(seq["seen"]["scenario"])               # the 0-79 seeds really ran


def test_seed_80_requested_directly_is_refused(tmp_path):
    a = make(tmp_path, "g.jsonl")
    from latency_arena_adapter.app.adapters.trading import ERR
    with pytest.raises(ERR.SeedGuardError):
        a.b.replay(T.ACTOR, None, (79, 80))
    o = a.simulate(a.observe(), None, [80, 81, 99])                         # only out-of-sample seeds
    assert o.metrics["seeds_run"] == 0 and o.in_sample["evaluated"] is False


# ---------------------------------------------------------------- 4. audit chain
def test_audit_chain_verifies_and_detects_tampering(seq, tmp_path):
    from latency_arena_adapter.app.adapters.trading import LB
    AU = LB.sub("audit")
    a = seq["adapter"]
    assert a.verify_chain()["ok"]
    entries = seq["audit"]
    assert len(entries) == min(50, len(seq["audit_all"])) and entries == seq["audit_all"][-len(entries):]
    assert all(set(e) == {"t", "event", "data", "prev", "hash"} for e in entries)
    now = a.audit_log(0)
    assert a.audit_log(3) == now[-3:] and now[-1]["event"] == "inject"         # newest last
    assert all(entries[i]["prev"] == entries[i - 1]["hash"] for i in range(1, len(entries)))
    events = {e["event"] for e in seq["audit_all"]}
    assert {"boot", "inject", "refused", "applied", "kill_frame_sent", "config_frame_sent"} <= events
    recs = a.b.audit.read_all()
    for i in (0, len(recs) // 2, len(recs) - 1):
        bad = json.loads(json.dumps(recs))
        bad[i]["payload"] = {"forged": True}
        v = AU.verify_records(bad)
        assert not v["ok"] and v["first_bad_index"] == i
    b = make(tmp_path, "t.jsonl")
    b.inject("reset")
    lines = open(b.b.audit.path, encoding="ascii").read().splitlines()
    rec = json.loads(lines[1])
    rec["actor"] = "human:mallory"
    lines[1] = AU.canonical_json(rec)
    with open(b.b.audit.path, "w", encoding="ascii", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    v = b.verify_chain()
    assert not v["ok"] and v["first_bad_index"] == 1


# ---------------------------------------------------------------- 5. determinism
def test_same_state_action_seeds_give_identical_outcome(seq, tmp_path):
    seeds = [0, 1, 59, 60, 61, 79, 80, 99]
    outs = []
    for i in range(2):
        a = make(tmp_path, f"d{i}.jsonl")
        a.inject("gate_trip")
        st = a.observe()
        act = next(x for x in a.actions(a.detect(st)[0]) if x.id == "size_down")
        outs.append(json.dumps(to_json(a.simulate(st, act, seeds)), sort_keys=True))
        outs.append(json.dumps(to_json(a.simulate(st, act, seeds)), sort_keys=True))        # cached repeat
    assert len(set(outs)) == 1


# ---------------------------------------------------------------- 6. labels
def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)


def test_no_label_calls_twin_timing_fpga_latency(seq):
    vals = [to_json(seq[k]) for k in ("obs0", "state", "incidents", "actions", "invariants", "template", "audit_all",
                                      "apply_kill", "apply_cfg_alice")]
    vals += [to_json(o) for o in seq["sim"].values()]
    src = open(T.__file__, encoding="utf-8").read()
    hits = 0
    for s in list(_strings(vals)) + [src]:
        for m in re.finditer(r"fpga latency", s, re.I):
            hits += 1
            before = s[max(0, m.start() - 12):m.start()].lower()
            assert "not " in before or "never " in before, s[max(0, m.start() - 60):m.end()]
    assert hits > 0                                                         # the disclaimer is actually present
