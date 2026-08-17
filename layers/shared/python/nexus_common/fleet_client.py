"""Client for the simulated fleet's control API — Guardian's hands.

This is the seam where a playbook stops being a row in a table and becomes an
action against something. In the demo that something is `generator/live.py`; in
a real deployment it would be whatever actually scales the pool. Guardian only
ever talks through this interface, so swapping the substrate does not touch the
remediation logic.

`GENERATOR_URL` unset means there is nothing to act on. Rather than pretend a
step succeeded, every call raises `FleetUnavailable`, and Guardian records the
execution as failed-to-start instead of inventing an outcome.

Uses urllib rather than requests so the Lambda layer needs no HTTP dependency.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import log

logger = log.get_logger("fleet")

DEFAULT_TIMEOUT = 8.0


class FleetUnavailable(RuntimeError):
    """The fleet control API is not configured or not reachable."""


def base_url() -> str:
    url = os.environ.get("GENERATOR_URL", "").strip().rstrip("/")
    if not url:
        raise FleetUnavailable(
            "GENERATOR_URL is not set — no fleet to act on. Start `make live` and "
            "point GENERATOR_URL at it."
        )
    return url


def configured() -> bool:
    return bool(os.environ.get("GENERATOR_URL", "").strip())


def _request(method: str, path: str, payload: dict | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    url = f"{base_url()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        raise FleetUnavailable(f"{method} {path} returned {e.code}: {e.read()[:200]!r}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FleetUnavailable(f"{method} {path} failed: {e}") from e


def snapshot() -> list[dict]:
    """Current status of every service."""
    return _request("GET", "/fleet").get("services", [])


def telemetry(service: str) -> dict:
    """The service's trailing window, digest and canonical text."""
    return _request("GET", f"/telemetry/{service}")


def window(service: str) -> dict[str, list[float]]:
    return telemetry(service).get("window", {})


def step_key(action: str, params: dict | None) -> str:
    """The identity of a step: its desired state, not the moment it was asked for.

    Must match `generator.fleet.step_key` exactly — it is how a retry recognises
    a step it already applied, and how a rollback names the step it undoes.
    """
    return f"{action}:{json.dumps(params or {}, sort_keys=True, default=str)}"


def apply_action(service: str, action: str, params: dict | None = None,
                 revert_of: str | None = None) -> dict:
    """Execute one remediation step, or revert one applied earlier.

    Passing `revert_of` is how a rollback undoes exactly what it undid rather
    than layering a second remediation on top of the first.
    """
    result = _request("POST", "/action", {
        "service": service, "action": action, "params": params or {},
        "revert_of": revert_of,
    })
    logger.info("fleet action", service=service, action=action,
                effective=result.get("effective"), effect=result.get("effect"),
                revert_of=revert_of)
    return result


def start_ramp(service: str, archetype: str, speed: float = 1.0) -> dict:
    """Only used by rehearsal scripts; agents never start ramps."""
    return _request(
        "POST", "/ramp", {"service": service, "archetype": archetype, "speed": speed}
    )
