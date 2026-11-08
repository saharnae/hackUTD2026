"""Append-only, hash-chained audit log (JSONL).

Record: {index, ts, actor, action, payload, prev_hash, hash}
  hash = SHA-256( prev_hash + canonical_json(record without "hash") ), lowercase hex.
  canonical_json = json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).
  GENESIS_HASH (64 zeros) is the prev_hash of record 0.

ASSUMPTIONS:
  AU1. ts is ISO-8601 UTC wall-clock metadata only; nothing in the backend's logic reads it.
       The clock is injectable so tests are deterministic.
  AU2. Payloads are redacted (backend/redaction.py) BEFORE hashing, so a secret never reaches disk.
  AU3. verify_chain() checks every record's index, prev_hash link and hash. Deleting or editing
       the LAST record cannot be detected from the file alone; an AuditLog instance remembers the
       head hash it wrote, and verify_chain() also compares against it. TODO(FINAL): anchor the head
       hash somewhere outside the file (e.g. print it on the dashboard) for cross-restart checks.
  AU4. One writer process; a lock makes appends atomic across threads.
"""

import datetime
import hashlib
import json
import os
import threading

from .redaction import redact

GENESIS_HASH = "0" * 64
FIELDS = ("index", "ts", "actor", "action", "payload", "prev_hash", "hash")


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def record_hash(rec_without_hash):
    return hashlib.sha256((rec_without_hash["prev_hash"] + canonical_json(rec_without_hash)).encode("ascii")).hexdigest()


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _finite(x):
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        return repr(x)                                     # NaN / inf are not JSON: stored as text
    if isinstance(x, dict):
        return {str(k): _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return str(x)


def _json_safe(x):
    """Round-trip through JSON so the hashed form is exactly what is stored."""
    return json.loads(canonical_json(_finite(x)))


class AuditLog:
    def __init__(self, path, clock=None):
        self.path = path
        self.clock = clock or _utc_now
        self._lock = threading.Lock()
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        recs = self.read_all() if os.path.exists(path) else []
        self._next = len(recs)
        self._head = recs[-1]["hash"] if recs else GENESIS_HASH
        self._anchor = self._head                          # AU3

    # ------------------------------------------------------------ write
    def append(self, actor, action, payload=None):
        with self._lock:
            rec = {"index": self._next, "ts": self.clock(), "actor": redact(str(actor)),
                   "action": redact(str(action)), "payload": _json_safe(redact(payload if payload is not None else {})),
                   "prev_hash": self._head}
            rec["hash"] = record_hash(rec)
            with open(self.path, "a", encoding="ascii", newline="\n") as f:
                f.write(canonical_json(rec) + "\n")
            self._next += 1
            self._head = self._anchor = rec["hash"]
            return dict(rec)

    # ------------------------------------------------------------ read
    def read_all(self):
        out = []
        if not os.path.exists(self.path):
            return out
        with open(self.path, encoding="ascii") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def tail(self, limit=50):
        recs = self.read_all()
        return recs[-limit:] if limit else recs

    @property
    def head_hash(self):
        return self._head

    # ------------------------------------------------------------ verify
    def verify_chain(self):
        return verify_records(self._safe_read(), expected_head=self._anchor)

    def _safe_read(self):
        try:
            return self.read_all()
        except (ValueError, UnicodeDecodeError) as e:
            return e


def verify_records(recs, expected_head=None):
    """-> {"ok", "count", "head_hash", "first_bad_index", "reason"}."""
    if isinstance(recs, Exception):
        return {"ok": False, "count": 0, "head_hash": None, "first_bad_index": 0,
                "reason": f"unparseable log line: {type(recs).__name__}"}
    prev = GENESIS_HASH
    for i, r in enumerate(recs):
        bad = None
        if not isinstance(r, dict) or set(r) != set(FIELDS):
            bad = "record fields changed"
        elif r["index"] != i:
            bad = f"index {r['index']} at position {i} (record removed, inserted or reordered)"
        elif r["prev_hash"] != prev:
            bad = "prev_hash does not link to the previous record"
        else:
            body = {k: r[k] for k in FIELDS if k != "hash"}
            if record_hash(body) != r["hash"]:
                bad = "hash does not match the record contents"
        if bad:
            return {"ok": False, "count": len(recs), "head_hash": None, "first_bad_index": i, "reason": bad}
        prev = r["hash"]
    if expected_head is not None and prev != expected_head:
        return {"ok": False, "count": len(recs), "head_hash": prev, "first_bad_index": len(recs),
                "reason": "head hash differs from the last hash this process wrote (tail removed or replaced)"}
    return {"ok": True, "count": len(recs), "head_hash": prev, "first_bad_index": None, "reason": None}
