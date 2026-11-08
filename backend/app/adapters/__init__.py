"""Adapter registry. Swapping scenarios = changing SCENARIO_ADAPTER (contract rule 6)."""
from __future__ import annotations

import importlib
import os

# name -> "module:Class". Varish/Haashim add their real adapters here.
REGISTRY = {
    "mock_trading": "app.adapters.mock_trading:MockTradingAdapter",
    "trading": "app.adapters.trading:TradingAdapter",      # Varish
    "disaster": "app.adapters.disaster:DisasterAdapter",   # Haashim
}

_instance = None


def get_adapter(reload: bool = False):
    global _instance
    if _instance is None or reload:
        name = os.getenv("SCENARIO_ADAPTER", "mock_trading")
        mod, cls = REGISTRY[name].split(":")
        _instance = getattr(importlib.import_module(mod), cls)()
    return _instance
