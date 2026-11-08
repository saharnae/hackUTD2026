"""Backend: one object that owns the pack, the domain-free engine, the data source, the audit log,
the authority and the proposals. The API and every agent tool go through these methods, and every
method calls authorize() first.
"""

from collections import deque

from . import _paths  # noqa: F401
from . import config as C
from .audit import AuditLog
from .authority import Authority, parse_actor
from .datasource import make_source
from .errors import AuthorityError, NotFound, SeedGuardError
from .packs import Engine
from .packs.options import OptionsPack
from .packs.options import configmap as CM
from .packs.options import frames_io as FIO
from .packs.options import replay as RP
from .proposals import ProposalStore
from .redaction import redact
import frames as F


class StateError(Exception):
    """Request is valid but the object is in the wrong state (HTTP 409)."""


class Backend:
    def __init__(self, audit_path=None, seed=None, regime=None, use_serial=False, workers=1,
                 held_out_seeds=None, stress_regimes=None, clock=None, tuning_result=None):
        self.audit = AuditLog(audit_path or C.AUDIT_LOG_PATH, clock)
        self.authority = Authority(self.audit)
        self.held_out = tuple(held_out_seeds or C.HELD_OUT_SEEDS)
        RP.guard_seeds(RP.seeds_from_range(*self.held_out), held_out=True)
        self.pack = OptionsPack(regime, RP.Replayer(workers), tuning_result, stress_regimes, self.held_out)
        self.engine = Engine(self.pack)
        self.source = make_source(self.pack, seed, use_serial)
        self.decoder = F.FrameDecoder()
        self.last_status = None
        self.log_buffer = deque(maxlen=5000)
        self.proposals = ProposalStore()
        self.reports = []
        st = self.source.state or {}
        self.audit.append("system", "boot", {"data_source": self.source.kind, "label": self.source.label,
                                             "seed": st.get("seed"), "regime": st.get("regime"),
                                             "held_out_seeds": list(self.held_out)})

    # ------------------------------------------------------------ helpers
    def authorize(self, actor, action, payload=None):
        return self.authority.authorize(actor, action, payload)

    def _seed_guarded(self, actor, action, fn):
        try:
            return fn()
        except SeedGuardError as e:
            self.audit.append(actor, "refused", {"requested": action, "reason": e.reason, "kind": "seed_guard",
                                                 "payload": e.detail})
            raise

    def _state(self):
        if self.source.state is None:
            raise StateError("this needs the software twin (the serial source has no simulated state)")
        return self.source.state

    def _poll(self):
        st, logs, _ = FIO.decode_stream(self.decoder, self.source.read_frames())
        if st:
            self.last_status = st[-1]
        self.log_buffer.extend(logs)
        return logs

    def observation(self, new_log=None):
        s = self.source.state
        if s is not None:
            channels, summary = s["channels"], s["summary"]
        else:                                                          # serial: only what the frames carry
            ls = self.last_status or {}
            channels = {"loss_dollars": max(0.0, -ls["equity_dollars"]) if ls else None,
                        "tokens_used": 20 - ls["tokens"] if ls else None}
            summary = None
        return {"source": self.source.label, "status": self.last_status, "channels": channels,
                "limits": self.pack.limits(s), "summary": summary,
                "new_log_records": len(new_log) if new_log is not None else None,
                "unmodeled_status_fields": list(_unmodeled()) if self.source.kind == "twin" else []}

    # ------------------------------------------------------------ reads
    def status(self, actor):
        self.authorize(actor, "read_status")
        return self.observation(self._poll())

    def log(self, actor, limit=50):
        self.authorize(actor, "read_log", {"limit": limit})
        self._poll()
        limit = max(1, min(int(limit), 1000))
        return {"source": self.source.label, "records": list(self.log_buffer)[-limit:]}

    def audit_records(self, actor, limit=200):
        self.authorize(actor, "read_audit", {"limit": limit})
        return self.audit.tail(limit)

    def verify_audit(self):
        return self.audit.verify_chain()

    # ------------------------------------------------------------ time
    def advance(self, actor, dt_s):
        """Twin time passes (supervisor 'simulate' / observe step)."""
        self.authorize(actor, "simulate", {"dt_s": dt_s})
        self.source.advance(float(dt_s))
        return self.observation(self._poll())

    def sim_step(self, actor, dt_s=0.0, new_session=False, seed=None, regime=None):
        """Human API: advance the live twin, or power-cycle it (TW6)."""
        self.authorize(actor, "sim_step", {"dt_s": dt_s, "new_session": new_session, "seed": seed, "regime": regime})
        if new_session:
            self.authorize(actor, "twin_new_session", {"seed": seed, "regime": regime})
            self._seed_guarded(actor, "twin_new_session", lambda: self.source.new_session(seed, regime))
            self.decoder = F.FrameDecoder()
            self.log_buffer.clear()
        if dt_s:
            self.source.advance(float(dt_s))
        return self.observation(self._poll())

    # ------------------------------------------------------------ simulate
    def forecast(self, actor, horizon_s=None, commands=None):
        h = float(horizon_s or C.FORECAST_HORIZON_S)
        self.authorize(actor, "forecast", {"horizon_s": h, "commands": commands})
        f = self.engine.forecast(self._state(), h, commands or [])
        f.pop("final_state", None)
        f["label"] = self.pack.forecast_label if hasattr(self.pack, "forecast_label") else \
            "SIMULATED twin forecast: replays the same seeded scenario (perfect foresight inside the twin)"
        return f

    def what_if(self, actor, change):
        self.authorize(actor, "what_if", {"change": change})
        if not isinstance(change, dict) or change.get("kind") not in ("jump", "config", "kill"):
            raise ValueError("change.kind must be 'jump', 'config' or 'kill'")
        if change.get("kind") == "config":
            self._check_params(actor, "what_if", change.get("params"))
        return self._seed_guarded(actor, "what_if", lambda: self.engine.what_if(self._state(), change))

    def replay(self, actor, params=None, seeds=None, regimes=None):
        lo, hi = seeds if seeds else self.held_out
        self.authorize(actor, "replay", {"params": params, "seeds": [lo, hi], "regimes": regimes})
        seed_list = self._seed_guarded(actor, "replay", lambda: RP.guard_seeds(RP.seeds_from_range(lo, hi)))
        base = self.pack.current_params(self._state())
        if params:
            self._check_params(actor, "replay", params)
            p = self.pack.apply_changes(base, params)
        else:
            p = base
        regimes = list(regimes or self.pack.stress_regimes)
        return self._seed_guarded(actor, "replay", lambda: self.pack.replayer.summary(p, seed_list, regimes))

    def plan(self, actor, goal=None):
        goal = dict(goal or {})
        goal.setdefault("seeds", RP.seeds_from_range(*self.held_out))
        self.authorize(actor, "plan_actions", {"goal": {k: v for k, v in goal.items() if k != "seeds"},
                                               "seeds": [min(goal["seeds"]), max(goal["seeds"])]})
        return self._seed_guarded(actor, "plan_actions", lambda: self.engine.plan_actions(self._state(), goal))

    def _check_params(self, actor, action, params):
        cur = {k: self._state()["session_params"][k] for k in CM.TUNABLE_PARAMS}
        probs = CM.check_change(params) or CM.merged_problems(cur, params)
        if probs:
            kinds = sorted({p["kind"] for p in probs})
            self.authority.refuse(actor, action, "; ".join(p["reason"] for p in probs), {"params": params},
                                  "hardware_limit" if "hardware_limit" in kinds else kinds[0])

    # ------------------------------------------------------------ proposals (agent side)
    def current_tunables(self):
        return {k: self._state()["session_params"][k] for k in CM.TUNABLE_PARAMS}

    def propose(self, actor, changes, rationale="", incident=None):
        cur = self.current_tunables()
        rationale = redact(str(rationale or ""))
        self.authorize(actor, "propose_config", {"params": changes, "current": cur, "rationale": rationale})
        full = self.pack.apply_changes(dict(cur, hedge_band_shares=self.pack.hedge_band_shares), changes)
        tun = {k: full[k] for k in CM.TUNABLE_PARAMS}
        p = self.proposals.create(created_by=actor, changes={k: v for k, v in tun.items() if v != cur[k]},
                                  requested=changes, params=tun, current=cur, fields=CM.to_fields(tun),
                                  rationale=rationale, incident=incident, frame="0x12 (draft: no frame built)")
        self.audit.append(actor, "proposal_drafted", {"proposal_id": p["id"], "changes": p["changes"],
                                                      "fields": p["fields"]})
        return p

    def compare_proposal(self, actor, pid, regimes=None):
        p = self._proposal(pid)
        change = {"kind": "config", "params": p["changes"] or p["requested"],
                  "seeds": RP.seeds_from_range(*self.held_out), "regimes": list(regimes or self.pack.stress_regimes)}
        res = self.what_if(actor, change)["result"]
        cmp = {"label": res["label"], "seeds": res["baseline"]["seeds"], "regimes": res["baseline"]["regimes"],
               "baseline_score": res["baseline"]["score"], "proposal_score": res["with_change"]["score"],
               "baseline": res["baseline"]["per_regime"], "proposal": res["with_change"]["per_regime"],
               "delta": res["delta"], "unexpected_trips": res["with_change"]["unexpected_trips"],
               "determinism": [res["baseline"]["determinism_hash"], res["with_change"]["determinism_hash"]]}
        self.proposals.update(pid, comparison=cmp)
        self.audit.append(actor, "proposal_compared", {"proposal_id": pid, "seeds": cmp["seeds"],
                                                       "baseline_score": cmp["baseline_score"],
                                                       "proposal_score": cmp["proposal_score"],
                                                       "determinism": cmp["determinism"]})
        return cmp

    def finalize_proposal(self, actor, pid, write_up):
        p = self._proposal(pid)
        if p["status"] != "draft":
            raise StateError(f"proposal {pid} is {p['status']}, not draft")
        if p["comparison"] is None:
            raise StateError(f"proposal {pid} has no held-out comparison yet")
        self.authorize(actor, "propose_config", {"params": p["requested"], "current": p["current"],
                                                 "proposal_id": pid, "step": "write_up"})
        p = self.proposals.update(pid, status="pending_human", write_up=redact(write_up))
        self.audit.append(actor, "proposal_ready", {"proposal_id": pid, "status": "pending_human"})
        return p

    def _proposal(self, pid):
        p = self.proposals.get(pid)
        if p is None:
            raise NotFound(f"no proposal {pid}")
        return p

    # ------------------------------------------------------------ kill
    def request_kill(self, actor, reason=""):
        self.authorize(actor, "request_kill", {"reason": reason})
        return self._send_kill(actor, reason)

    def human_kill(self, operator, reason=""):
        actor = f"human:{operator}"
        self.authorize(actor, "kill", {"reason": reason})
        return self._send_kill(actor, reason)

    def _send_kill(self, actor, reason):
        cost = self.pack.cost(self.pack.kill_command(self.source.state)) if self.source.state else None
        frame = F.encode(F.T_KILL, b"")
        res = self.source.send_frame(frame)
        self.audit.append(actor, "kill_frame_sent", {"frame": "0x15", "hex": frame.hex(), "result": res,
                                                     "reason": reason, "cost_estimate": cost})
        self._poll()
        return {"result": res, "cost_estimate": cost, "observation": self.observation()}

    # ------------------------------------------------------------ human decisions
    def accept(self, approver, pid):
        """The ONLY path that builds and sends a 0x12 config frame."""
        actor = f"human:{(approver or '').strip()}"
        self.authorize(actor, "accept_proposal", {"proposal_id": pid})
        p = self._proposal(pid)
        if p["status"] != "pending_human":
            raise StateError(f"proposal {pid} is {p['status']}; only pending_human proposals can be accepted")
        payload = CM.config_payload(p["params"])
        frame = F.encode(F.T_CONFIG, payload)
        res = self.source.send_frame(frame)
        p = self.proposals.update(pid, status="accepted", decided_by=actor, board_result=res, frame_hex=frame.hex())
        self.audit.append(actor, "config_frame_sent", {"proposal_id": pid, "frame": "0x12", "hex": frame.hex(),
                                                       "fields": p["fields"], "result": res})
        self._poll()
        return p

    def deny(self, approver, pid, reason=""):
        actor = f"human:{(approver or '').strip()}"
        self.authorize(actor, "deny_proposal", {"proposal_id": pid, "reason": reason})
        p = self._proposal(pid)
        if p["status"] not in ("draft", "pending_human"):
            raise StateError(f"proposal {pid} is already {p['status']}")
        p = self.proposals.update(pid, status="denied", decided_by=actor, decision_reason=reason)
        self.audit.append(actor, "proposal_denied", {"proposal_id": pid, "reason": reason})
        return p

    def add_report(self, actor, report):
        report = redact(report)
        self.reports.append(report)
        self.audit.append(actor, "report_written", {"report_id": report["id"], "incident": report.get("incident_id"),
                                                    "chain_ok": report["audit_chain"]["ok"]})
        return report


def _unmodeled():
    from .packs.options import twin as TW
    return TW.UNMODELED_STATUS


__all__ = ["Backend", "StateError", "AuthorityError", "SeedGuardError", "NotFound", "parse_actor"]
