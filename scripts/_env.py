"""Local-run bootstrap shared by the scripts that import repo code.

In Lambda, `nexus_common` arrives as a layer on `/opt/python` and `generator` is
not present at all. Locally both are just directories in the repo, so the path
has to be arranged before either can be imported. Doing it in one place keeps
the import dance out of every script.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAYER_PATH = REPO_ROOT / "layers" / "shared" / "python"


def load_dotenv() -> None:
    """Minimal .env loader — no dependency, and it never overrides a real env var."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def bootstrap() -> None:
    for path in (str(REPO_ROOT), str(LAYER_PATH)):
        if path not in sys.path:
            sys.path.insert(0, path)
    load_dotenv()


def require_dsn() -> str:
    dsn = os.environ.get("COCKROACH_DB_URL")
    if not dsn:
        sys.exit("COCKROACH_DB_URL is not set (put it in .env or the environment)")
    return dsn
