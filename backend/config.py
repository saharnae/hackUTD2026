"""Backend configuration registry (SIMULATED arena). Every entry is TODO(FINAL): unconfirmed.

Universe and tuned values are NOT copied here: they are read from the universe block in
golden/golden.py, the single source of truth. This file only adds what golden.py does not own
(hardware pins, arming switch, agent tool names and model endpoint).

Hardware pins are PROPOSED placeholders only. They are not in any pin-assignment or Quartus file
and must not be wired from this file until confirmed.
No network access happens here: the Nemotron entries are unset strings/None, never called.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "golden"))
import golden as g  # noqa: E402

# ---------------------------------------------------------------- hardware (PROPOSED, all TODO(FINAL))
ACTIVE_LOW = "active-low"
PINS = {
    "shock_button": {"pin": "IO3", "polarity": ACTIVE_LOW},   # TODO(FINAL)
    "kill_button":  {"pin": "IO4", "polarity": ACTIVE_LOW},   # TODO(FINAL)
    "buzzer":       {"pin": "IO5", "polarity": ACTIVE_LOW},   # TODO(FINAL)
    "relay":        {"pin": "IO6", "polarity": ACTIVE_LOW},   # TODO(FINAL)
}
ARM_SWITCH = {"switch": "SW9", "edge": "rising", "when": "after boot"}   # TODO(FINAL)

# ---------------------------------------------------------------- universe (from golden.py, TODO(FINAL) there)
UNDERLYING_SYMBOL = g.UNDERLYING_SYMBOL                  # "SPY"  TODO(FINAL)
SPOT = g.SPOT                                            # $ 733.50  TODO(FINAL): confirm vs a live quote
STRIKES = g.STRIKES                                      # recentered per snapshot (A22)  TODO(FINAL)

# ---------------------------------------------------------------- tuned values (from golden.py)
EDGE_THRESHOLD_TICKS = g.EDGE_THRESHOLD_TICKS            # 1.5  TODO(FINAL)
EDGE_THRESHOLD_RANGE = g.EDGE_THRESHOLD_RANGE            # (1.0, 3.0)  TODO(FINAL)
MIN_MOVE_CENTS = g.MIN_MOVE_CENTS                        # 2, cents  TODO(FINAL)
MIN_MOVE_RANGE = g.MIN_MOVE_RANGE                        # (1, 5) cents  TODO(FINAL)
SIZE_CAP_BASE = g.SIZE_CAP_BASE                          # 5  TODO(FINAL)
SIZE_CAP_MAX = g.SIZE_CAP_MAX                            # 10  TODO(FINAL)
MAX_ORDERS_PER_EVENT = g.MAX_ORDERS_PER_EVENT            # 2  TODO(FINAL)
HEDGE_BAND_SHARES = g.HEDGE_BAND_SHARES                  # None: no default confirmed  TODO(FINAL)
HEDGE_BAND_SHARES_RANGE = g.HEDGE_BAND_SHARES_RANGE      # (200, 1000)  TODO(FINAL)
FEE_TICKS_PER_CONTRACT = g.FEE_TICKS_PER_CONTRACT        # 0.5  TODO(FINAL)
MM_HALF_SPREAD_TICKS = g.MM_HALF_SPREAD_TICKS            # 1  TODO(FINAL)
QUOTE_SIZE_RANGE = (g.QUOTE_SIZE_MIN, g.QUOTE_SIZE_MAX)  # (20, 200)  TODO(FINAL)
VOL_BETA = g.VOL_BETA                                    # 0.0  TODO(FINAL)

# ---------------------------------------------------------------- risk limits (compile-time on the FPGA)
JUMP_LIMIT_CENTS = g.JUMP_LIMIT_CENTS                    # R04, 370 c (error_curve.py)  TODO(FINAL)
NET_DELTA_LIMIT = g.NET_DELTA_LIMIT                      # R09, 20 (hedge qty: spy_shares)  TODO(FINAL)
NET_GAMMA_LIMIT = g.NET_GAMMA_LIMIT_X100 / 100.0         # R10, 0.03; ~2.4x looser on SPY  TODO(FINAL)
NET_VEGA_LIMIT = g.NET_VEGA_LIMIT                        # R11, $1,500 per vol point  TODO(FINAL)

# ---------------------------------------------------------------- agent (unset, TODO(FINAL))
TOOL_NAMES = ()                 # TODO(FINAL): agent tool names not chosen yet
NEMOTRON_ENDPOINT = None        # TODO(FINAL): not configured; no network calls are made from this repo's tests
NEMOTRON_MODEL = None           # TODO(FINAL): not chosen

REGISTRY = {k: v for k, v in globals().items() if k.isupper() and k != "ACTIVE_LOW"}

# ---------------------------------------------------------------- backend (APPENDED; all TODO(FINAL))
# Agent tool names (backend/agents/tools.py). Names are TODO(FINAL).
TOOL_NAMES = ("get_status", "get_log", "what_if", "replay", "propose_config", "request_kill")  # TODO(FINAL)
# LLM: the real client reads NEMOTRON_BASE_URL, NEMOTRON_MODEL, NVIDIA_API_KEY from the environment at call
# time (backend/agents/llm.py). Their values are never stored here, printed or logged.
NEMOTRON_ENV_VARS = ("NEMOTRON_BASE_URL", "NEMOTRON_MODEL", "NVIDIA_API_KEY")
LLM_BACKEND = os.environ.get("LA_LLM_BACKEND", "fake")        # "fake" (default) or "nemotron"  TODO(FINAL)
# Data source: the software twin by default. The real serial port is OFF by default.
USE_SERIAL = os.environ.get("LA_USE_SERIAL", "0") == "1"       # TODO(FINAL)
SERIAL_PORT = os.environ.get("LA_SERIAL_PORT")                 # e.g. "COM3"  TODO(FINAL)
SERIAL_BAUD = 1_000_000                                        # README: 1 Mbaud 8N1
SERIAL_WRITE_ENABLED = False                                   # never send frames to the real board  TODO(FINAL)
# Seeds: the live twin uses the search split; held-out checks use 60-79; agents never see 80+.
TWIN_SEEDS = (0, 59)                                           # TODO(FINAL)
HELD_OUT_SEEDS = (60, 79)
AGENT_SEED_LIMIT = 80                                          # seeds >= this are refused everywhere agents reach
TWIN_DEFAULT_SEED = 5                                          # TODO(FINAL)
TWIN_DEFAULT_REGIME = "normal"                                 # TODO(FINAL)
STRESS_REGIMES = ("normal", "single_jump", "flash_crash", "feed_fault")   # held-out stress set  TODO(FINAL)
LIMIT_WARN_FRAC = 0.5                                          # warn at 50% of a gate limit  TODO(FINAL)
LIMIT_HIGH_FRAC = 0.8                                          # high at 80% of a gate limit  TODO(FINAL)
BACKEND_WORKERS = 4                                            # evaluator processes for the app (tests use 1)  TODO(FINAL)
FORECAST_HORIZON_S = 0.05                                      # 50 ms of twin time  TODO(FINAL)
AUDIT_LOG_PATH = os.environ.get(
    "LA_AUDIT_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.jsonl"))   # TODO(FINAL)

REGISTRY.update({k: v for k, v in globals().items() if k.isupper() and k not in REGISTRY and k != "ACTIVE_LOW"})
REGISTRY["TOOL_NAMES"] = TOOL_NAMES
