"""Put the repo's existing Python folders on sys.path so the backend can import them read-only.

golden/ (golden.py, engine.py), sim/ (options_model.py), slowpath/ (frames.py),
slowpath/tuning/ (evaluator.py, tune.py), tools/ (gen_status_frame.py). Nothing there is edited.
"""

import os
import sys

BACKEND = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BACKEND)
for _sub in ("golden", "sim", "slowpath", os.path.join("slowpath", "tuning"), "tools"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

TUNING_RESULT = os.path.join(ROOT, "slowpath", "tuning", "tuning_result.json")
