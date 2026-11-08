"""Disaster-pack agents (SIMULATED). Deterministic heuristics, no LLM. They see ONLY the agent view:
world.registry(seed) (static attributes) and the pack state (reports, stated severity, queue,
crews). They never import or call world._truth (tested). Their output is proposals; the pack's
care rules decide (C6).

TriageAgent        advisory scores for the queue (band 2 ordering only).
OverlookedAuditor  finds households likely to need help but silent: compares each zone's report
                   rate with what its residents' age bands, device dependence, mobility and language
                   predict (rates pooled over all zones) and with its neighbours, then lists the
                   unreported households with the highest observable risk in the suspicious zones.
Commander          a scripted human stand-in for metric runs: accepts every pending proposal (the pack
                   re-checks the care rules on accept). In the demo a named human does it explicitly.

ASSUMPTIONS (DA, all TODO(FINAL)):
  DA1. Observable risk proxy = 3*device + 2*(80+) + 0.5*(65-79) + 1.5*mobility + 1*(language 2).
  DA2. Triage score = 10*stated severity + risk. A commander-accepted welfare check (no report) is a
       suspected urgent need: 25 + risk, above stated-high reports, still below the C1 floor (band 0).
  DA3. Zone suspicion = max(0, 1 - observed/expected report ratio relative to the neighbours'
       median ratio); zones with suspicion >= 0.3 are flagged. Candidate score =
       risk * (1 + 4*suspicion of its zone).
  DA4. Auditor budget: 150 welfare checks per run, in rounds of 75 at 2 h and 4 h after the storm, proposed
       as life-safety checks (pack DP4: band 0 only once a human accepts).
"""

from . import world as W

RISK_W = {"device_dependent": 3.0, "age80": 2.0, "age65": 0.5, "mobility_limited": 1.5, "lang2": 1.0}   # DA1
FLAG_SUSPICION = 0.3          # DA3  TODO(FINAL)
AUDIT_ROUNDS_H = (2, 4)       # DA4  TODO(FINAL)
AUDIT_BUDGET_PER_ROUND = 75   # DA4  TODO(FINAL)


def risk(reg, i):
    return (RISK_W["device_dependent"] * reg["device_dependent"][i] + RISK_W["age80"] * (reg["age_band"][i] == 2)
            + RISK_W["age65"] * (reg["age_band"][i] == 1) + RISK_W["mobility_limited"] * reg["mobility_limited"][i]
            + RISK_W["lang2"] * (reg["language"][i] == 2))


class TriageAgent:
    actor = "supervisor:triage"

    def propose(self, state, reg):
        hh = state["hh"]
        scores = {}
        for i in hh["queue"]:
            st = hh["stated"][i]
            scores[str(i)] = round((25.0 if st is None else 10.0 * st) + risk(reg, i), 4)   # DA2
        return {"kind": "propose", "actor": self.actor, "proposal": {"type": "scores", "scores": scores}}


class OverlookedAuditor:
    actor = "supervisor:overlooked_auditor"

    def zone_report(self, state, reg):
        """Per zone: observed reports, expected from pooled cell rates, ratio, suspicion."""
        hh = state["hh"]
        cell = lambda i: (reg["age_band"][i], reg["device_dependent"][i], reg["mobility_limited"][i],  # noqa: E731
                          reg["language"][i])
        n_c, r_c = {}, {}
        for i in range(W.N_HOUSEHOLDS):
            k = cell(i)
            n_c[k] = n_c.get(k, 0) + 1
            r_c[k] = r_c.get(k, 0) + (hh["rep_t"][i] is not None)
        rate = {k: r_c[k] / n_c[k] for k in n_c}
        obs, exp = [0] * W.N_ZONES, [0.0] * W.N_ZONES
        for i in range(W.N_HOUSEHOLDS):
            z = reg["zone"][i]
            obs[z] += hh["rep_t"][i] is not None
            exp[z] += rate[cell(i)]
        ratio = [obs[z] / exp[z] if exp[z] > 0 else 1.0 for z in range(W.N_ZONES)]
        out = []
        for z in range(W.N_ZONES):
            nb = sorted(ratio[x] for x in W.neighbours(z))
            med = nb[len(nb) // 2] if nb else 1.0
            susp = max(0.0, 1.0 - ratio[z] / med) if med > 0 else 0.0
            out.append({"zone": z, "observed": obs[z], "expected": round(exp[z], 2), "ratio": round(ratio[z], 4),
                        "neighbour_median_ratio": round(med, 4), "suspicion": round(susp, 4),
                        "flagged": susp >= FLAG_SUSPICION})
        return out

    def rank(self, state, reg, budget):
        zr = self.zone_report(state, reg)
        susp = [z["suspicion"] if z["flagged"] else 0.0 for z in zr]
        hh = state["hh"]
        cands = [i for i in range(W.N_HOUSEHOLDS) if hh["rep_t"][i] is None and hh["queued_t"][i] is None
                 and hh["crew"][i] is None]
        cands.sort(key=lambda i: (-(risk(reg, i) * (1 + 4 * susp[reg["zone"][i]])), i))
        return cands[:budget], zr

    def propose(self, state, reg, budget=AUDIT_BUDGET_PER_ROUND):
        hs, zr = self.rank(state, reg, budget)
        if not hs:
            return None
        return {"kind": "propose", "actor": self.actor,
                "proposal": {"type": "welfare_checks", "households": hs, "urgency": "life_safety_check",
                             "evidence": [z for z in zr if z["flagged"]]}}


class Commander:
    """Scripted human stand-in for metric runs (accepts every pending proposal; rules re-checked)."""

    def __init__(self, name="commander"):
        self.actor = f"human:{name}"

    def decide(self, state):
        return [{"kind": "accept_proposal", "actor": self.actor, "id": p["id"]}
                for p in state["proposals"] if p["status"] == "pending"]
