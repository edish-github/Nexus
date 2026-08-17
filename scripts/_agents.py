"""Load agent handlers by path.

Every agent is its own Lambda code directory with the same entrypoint name,
`app.py`, so importing two of them normally would collide on `sys.modules`.
Loading them by path under distinct names lets one process drive the whole
pipeline — which is what the local runner and the tests both need — without the
agents having to know anything about it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def load_agent(name: str) -> ModuleType:
    """Import `agents/<name>/app.py` as the module `nexus_agent_<name>`."""
    module_name = f"nexus_agent_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = AGENTS_DIR / name / "app.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load agent {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
