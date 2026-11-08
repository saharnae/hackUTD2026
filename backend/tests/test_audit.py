"""Audit log: hash chain, tamper detection on every record and every field, API verify."""

import hashlib
import json

import pytest

from backend.audit import FIELDS, GENESIS_HASH, AuditLog, canonical_json, record_hash, verify_records
from .conftest import fixed_clock

N = 6


def _log(path):
    a = AuditLog(path, fixed_clock())
    for i in range(N):
        a.append(f"supervisor:t{i}", f"act{i}", {"i": i, "nested": {"x": [i, i + 1]}, "s": "text"})
    return a


def _lines(path):
    with open(path, encoding="ascii") as f:
        return [json.loads(x) for x in f if x.strip()]


def test_chain_formula_and_genesis(audit_path):
    a = _log(audit_path)
    recs = a.read_all()
    assert recs[0]["prev_hash"] == GENESIS_HASH == "0" * 64
    for i, r in enumerate(recs):
        assert set(r) == set(FIELDS) and r["index"] == i
        body = {k: r[k] for k in FIELDS if k != "hash"}
        assert r["hash"] == hashlib.sha256((r["prev_hash"] + canonical_json(body)).encode()).hexdigest()
        if i:
            assert r["prev_hash"] == recs[i - 1]["hash"]
    assert a.verify_chain()["ok"] and a.verify_chain()["count"] == N


TAMPERS = {
    "index": lambda r: r.update(index=r["index"] + 100),
    "ts": lambda r: r.update(ts="2030-01-01T00:00:00+00:00"),
    "actor": lambda r: r.update(actor="human:mallory"),
    "action": lambda r: r.update(action="config_frame_sent"),
    "payload": lambda r: r["payload"]["nested"]["x"].append(99),
    "payload_key": lambda r: r["payload"].update(extra=1),
    "prev_hash": lambda r: r.update(prev_hash="f" * 64),
    "hash": lambda r: r.update(hash="0" * 63 + "1"),
    "drop_field": lambda r: r.pop("ts"),
}


@pytest.mark.parametrize("field", sorted(TAMPERS))
@pytest.mark.parametrize("i", range(N))
def test_tampering_any_record_is_detected(audit_path, i, field):
    _log(audit_path)
    recs = _lines(audit_path)
    TAMPERS[field](recs[i])
    v = verify_records(recs)
    assert not v["ok"] and v["first_bad_index"] == i, v


@pytest.mark.parametrize("i", range(N - 1))
def test_deleting_a_record_is_detected(audit_path, i):
    _log(audit_path)
    recs = _lines(audit_path)
    del recs[i]
    v = verify_records(recs)
    assert not v["ok"] and v["first_bad_index"] == i


def test_reorder_and_insert_detected(audit_path):
    _log(audit_path)
    recs = _lines(audit_path)
    swapped = recs[:2] + [recs[3], recs[2]] + recs[4:]
    assert not verify_records(swapped)["ok"]
    forged = dict(recs[2], actor="human:mallory")
    assert not verify_records(recs[:3] + [forged] + recs[3:])["ok"]


def test_full_rewrite_and_tail_truncation_detected_by_head_anchor(audit_path):
    a = _log(audit_path)
    recs = _lines(audit_path)
    # attacker edits record 2 and recomputes every later hash: the file alone looks valid ...
    recs[2]["payload"] = {"i": "forged"}
    prev = recs[1]["hash"]
    for r in recs[2:]:
        r["prev_hash"] = prev
        r["hash"] = record_hash({k: r[k] for k in FIELDS if k != "hash"})
        prev = r["hash"]
    with open(audit_path, "w", encoding="ascii", newline="\n") as f:
        f.write("".join(canonical_json(r) + "\n" for r in recs))
    assert verify_records(recs)["ok"]
    # ... but the log instance remembers the head it wrote (AU3)
    assert not a.verify_chain()["ok"]
    # tail truncation
    p2 = audit_path + ".2"
    b = _log(p2)
    kept = _lines(p2)[:-1]
    with open(p2, "w", encoding="ascii", newline="\n") as f:
        f.write("".join(canonical_json(r) + "\n" for r in kept))
    assert verify_records(kept)["ok"]                    # a shorter valid chain ...
    v = b.verify_chain()
    assert not v["ok"] and "head hash" in v["reason"]    # ... is caught by the anchor


def test_garbage_line_detected(audit_path):
    a = _log(audit_path)
    with open(audit_path, "a", encoding="ascii") as f:
        f.write("{not json\n")
    assert not a.verify_chain()["ok"]


def test_reopen_continues_chain(audit_path):
    _log(audit_path)
    b = AuditLog(audit_path, fixed_clock())
    r = b.append("system", "again", {})
    assert r["index"] == N and b.verify_chain()["ok"]


def test_api_audit_verify_detects_tamper(backend, client_for):
    c = client_for(backend)
    c.get("/status")
    code, body, _ = c.get("/audit/verify")
    assert code == 200 and body["ok"] and body["count"] >= 2
    recs = _lines(backend.audit.path)
    recs[1]["actor"] = "human:mallory"
    with open(backend.audit.path, "w", encoding="ascii", newline="\n") as f:
        f.write("".join(canonical_json(r) + "\n" for r in recs))
    code, body, _ = c.get("/audit/verify")
    assert code == 200 and not body["ok"] and body["first_bad_index"] == 1
