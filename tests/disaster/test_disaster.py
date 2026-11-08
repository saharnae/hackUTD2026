"""Tests for backend/packs/disaster (SIMULATED, synthetic data). Each care rule C1-C7 is tested on its own."""

import ast
import io
import json
import os
import sys
from contextlib import redirect_stdout

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from backend.audit import AuditLog  # noqa: E402
from backend.packs.base import Engine  # noqa: E402
from backend.packs.disaster import agents as A  # noqa: E402
from backend.packs.disaster import demo as D  # noqa: E402
from backend.packs.disaster import pack as P  # noqa: E402
from backend.packs.disaster import supervisor as S  # noqa: E402
from backend.packs.disaster import world as W  # noqa: E402

HUMAN, AGENT = "human:commander", "supervisor:triage"
HB = {"kind": "agent_heartbeat", "actor": AGENT}


@pytest.fixture
def audit(tmp_path):
    return AuditLog(str(tmp_path / "audit.jsonl"), clock=lambda: "2026-11-08T00:00:00+00:00")


def started(seed=0, hours=2.0, policy="supervised", agents=True):
    pk = P.DisasterPack(policy)
    st = pk.step(pk.initial_state(seed), 0, [{"kind": "shock", "actor": HUMAN}])
    reg, tri = W.registry(seed), A.TriageAgent()
    while st["t_s"] - st["storm_t"] < hours * 3600:
        cmds = [HB, tri.propose(st, reg)] if agents and policy == "supervised" else []
        st = pk.step(pk.step(st, 0, cmds), 300, [])
    return pk, st


def last(st, action=None):
    evs = [e for e in st["events"] if action is None or e["action"] == action]
    return evs[-1]


# ---------------------------------------------------------------- interface and purity
def test_existing_pack_interface_and_domain_free_engine():
    pk = P.DisasterPack()
    for m in ("initial_state", "step", "limits", "cost"):
        assert callable(getattr(pk, m))
    st = pk.step(pk.initial_state(1), 0, [{"kind": "shock", "actor": HUMAN}])
    before = json.dumps(st, sort_keys=True)
    fc = Engine(pk).forecast(st, 3600)                                # P3's domain-free engine, unchanged
    assert json.dumps(st, sort_keys=True) == before                   # step() never mutates its input
    assert fc["points"] and set(fc["final"]) == set(pk.limits(st))
    for lim in pk.limits(st).values():
        assert lim["warn"] <= lim["high"] <= lim["trip"]
    c = pk.cost({"kind": "propose", "proposal": {"type": "welfare_checks", "households": [1, 2]}})
    assert c["minutes"] > 0 and c["dollars"] is None and "SIMULATED" in c["notes"]
    assert st["label"].startswith("SIMULATED")


def test_hidden_truth_never_reaches_agents():
    _, st = started(0, 1.0)
    blob = json.dumps(st)
    for k in ('"tier"', '"silent"', '"dark_zones"', '"severity"', '"report_after_s"', '"onsite_s"'):
        assert k not in blob
    tree = ast.parse(open(A.__file__, encoding="utf-8").read())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
        {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "_truth" not in names and "_generate" not in names
    assert set(W.registry(0)) == {"zone", "age_band", "device_dependent", "mobility_limited", "language"}


# ---------------------------------------------------------------- determinism
def test_determinism_per_seed(audit, tmp_path):
    a = S.run(3, "supervised", audit, horizon_s=4 * 3600)
    b = S.run(3, "supervised", AuditLog(str(tmp_path / "b.jsonl"), clock=lambda: "x"), horizon_s=4 * 3600)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    c = S.run(4, "supervised", AuditLog(str(tmp_path / "c.jsonl"), clock=lambda: "x"), horizon_s=4 * 3600)
    assert a["summary"] != c["summary"]
    assert W.registry(3) == W.registry(3) and W._truth(3) == W._truth(3)
    t = W._truth(0)
    assert len(t["top"]) == W.N_HOUSEHOLDS // 10 and len(t["silent"]) == round(W.SILENT_SHARE * len(t["top"]))
    assert all(t["report_after_s"][i] is None for i in t["silent"])  # silent households never report


# ---------------------------------------------------------------- C1 life-safety floor
def test_c1_downgrade_refused_and_floor_holds():
    pk, st = started(0, 2.0)
    crit = [i for i in st["hh"]["queue"] if st["hh"]["stated"][i] == 3]
    assert crit
    st2 = pk.step(st, 0, [HB, {"kind": "propose", "actor": AGENT,
                               "proposal": {"type": "set_priority", "household": crit[0], "priority": "routine"}}])
    e = last(st2, "propose:set_priority")
    assert e["result"] == "refused" and e["rule"] == "C1"
    st3 = pk.step(st2, 0, [HB, {"kind": "propose", "actor": AGENT,             # even a huge negative score
                                "proposal": {"type": "scores", "scores": {str(crit[0]): -1e9}}}])
    order = pk._order(st3, st3["t_s"], True)
    bands = [b for b, *_ in order]
    assert bands == sorted(bands)                                            # band 0 strictly first
    pos = {r[3]: k for k, r in enumerate(order)}
    n0 = bands.count(0)
    assert all(pos[i] < n0 for i in crit)


def test_c1_dispatch_never_skips_a_known_critical():
    pk, st = started(0, 3.0)
    reg = W.registry(0)
    for _ in range(12):
        prev = st
        st = pk.step(st, 60, [HB])
        newly = [i for i in range(W.N_HOUSEHOLDS) if prev["hh"]["crew"][i] is None and st["hh"]["crew"][i] is not None]
        waiting_crit = [i for i in prev["hh"]["queue"] if pk._known_critical(prev, i)]
        served_noncrit = [i for i in newly if not pk._known_critical(prev, i)]
        if served_noncrit and waiting_crit:
            # only allowed when every waiting critical was blocked by the C4 zone cap
            load = pk._zone_load(st)
            assert all(load[reg["zone"][i]] >= P.ZONE_CREW_CAP or st["hh"]["crew"][i] is not None for i in waiting_crit)


# ---------------------------------------------------------------- C2 starvation
def test_c2_starvation_bumps_ahead_of_scores():
    pk, st = started(0, 2.0)
    q = [i for i in st["hh"]["queue"] if not pk._known_critical(st, i)]
    old, rest = q[0], q[1:4]
    st = json.loads(json.dumps(st))
    st["hh"]["queued_t"][old] = st["t_s"] - P.STARVATION_S - 60
    st["agent_scores"].update({str(old): -100.0, **{str(i): 100.0 for i in rest}})
    order = pk._order(st, st["t_s"], True)
    pos = {r[3]: k for k, r in enumerate(order)}
    assert pk._band(st, old, st["t_s"]) == 1
    assert all(pos[old] < pos[i] for i in rest)
    st["hh"]["queued_t"][old] = st["t_s"] - P.STARVATION_S + 60             # just under the limit: not bumped
    assert pk._band(st, old, st["t_s"]) == 2


# ---------------------------------------------------------------- C3 no double assignment
def test_c3_proposals_refused():
    pk, st = started(0, 2.0)
    busy = next(c for c, cr in enumerate(st["crews"]) if cr["hh"] is not None)
    q = st["hh"]["queue"][0]
    st = pk.step(st, 0, [HB, {"kind": "propose", "actor": AGENT, "proposal": {"type": "assign", "crew": busy, "household": q}}])
    assert last(st, "propose:assign")["rule"] == "C3"
    served = next(i for i in range(W.N_HOUSEHOLDS) if st["hh"]["crew"][i] is not None)
    for prop in ({"type": "welfare_checks", "households": [served]}, {"type": "welfare_checks", "households": [q, q]}):
        st = pk.step(st, 0, [HB, {"kind": "propose", "actor": AGENT, "proposal": prop}])
        assert last(st, "propose:welfare_checks")["result"] == "refused" and last(st, "propose:welfare_checks")["rule"] == "C3"


def _invariants(st):
    crews = [c["hh"] for c in st["crews"] if c["hh"] is not None]
    assert len(crews) == len(set(crews))                                    # one household per crew
    for c, cr in enumerate(st["crews"]):
        if cr["hh"] is not None:
            assert st["hh"]["crew"][cr["hh"]] == c                          # one crew per household
    assigned = [i for i in range(W.N_HOUSEHOLDS) if st["hh"]["crew"][i] is not None]
    assert len(assigned) == len(set(assigned))
    assert not set(assigned) & set(st["hh"]["queue"])
    zone = W.registry(st["seed"])["zone"]
    load = [0] * W.N_ZONES
    for h in crews:
        load[zone[h]] += 1
    assert max(load) <= P.ZONE_CREW_CAP                                     # C4 zone cap
    assert all(P.DisasterPack._busy(cr, st["t_s"]) <= P.CREW_BUSY_CAP_S + 3600 for cr in st["crews"])


def test_c3_c4_invariants_hold_through_a_run():
    pk = P.DisasterPack()
    st = pk.step(pk.initial_state(2), 0, [{"kind": "shock", "actor": HUMAN}])
    reg, tri = W.registry(2), A.TriageAgent()
    for k in range(0, 24 * 12, 3):                                          # every 15 min, 24 h
        st = pk.step(pk.step(st, 0, [HB, tri.propose(st, reg)]), 900 if k % 2 else 300, [])
        if st["mode"] != "agent":
            st = pk.step(st, 0, [HB])
        _invariants(st)
    assert all(P.DisasterPack._busy(cr, st["t_s"]) <= P.CREW_BUSY_CAP_S + 3600 for cr in st["crews"])


# ---------------------------------------------------------------- C4 capacity
def test_c4_zone_and_crew_caps_refuse_assignments():
    pk, st = started(0, 2.0)
    st = json.loads(json.dumps(st))
    zone = W.registry(0)["zone"]
    i = next(h for h in st["hh"]["queue"] if pk._known_critical(st, h)) if any(
        pk._known_critical(st, h) for h in st["hh"]["queue"]) else st["hh"]["queue"][0]
    free = [c for c, cr in enumerate(st["crews"]) if cr["hh"] is None]
    if len(free) < 1:
        st["crews"][0].update(hh=None, start=None, until=None)
        free = [0]
    c = free[0]
    # crew cap
    st["crews"][c]["busy_done"] = P.CREW_BUSY_CAP_S
    s2 = pk.step(st, 0, [HB, {"kind": "propose", "actor": AGENT, "proposal": {"type": "assign", "crew": c, "household": i}}])
    assert last(s2, "propose:assign")["rule"] == "C4"
    st["crews"][c]["busy_done"] = 0
    # zone cap: fill the household's zone with other crews
    z = zone[i]
    others = [h for h in range(W.N_HOUSEHOLDS) if zone[h] == z and st["hh"]["crew"][h] is None and h != i
              and h not in st["hh"]["queue"]]
    for cr in st["crews"]:
        if cr["hh"] is not None and zone[cr["hh"]] == z:
            st["hh"]["crew"][cr["hh"]] = None
            cr.update(hh=None, start=None, until=None)
    for k, cc in enumerate([x for x in range(W.N_CREWS) if x != c][:P.ZONE_CREW_CAP]):
        h = others[k]
        st["crews"][cc].update(hh=h, start=st["t_s"], until=st["t_s"] + 3600)
        st["hh"]["crew"][h] = cc
    s3 = pk.step(st, 0, [HB, {"kind": "propose", "actor": AGENT, "proposal": {"type": "assign", "crew": c, "household": i}}])
    assert last(s3, "propose:assign")["rule"] == "C4"


# ---------------------------------------------------------------- C5 heartbeat fallback
def test_c5_missing_heartbeat_falls_back_to_rule_queue():
    pk, st = started(0, 2.0)
    assert st["mode"] == "agent"
    st = pk.step(st, P.HB_TIMEOUT_S + 120, [])                              # no heartbeat
    assert st["mode"] == "rule_queue"
    assert any(e["action"] == "fallback" and e["rule"] == "C5" for e in st["events"])
    st2 = pk.step(st, 0, [{"kind": "propose", "actor": AGENT, "proposal": {"type": "scores", "scores": {"1": 5}}}])
    assert last(st2)["result"] == "refused" and last(st2)["rule"] == "C5"
    # the rule queue is FCFS inside each band: agent scores are ignored
    order = pk._order(st2, st2["t_s"], False)
    for b in (0, 1, 2):
        ts = [r[2] for r in order if r[0] == b]
        assert ts == sorted(ts)


def test_c5_kill_cuts_the_agent_for_good():
    pk, st = started(0, 1.0)
    st = pk.step(st, 0, [{"kind": "kill", "actor": HUMAN}])
    st = pk.step(st, 0, [HB])
    assert last(st)["result"] == "refused" and last(st)["rule"] == "C5"
    st = pk.step(st, 600, [])
    assert st["mode"] == "rule_queue" and not st["agent"]["link"]


# ---------------------------------------------------------------- C6 one-way authority
def test_c6_roles():
    pk, st = started(0, 1.0)
    st = pk.step(st, 0, [HB, {"kind": "propose", "actor": "supervisor:auditor",
                              "proposal": {"type": "welfare_checks", "households": [st["hh"]["queue"][0]]}}])
    pid = st["proposals"][-1]["id"]
    for cmd in ({"kind": "accept_proposal", "actor": AGENT, "id": pid}, {"kind": "deny_proposal", "actor": AGENT, "id": pid},
                {"kind": "shock", "actor": AGENT}, {"kind": "propose", "actor": HUMAN, "proposal": {"type": "scores", "scores": {}}},
                {"kind": "agent_heartbeat", "actor": HUMAN}, {"kind": "propose", "actor": "someone", "proposal": {}}):
        s2 = pk.step(st, 0, [cmd])
        assert last(s2)["result"] == "refused" and last(s2)["rule"] == "C6", cmd
    assert st["proposals"][-1]["status"] == "pending"
    s3 = pk.step(st, 0, [{"kind": "accept_proposal", "actor": HUMAN, "id": pid}])
    assert last(s3)["result"] == "accepted"


def test_c6_human_accept_cannot_override_a_care_rule():
    pk, st = started(0, 2.0)
    q = st["hh"]["queue"][0]
    st = pk.step(st, 0, [HB, {"kind": "propose", "actor": AGENT, "proposal": {"type": "welfare_checks", "households": [q]}}])
    pid = st["proposals"][-1]["id"]
    st = json.loads(json.dumps(st))
    c = next(c for c, cr in enumerate(st["crews"]) if cr["hh"] is None) if any(
        cr["hh"] is None for cr in st["crews"]) else 0
    st["crews"][c].update(hh=q, start=st["t_s"], until=st["t_s"] + 600)    # the household got a crew meanwhile
    st["hh"]["crew"][q] = c
    st["hh"]["queue"].remove(q)
    s2 = pk.step(st, 0, [{"kind": "accept_proposal", "actor": HUMAN, "id": pid}])
    assert last(s2)["result"] == "refused" and last(s2)["rule"] == "C3"


def test_c6_agent_cannot_accept_through_the_supervisor(audit):
    pk = P.DisasterPack()
    sup = S.DisasterSupervisor(pk, audit)
    st = sup.step(pk.initial_state(0), 0, [{"kind": "shock", "actor": HUMAN}])
    st = sup.step(st, 300, [HB])
    st = sup.step(st, 0, [HB, {"kind": "propose", "actor": AGENT,
                               "proposal": {"type": "welfare_checks", "households": [10]}}])
    pid = st["proposals"][-1]["id"]
    n = audit.verify_chain()["count"]
    st = sup.step(st, 0, [{"kind": "accept_proposal", "actor": AGENT, "id": pid}])
    recs = audit.read_all()[n:]
    assert recs and recs[0]["action"] == "refused" and recs[0]["payload"]["requested"] == "accept_proposal"
    assert st["proposals"][-1]["status"] == "pending"                        # P3 Authority stopped it first


# ---------------------------------------------------------------- C7 audit
def test_c7_hash_chained_audit(audit):
    S.run(1, "supervised", audit, horizon_s=3 * 3600)
    v = audit.verify_chain()
    assert v["ok"] and v["count"] > 10
    recs = audit.read_all()
    assert recs[0]["action"] == "disaster.shock" and recs[0]["prev_hash"] == "0" * 64
    lines = open(audit.path).read().splitlines()
    doc = json.loads(lines[1])
    doc["payload"]["result"] = "tampered"
    lines[1] = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    open(audit.path, "w").write("\n".join(lines) + "\n")
    bad = audit.verify_chain()
    assert not bad["ok"] and bad["first_bad_index"] == 1


# ---------------------------------------------------------------- overlooked auditor
def test_auditor_recall_on_silent_households():
    for seed in range(5):
        _, st = started(seed, 2.0, agents=False)
        reg, truth = W.registry(seed), W._truth(seed)                       # truth only for scoring the test
        silent = set(truth["silent"])
        budget = 2 * A.AUDIT_BUDGET_PER_ROUND
        picked, zr = A.OverlookedAuditor().rank(st, reg, budget)
        unreported = sum(1 for i in range(W.N_HOUSEHOLDS) if st["hh"]["rep_t"][i] is None)
        recall = len(silent & set(picked)) / len(silent)
        chance = budget / unreported                                         # expected recall of a random list
        assert recall >= 0.15 and recall >= 3 * chance, (seed, recall, chance)
        flagged = {z["zone"] for z in zr if z["flagged"]}
        assert set(truth["dark_zones"]) & flagged                           # the zone pattern is found


# ---------------------------------------------------------------- demo end to end
def test_demo_script_end_to_end(tmp_path):
    audit = AuditLog(str(tmp_path / "demo.jsonl"), clock=lambda: "t")
    r = D.run_demo(0, audit)
    d = r["downgrade"]
    assert d["event"]["result"] == "refused" and d["event"]["rule"] == "C1" and d["still_band_0"]
    assert d["audit_record"]["action"] == "refused" and d["audit_record"]["payload"]["rule"] == "C1"
    assert r["agent_self_accept"]["refused"] and r["agent_self_accept"]["audit"]
    assert r["kill"]["mode"] == "rule_queue" and r["kill"]["fallback_events"]
    assert r["kill"]["agent_proposal_after_kill"]["result"] == "refused"
    assert r["commander_accept"]["result"] == "accepted" and r["mode_at_end"] == "rule_queue"
    assert r["supervised"]["overlooked_high_need"] < r["baseline"]["overlooked_high_need"]
    assert r["supervised"]["mean_ttfc_top10_min"] < r["baseline"]["mean_ttfc_top10_min"]
    assert r["baseline"]["silent_reached"] == 0 < r["supervised"]["silent_reached"]
    assert r["audit"]["ok"]


def test_demo_cli_labels(tmp_path):
    out = io.StringIO()
    with redirect_stdout(out):
        assert D.main(["--seed", "1", "--audit-log", str(tmp_path / "cli.jsonl")]) == 0
    lines = out.getvalue().splitlines()
    assert lines and all(x.startswith("[SIMULATED] ") for x in lines)
    assert any("REFUSED by C1" in x for x in lines)
