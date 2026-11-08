"""Pack interface and the domain-free Engine.

The interface signatures are the ones given in the task (no file defining them existed in the repo):
  Pack:   initial_state(seed) -> State ; step(state, dt_s, commands) -> State (deterministic per seed) ;
          limits(state) -> channel -> {warn, high, trip} ; cost(command) -> dollars and minutes
  Engine: forecast(state, horizon_s, commands=[]) ; what_if(state, change) ; plan_actions(state, goal)

Conventions (needed by a domain-free engine; stated here, nothing else is assumed):
  PK1. State is a JSON-safe dict. state["channels"] maps channel name -> current number (or None).
       Every channel is "higher is worse"; limits(state)[ch] = {"warn", "high", "trip"}, warn <= high <= trip.
  PK2. A command is a JSON-safe dict with a "kind" key. step() must be pure: same state, dt and
       commands -> the same new state. It never mutates its input.
  PK3. cost(command) -> {"dollars": float | None, "minutes": float | None, "notes": str}. None = not known.
  PK4. Optional pack hooks the Engine uses when present:
         what_if(state, change) -> dict | None      (None = use the generic forecast comparison)
         candidate_actions(state, goal) -> [command]
         evaluate_action(state, command | None, goal) -> {"ok": bool, "score": float, "metrics": dict}
         forecast_step_s: float                     (step size the forecast uses)
The Engine knows nothing about options, disasters or any other domain.
"""

from typing import Any, Dict, List, Optional, Protocol

State = Dict[str, Any]
Command = Dict[str, Any]


class Pack(Protocol):
    name: str

    def initial_state(self, seed: int) -> State: ...
    def step(self, state: State, dt_s: float, commands: List[Command]) -> State: ...
    def limits(self, state: State) -> Dict[str, Dict[str, float]]: ...
    def cost(self, command: Command) -> Dict[str, Any]: ...


LEVELS = ("warn", "high", "trip")


def level_of(value, lim):
    """Highest level reached by value, or None."""
    if value is None:
        return None
    hit = None
    for lv in LEVELS:
        if lim.get(lv) is not None and value >= lim[lv]:
            hit = lv
    return hit


class Engine:
    def __init__(self, pack: Pack):
        self.pack = pack

    # ------------------------------------------------------------ forecast
    def forecast(self, state: State, horizon_s: float, commands: Optional[List[Command]] = None) -> Dict[str, Any]:
        """Step the pack forward horizon_s from state (commands applied on the first step only).
        Returns points, first crossing time of each limit level, and the final channels. Pure."""
        if horizon_s <= 0:
            raise ValueError("horizon_s must be > 0")
        step = getattr(self.pack, "forecast_step_s", None) or horizon_s / 10.0
        limits = self.pack.limits(state)
        s, t, first, points = state, 0.0, True, []
        while t < horizon_s - 1e-12:
            dt = min(step, horizon_s - t)
            s = self.pack.step(s, dt, list(commands or []) if first else [])
            first = False
            t += dt
            points.append({"t_s": round(t, 9), "channels": dict(s.get("channels", {})),
                           "summary": s.get("summary")})                 # opaque to the engine
        crossings = []
        for ch, lim in sorted(limits.items()):
            for lv in LEVELS:
                for p in points:
                    v = p["channels"].get(ch)
                    if v is not None and lim.get(lv) is not None and v >= lim[lv]:
                        crossings.append({"channel": ch, "level": lv, "t_s": p["t_s"], "value": v, "limit": lim[lv]})
                        break
        return {"horizon_s": horizon_s, "points": points, "crossings": crossings,
                "final": dict(s.get("channels", {})), "final_state": s}

    # ------------------------------------------------------------ what-if
    def what_if(self, state: State, change: Command) -> Dict[str, Any]:
        hook = getattr(self.pack, "what_if", None)
        res = hook(state, change) if hook else None
        if res is None:                                           # generic: forecast with vs without
            h = getattr(self.pack, "what_if_horizon_s", None) or 1.0
            base = self.forecast(state, h)
            alt = self.forecast(state, h, [change])
            res = {"method": "forecast comparison", "horizon_s": h,
                   "baseline": base["final"], "with_change": alt["final"],
                   "baseline_crossings": base["crossings"], "with_change_crossings": alt["crossings"]}
        return {"change": change, "result": res}

    # ------------------------------------------------------------ planning
    def plan_actions(self, state: State, goal: Dict[str, Any]) -> Dict[str, Any]:
        """Candidates from the pack, each evaluated by the pack, filtered (ok) and ranked by score."""
        cands = self.pack.candidate_actions(state, goal)
        base = self.pack.evaluate_action(state, None, goal)
        kept, dropped = [], []
        for c in cands:
            ev = self.pack.evaluate_action(state, c, goal)
            row = {"action": c, "evaluation": ev, "improves": bool(ev["ok"] and ev["score"] > base["score"])}
            (kept if ev["ok"] else dropped).append(row)
        kept.sort(key=lambda r: -r["evaluation"]["score"])                # stable: ties keep pack order
        n = int(goal.get("max_candidates", 3))
        return {"goal": goal, "baseline": base, "candidates": kept[:n], "filtered_out": dropped}
