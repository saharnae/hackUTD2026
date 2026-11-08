"""Proposal store (in memory; every change is also in the audit log).

Lifecycle: draft (agent) -> pending_human (compared on held-out seeds, written up) -> accepted | denied (human).
A proposal holds field values only. The 0x12 frame bytes are built only in Backend.accept().
"""

import threading


class ProposalStore:
    def __init__(self):
        self._items, self._n, self._lock = {}, 0, threading.Lock()

    def create(self, **fields):
        with self._lock:
            self._n += 1
            pid = f"P{self._n:04d}"
            self._items[pid] = dict(fields, id=pid, status="draft", comparison=None, write_up=None,
                                    decided_by=None, decision_reason=None, board_result=None)
            return dict(self._items[pid])

    def get(self, pid):
        p = self._items.get(pid)
        return dict(p) if p else None

    def update(self, pid, **fields):
        with self._lock:
            self._items[pid].update(fields)
            return dict(self._items[pid])

    def list(self, status=None):
        return [dict(p) for p in self._items.values() if status is None or p["status"] == status]
