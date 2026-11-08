"""Load the Latency Arena repo's backend/ package under the alias `la_backend`.

Why an alias: the teammate's repo has its own top-level package named `backend` (this folder is dropped
in as their backend/app/), so `import backend` there resolves to THEIR package. Loading ours from its
file path under a different name avoids the clash. backend/ uses only relative imports internally, so it
works under any package name. backend/_paths.py then puts golden/, sim/, slowpath/, slowpath/tuning/ and
tools/ on sys.path.

Repo root: LA_REPO_ROOT if set, else the first parent folder of this file that contains backend/_paths.py.
"""

import importlib
import importlib.util
import os
import sys

ALIAS = "la_backend"


def repo_root():
    env = os.environ.get("LA_REPO_ROOT")
    if env:
        if not os.path.isfile(os.path.join(env, "backend", "_paths.py")):
            raise ImportError(f"LA_REPO_ROOT={env!r} does not contain backend/_paths.py")
        return os.path.abspath(env)
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isfile(os.path.join(d, "backend", "_paths.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise ImportError("Latency Arena repo not found: set LA_REPO_ROOT to the folder that contains backend/")
        d = parent


def load():
    if ALIAS in sys.modules:
        return sys.modules[ALIAS]
    root = repo_root()
    pkg = os.path.join(root, "backend")
    spec = importlib.util.spec_from_file_location(ALIAS, os.path.join(pkg, "__init__.py"),
                                                  submodule_search_locations=[pkg])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[ALIAS] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[ALIAS]
        raise
    return mod


def sub(name):
    load()
    return importlib.import_module(f"{ALIAS}.{name}")
