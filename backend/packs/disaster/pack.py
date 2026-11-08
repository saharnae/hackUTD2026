"""Disaster-response pack (SIMULATED): the same Pack interface as P3 (backend/packs/base.py):
initial_state(seed), step(state, dt_s, commands), limits(state), cost(command). Pure and
deterministic per seed. Everything is synthetic and labeled SIMULATED.

People-centred story: households report needs after a storm, crews make first contact, and the
care rules make sure nobody with a life-safety need is pushed down, nobody waits forever, and the
agent can only ever propose.

Care rules (enforced here, each tested):
  C1 life-safety floor  A household KNOWN to have a life-safety need (its report states critical,
                        or a commander confirmed it) is always in band 0, ahead of everyone else.
                        No proposal can move it below that floor (refused, audited).
  C2 starvation timer   Anyone waiting longer than STARVATION_S is bumped into band 1 (ahead of all
                        ordinary ranking), oldest first.
  C3 no double booking  One crew per household, one household per crew.
  C4 capacity           A crew works one job at a time and starts no new job once it has worked
                        CREW_BUSY_CAP_S in the horizon (the job in hand may finish past the cap);
                        at most ZONE_CREW_CAP crews work in one zone at the same time.
  C5 heartbeat fallback No agent heartbeat for HB_TIMEOUT_S (or a kill) -> the rule queue: FCFS with
                        C1-C4 enforced; agent scores are ignored and new agent proposals refused.
  C6 one-way authority  Agents ("supervisor:<name>") may only propose and send heartbeats. Only a
                        human ("human:<name>") can start the storm clock, accept or deny a proposal.
                        A proposal that would break C1-C4 is refused, even if a human accepts it.
  C7 audit              Every decision is an event in state["events"]; DisasterSupervisor
                        (supervisor.py) writes each one to P3's hash-chained backend.audit.AuditLog.

Queue order: band 0 (C1, by queue time), band 1 (C2, by queue time), band 2: by queue time in the
rule queue (C5) and baseline; by the agent's advisory score when the agent is live.
Baseline = plain first-come-first-served (C3/C4 only: they are physical limits), no agent.

ASSUMPTIONS (DP, all TODO(FINAL)):
  DP1. Time: 60-second ticks; horizon 24 h after the storm starts.
  DP2. C1 binds on KNOWN life-safety status (stated critical in the report, or commander-confirmed).
       The rules cannot see hidden true need either; only the metrics do. A life-safety household
       that never reports is protected by the overlooked auditor, not by C1.
  DP3. Agent "scores" proposals are advisory ordering inside band 2 and apply at once while the agent
       is live (they cannot touch bands 0-1). Priority changes, welfare checks and crew assignments
       wait for a human (commander) to accept them.
  DP4. Welfare checks: an accepted check puts an unreported household in the queue (as of the accept
       time) so a crew visits it. A visit is the first contact. A list proposed with
       "urgency": "life_safety_check" makes the accepted households commander-confirmed critical (C1,
       band 0): the human accepts the auditor's precision trade-off; the agent cannot do it alone.
  DP5. Overlooked = a top-10%-true-need household with no first contact more than OVERLOOK_AFTER_S
       after the storm started. Time to first contact is censored at "now" for anyone not reached.
  DP6. limits() channel names and numbers are placeholders.
"""

import functools
import json

from ...authority import parse_actor           # same actor convention as P3 ("supervisor:x" / "human:x")
from . import world as W

LABEL = "SIMULATED disaster response (synthetic households, crews and reports; not real data)"
TICK_S = 60                       # DP1  TODO(FINAL)
HORIZON_S = 24 * 3600             # DP1  TODO(FINAL)
STARVATION_S = 8 * 3600           # C2   TODO(FINAL)
ZONE_CREW_CAP = 5                 # C4   TODO(FINAL)
CREW_BUSY_CAP_S = 16 * 3600       # C4   TODO(FINAL)
HB_TIMEOUT_S = 300                # C5   TODO(FINAL)
OVERLOOK_AFTER_S = 6 * 3600       # DP5  TODO(FINAL)
PRIORITIES = ("critical", "routine")


@functools.lru_cache(maxsize=16)
def _reports_at(seed):
    """elapsed seconds -> households whose (hidden) report arrives then. World physics, not agent input."""
    out = {}
    for i, r in enumerate(W._truth(seed)["report_after_s"]):
        if r is not None:
            out.setdefault(r, []).append(i)
    return out


def _ev(st, actor, action, result, rule=None, reason=None, **kw):
    e = {"t_s": st["t_s"], "actor": actor, "action": action, "result": result, "rule": rule, "reason": reason}
    e.update(kw)
    st["events"].append(e)
    return e


class DisasterPack:
    name = "disaster"
    forecast_step_s = 600.0
    what_if_horizon_s = 2 * 3600.0

    def __init__(self, policy="supervised"):
        if policy not in ("supervised", "baseline"):
            raise ValueError("policy must be 'supervised' or 'baseline'")
        self.policy = policy

    # ------------------------------------------------------------ Pack interface
    def initial_state(self, seed):
        if not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative int")
        n = W.N_HOUSEHOLDS
        return self._derive({
            "pack": self.name, "label": LABEL, "seed": seed, "policy": self.policy, "t_s": 0, "storm_t": None,
            "horizon_s": HORIZON_S, "mode": "pre_storm",
            "agent": {"link": self.policy == "supervised", "hb_t": None},
            "hh": {"rep_t": [None] * n, "stated": [None] * n, "queued_t": [None] * n, "src": [None] * n,
                   "contact_t": [None] * n, "crew": [None] * n, "confirmed_critical": [], "queue": []},
            "crews": [{"hh": None, "start": None, "until": None, "busy_done": 0} for _ in range(W.N_CREWS)],
            "agent_scores": {}, "proposals": [], "next_proposal_id": 1,
            "events": [], "counters": {"refused": 0, "accepted": 0, "pending": 0, "c2_bumped": 0,
                                       "welfare_queued": 0, "dispatched": 0}})

    def step(self, state, dt_s, commands=()):
        if dt_s < 0:
            raise ValueError("dt_s must be >= 0")
        st = json.loads(json.dumps(state))                 # PK2: never mutate the input
        st["events"] = []
        for cmd in commands or []:
            self._command(st, cmd)
        t_end = st["t_s"] + dt_s
        if st["storm_t"] is not None:
            k = (st["t_s"] - st["storm_t"]) // TICK_S + 1   # next tick strictly after now
            tau = st["storm_t"] + k * TICK_S
            while tau <= t_end and tau - st["storm_t"] <= st["horizon_s"]:
                self._tick(st, tau)
                tau += TICK_S
        st["t_s"] = t_end
        return self._derive(st)

    def limits(self, state=None):
        return {                                                          # DP6  TODO(FINAL)
            "overlooked_high_need": {"warn": 10, "high": 25, "trip": 50},
            "p90_ttfc_top10_min": {"warn": 120, "high": 240, "trip": 360},
            "max_wait_min": {"warn": 0.5 * STARVATION_S / 60, "high": 0.8 * STARVATION_S / 60,
                             "trip": STARVATION_S / 60},
            "agent_heartbeat_age_s": {"warn": 0.5 * HB_TIMEOUT_S, "high": 0.8 * HB_TIMEOUT_S, "trip": HB_TIMEOUT_S},
            "crew_idle_frac": {"warn": 0.3, "high": 0.5, "trip": 0.8},
        }

    def cost(self, command):
        kind = command.get("kind")
        p = command.get("proposal") or {}
        if kind == "propose" and p.get("type") == "welfare_checks":
            reg = W.registry(command.get("seed", 0))
            mins = sum(W.travel_s(reg["zone"][h]) / 60 + 25 for h in p.get("households", []))   # 25 min avg on-site
            return {"dollars": None, "minutes": round(mins, 1),
                    "notes": "crew-minutes for the welfare visits (travel + an assumed 25 min on site, SIMULATED)"}
        return {"dollars": None, "minutes": None, "notes": f"no cost model for {kind!r} (SIMULATED)"}

    # ------------------------------------------------------------ commands (C6)
    def _command(self, st, cmd):
        kind = cmd.get("kind")
        actor = cmd.get("actor", "")
        role, _ = parse_actor(actor)
        human_only = {"shock", "accept_proposal", "deny_proposal"}
        agent_only = {"propose", "agent_heartbeat"}
        if role is None:
            return self._refuse(st, actor, kind, "C6", f"unknown actor {actor!r}")
        if kind in human_only and role != "human":
            return self._refuse(st, actor, kind, "C6", f"{kind} is a human decision; agents may only propose")
        if kind in agent_only and role != "supervisor":
            return self._refuse(st, actor, kind, "C6", f"{kind} is the agent channel")
        if kind == "shock":
            if st["storm_t"] is None:
                st["storm_t"] = st["t_s"]
                st["mode"] = "baseline" if self.policy == "baseline" else "rule_queue"
                _ev(st, actor, "shock", "accepted", reason="storm starts (shock button)")
            return None
        if kind == "kill":
            if st["agent"]["link"]:
                st["agent"]["link"] = False
                _ev(st, actor, "kill", "accepted", "C5", "agent link cut: rule queue (FCFS + C1-C4) in force")
            return None
        if kind == "agent_heartbeat":
            if not st["agent"]["link"]:
                return self._refuse(st, actor, kind, "C5", "agent link was killed")
            st["agent"]["hb_t"] = st["t_s"]
            return None
        if kind == "propose":
            return self._propose(st, actor, cmd.get("proposal") or {})
        if kind in ("accept_proposal", "deny_proposal"):
            pr = next((p for p in st["proposals"] if p["id"] == cmd.get("id") and p["status"] == "pending"), None)
            if pr is None:
                return self._refuse(st, actor, kind, None, f"no pending proposal {cmd.get('id')!r}")
            if kind == "deny_proposal":
                pr["status"] = "denied"
                _ev(st, actor, kind, "accepted", proposal_id=pr["id"])
                return None
            prob = self._check(st, pr["type"], pr["payload"], for_accept=True)
            if prob:
                pr["status"] = "refused"
                return self._refuse(st, actor, kind, prob[0], prob[1], proposal_id=pr["id"])
            self._apply(st, pr["type"], pr["payload"])
            pr["status"] = "accepted"
            st["counters"]["accepted"] += 1
            _ev(st, actor, kind, "accepted", proposal_id=pr["id"], proposal_type=pr["type"],
                by_agent=pr["actor"])
            return None
        return self._refuse(st, actor, kind, "C6", f"unknown command kind {kind!r}")

    def _refuse(self, st, actor, action, rule, reason, **kw):
        st["counters"]["refused"] += 1
        _ev(st, actor, action, "refused", rule, reason, **kw)
        return None

    def _agent_live(self, st):
        a = st["agent"]
        return (self.policy == "supervised" and a["link"] and a["hb_t"] is not None
                and st["t_s"] - a["hb_t"] <= HB_TIMEOUT_S)

    def _propose(self, st, actor, p):
        ptype = p.get("type")
        if not self._agent_live(st):
            return self._refuse(st, actor, f"propose:{ptype}", "C5",
                                "agent offline (no heartbeat, killed, or baseline): rule queue in force")
        payload = {k: v for k, v in p.items() if k != "type"}
        prob = self._check(st, ptype, payload)
        if prob:
            return self._refuse(st, actor, f"propose:{ptype}", prob[0], prob[1])
        if ptype == "scores":                                              # DP3: advisory, band 2 only
            self._apply(st, ptype, payload)
            _ev(st, actor, "propose:scores", "accepted", reason="advisory ranking inside band 2",
                n=len(payload["scores"]))
            return None
        pr = {"id": st["next_proposal_id"], "actor": actor, "type": ptype, "payload": payload,
              "t_s": st["t_s"], "status": "pending"}
        st["next_proposal_id"] += 1
        st["proposals"].append(pr)
        st["counters"]["pending"] += 1
        _ev(st, actor, f"propose:{ptype}", "pending", reason="waiting for a human commander", proposal_id=pr["id"])
        return None

    # ------------------------------------------------------------ care-rule checks
    def _known_critical(self, st, i):
        return st["hh"]["stated"][i] == 3 or i in st["hh"]["confirmed_critical"]

    def _check(self, st, ptype, payload, for_accept=False):
        """-> None if allowed, else (rule, reason). Never consults hidden truth."""
        hh, n = st["hh"], W.N_HOUSEHOLDS
        if ptype == "scores":
            sc = payload.get("scores")
            if not isinstance(sc, dict):
                return ("C6", "scores must be a mapping household -> number")
            for k, v in sc.items():
                if not (str(k).isdigit() and int(k) < n) or not isinstance(v, (int, float)):
                    return ("C6", f"bad score entry {k!r}")
            return None
        if ptype == "set_priority":
            i, pri = payload.get("household"), payload.get("priority")
            if not isinstance(i, int) or not 0 <= i < n or pri not in PRIORITIES:
                return ("C6", "set_priority needs household index and priority critical|routine")
            if pri != "critical" and self._known_critical(st, i):
                return ("C1", f"household {i} has a known life-safety need; it cannot be ranked below the floor")
            return None
        if ptype == "welfare_checks":
            hs = payload.get("households")
            if not isinstance(hs, list) or not hs or any(not isinstance(i, int) or not 0 <= i < n for i in hs):
                return ("C6", "welfare_checks needs a non-empty list of household indices")
            if len(set(hs)) != len(hs):
                return ("C3", "a household appears twice in one welfare-check list")
            if any(hh["crew"][i] is not None for i in hs):
                return ("C3", "a listed household already has a crew")
            if payload.get("urgency") not in (None, "life_safety_check"):
                return ("C6", "urgency must be absent or 'life_safety_check'")
            return None
        if ptype == "assign":
            c, i = payload.get("crew"), payload.get("household")
            if not (isinstance(c, int) and 0 <= c < W.N_CREWS and isinstance(i, int) and 0 <= i < n):
                return ("C6", "assign needs crew and household indices")
            crew = st["crews"][c]
            if crew["hh"] is not None:
                return ("C3", f"crew {c} already has household {crew['hh']}")
            if hh["crew"][i] is not None:
                return ("C3", f"household {i} already has crew {hh['crew'][i]}")
            if hh["queued_t"][i] is None:
                return ("C6", f"household {i} is not in the queue")
            if self._busy(crew, st["t_s"]) >= CREW_BUSY_CAP_S:
                return ("C4", f"crew {c} reached its working-time cap")
            z = W.registry(st["seed"])["zone"][i]
            if self._zone_load(st)[z] >= ZONE_CREW_CAP:
                return ("C4", f"zone {z} already has {ZONE_CREW_CAP} crews")
            order = self._order(st, st["t_s"], self._agent_live(st))
            head_band = order[0][0] if order else 2
            if head_band < 2 and self._band(st, i, st["t_s"]) > head_band:
                return ("C1" if head_band == 0 else "C2",
                        "assignment would jump ahead of a household the care rules put first")
            return None
        return ("C6", f"unknown proposal type {ptype!r}")

    def _apply(self, st, ptype, payload):
        hh = st["hh"]
        if ptype == "scores":
            st["agent_scores"].update({str(k): float(v) for k, v in payload["scores"].items()})
        elif ptype == "set_priority":
            i = payload["household"]
            if payload["priority"] == "critical" and i not in hh["confirmed_critical"]:
                hh["confirmed_critical"].append(i)
        elif ptype == "welfare_checks":
            for i in payload["households"]:
                if payload.get("urgency") == "life_safety_check" and i not in hh["confirmed_critical"]:
                    hh["confirmed_critical"].append(i)
                if hh["queued_t"][i] is None:
                    hh["queued_t"][i], hh["src"][i] = st["t_s"], "welfare"
                    hh["queue"].append(i)
                    st["counters"]["welfare_queued"] += 1
        elif ptype == "assign":
            self._dispatch(st, payload["crew"], payload["household"], st["t_s"])

    # ------------------------------------------------------------ simulation
    @staticmethod
    def _busy(crew, t):
        b = crew["busy_done"]
        if crew["hh"] is not None:
            b += max(0, min(t, crew["until"]) - crew["start"])
        return b

    def _zone_load(self, st):
        zone = W.registry(st["seed"])["zone"]
        load = [0] * W.N_ZONES
        for c in st["crews"]:
            if c["hh"] is not None:
                load[zone[c["hh"]]] += 1
        return load

    def _band(self, st, i, tau):
        if self.policy == "baseline":
            return 2
        if self._known_critical(st, i):
            return 0                                                       # C1
        if tau - st["hh"]["queued_t"][i] >= STARVATION_S:
            return 1                                                       # C2
        return 2

    def _order(self, st, tau, live):
        hh = st["hh"]
        q = hh["queue"]
        sc = st["agent_scores"]
        rows = []
        for i in q:
            b = self._band(st, i, tau)
            s = -sc.get(str(i), 0.0) if (live and b == 2) else 0.0
            rows.append((b, s, hh["queued_t"][i], i))
        rows.sort()
        return rows

    def _dispatch(self, st, c, i, tau):
        crew, z = st["crews"][c], W.registry(st["seed"])["zone"][i]
        truth = W._truth(st["seed"])
        travel, onsite = W.travel_s(z), truth["onsite_s"][i]              # on-site time is world physics
        crew.update(hh=i, start=tau, until=tau + travel + onsite)
        st["hh"]["crew"][i] = c
        st["hh"]["contact_t"][i] = tau + travel
        st["hh"]["queue"].remove(i)
        st["counters"]["dispatched"] += 1

    def _tick(self, st, tau):
        hh, truth = st["hh"], W._truth(st["seed"])
        for crew in st["crews"]:                                           # release finished jobs
            if crew["hh"] is not None and crew["until"] <= tau:
                crew["busy_done"] += crew["until"] - crew["start"]
                crew.update(hh=None, start=None, until=None)
        el = tau - st["storm_t"]
        for i in _reports_at(st["seed"]).get(el, ()):                     # reports arrive (the world)
            hh["rep_t"][i], hh["stated"][i] = tau, truth["stated_tier"][i]
            if hh["queued_t"][i] is None:
                hh["queued_t"][i], hh["src"][i] = tau, "report"
                hh["queue"].append(i)
        live = self._agent_live(dict(st, t_s=tau))
        mode = "baseline" if self.policy == "baseline" else ("agent" if live else "rule_queue")
        if mode != st["mode"]:
            if st["mode"] == "agent" and mode == "rule_queue":
                _ev(dict(st, t_s=tau), "rule_engine", "fallback", "engaged", "C5",
                    "agent heartbeat missing or link killed: FCFS with C1-C4")
            st["mode"] = mode
        free = [c for c, crew in enumerate(st["crews"])
                if crew["hh"] is None and self._busy(crew, tau) < CREW_BUSY_CAP_S]   # C3 / C4 crew limit
        if not free:
            return
        order = self._order(st, tau, live)
        if not order:
            return
        bumped = sum(1 for b, *_ in order if b == 1)
        st["counters"]["c2_bumped"] = max(st["counters"]["c2_bumped"], bumped)
        load = self._zone_load(st)
        zone = W.registry(st["seed"])["zone"]
        k = 0
        for c in free:
            while k < len(order) and load[zone[order[k][3]]] >= ZONE_CREW_CAP:   # C4 zone cap
                k += 1
            if k >= len(order):
                break
            i = order[k][3]
            self._dispatch(st, c, i, tau)
            load[zone[i]] += 1
            k += 1

    # ------------------------------------------------------------ metrics (the only place truth is read)
    def _derive(self, st):
        t, s0 = st["t_s"], st["storm_t"]
        hh = st["hh"]
        q = hh["queue"]
        a = st["agent"]
        hb_age = (t - a["hb_t"]) if (a["hb_t"] is not None and a["link"]) else None
        if s0 is None:
            st["channels"] = {"overlooked_high_need": None, "p90_ttfc_top10_min": None, "max_wait_min": None,
                              "agent_heartbeat_age_s": hb_age, "crew_idle_frac": None}
            st["summary"] = {"label": LABEL, "storm_started": False, "queued": 0}
            return st
        truth = W._truth(st["seed"])
        el = min(t - s0, st["horizon_s"])
        now = s0 + el
        top = truth["top"]
        ttfc = []
        for i in top:
            ct = hh["contact_t"][i]
            ttfc.append(((ct - s0) if ct is not None and ct <= now else el) / 60.0)
        ttfc_sorted = sorted(ttfc)
        p90 = ttfc_sorted[min(len(ttfc_sorted) - 1, int(0.9 * len(ttfc_sorted)))] if ttfc_sorted else None
        reached = lambda i: hh["contact_t"][i] is not None and hh["contact_t"][i] <= now   # noqa: E731
        overlooked = [i for i in top if not reached(i)] if el > OVERLOOK_AFTER_S else []
        silent = set(truth["silent"])
        busy = sum(self._busy(c, now) for c in st["crews"])
        util = busy / (W.N_CREWS * el) if el > 0 else 0.0
        st["channels"] = {
            "overlooked_high_need": len(overlooked),
            "p90_ttfc_top10_min": round(p90, 3),
            "max_wait_min": round(max(((now - hh["queued_t"][i]) / 60.0 for i in q), default=0.0), 3),
            "agent_heartbeat_age_s": hb_age,
            "crew_idle_frac": round(1.0 - util, 6),
        }
        st["summary"] = {
            "label": LABEL, "storm_started": True, "elapsed_h": round(el / 3600, 4), "mode": st["mode"],
            "queued": len(q), "first_contacts": sum(1 for i in range(W.N_HOUSEHOLDS) if reached(i)),
            "top10_reached": sum(1 for i in top if reached(i)), "top10_total": len(top),
            "mean_ttfc_top10_min": round(sum(ttfc) / len(ttfc), 3),
            "p90_ttfc_top10_min": round(p90, 3),
            "overlooked_high_need": len(overlooked),
            "overlooked_silent": sum(1 for i in overlooked if i in silent),
            "silent_reached": sum(1 for i in silent if reached(i)), "silent_total": len(silent),
            "crew_utilization": round(util, 6),
            "c2_max_bumped": st["counters"]["c2_bumped"], "welfare_queued": st["counters"]["welfare_queued"],
        }
        return st
