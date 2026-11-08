"""Shared contract between the agent core and every scenario adapter.

Copy this file verbatim into the trading backend and the disaster adapter.
The agent core imports ONLY this file, never domain code (contract rule 1).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional, Protocol

IN_SAMPLE = list(range(0, 80))       # tune here
OUT_OF_SAMPLE = list(range(80, 100))  # report here only after the evaluator has run


@dataclass
class Incident:
    id: str                # stable id so agents and humans can refer to it
    kind: str              # "gate_trip", "stale_feed", "starved_case", ...
    severity: int          # 1 low, 2 high, 3 critical
    detail: dict           # scenario-specific facts (JSON-safe values only)
    at: float              # scenario clock, seconds


@dataclass
class Action:
    id: str
    label: str             # plain words shown to the human
    params: dict
    reduces_risk: bool     # True = only makes things safer (e.g. kill 0x15)


@dataclass
class Outcome:
    metrics: dict                  # per-seed or summary numbers the proposal compares
    in_sample: dict                # aggregated over seeds 0-79
    out_of_sample: dict            # aggregated over seeds 80-99; {"evaluated": False} until run
    source: str = "SIMULATED"      # e.g. "SIMULATED (twin)"; never "FPGA timing" unless it is


@dataclass
class ApplyResult:
    ok: bool
    reason: str
    detail: dict = field(default_factory=dict)   # e.g. {"frame": "0x15"}


class ScenarioAdapter(Protocol):
    name: str

    def observe(self) -> dict: ...
    def detect(self, state: dict) -> list[Incident]: ...
    def actions(self, incident: Incident) -> list[Action]: ...
    def simulate(self, state: dict, action: Optional[Action], seeds: list[int]) -> Outcome: ...
    def invariants(self) -> list[str]: ...
    def check(self, action: Action) -> tuple[bool, str]: ...
    def apply(self, action: Action, approved_by: Optional[str]) -> ApplyResult: ...
    def report_template(self) -> dict: ...
    def audit_log(self, limit: int = 50) -> list[dict]: ...   # hash-chained entries, newest last


def to_json(obj):
    """Dataclasses -> plain dicts so results can go to Nemotron and the UI."""
    if isinstance(obj, list):
        return [to_json(o) for o in obj]
    if isinstance(obj, tuple):
        return [to_json(o) for o in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return obj
