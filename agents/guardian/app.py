# GUARDIAN — execute + verify + rollback.
from __future__ import annotations

from nexus_common import log

logger = log.get_logger("guardian")


def handler(event: dict, _context=None) -> dict:
    pid = event.get("prediction_id") or event.get("detail", {}).get("prediction", {}).get("id")
    logger.info("guardian invoked (scaffold)", prediction_id=pid)
    # TODO: step executor, verification window, rollback, ccloud health check.
    return {"agent": "guardian", "status": "scaffold-ok", "prediction_id": pid,
            "outcome": "noop"}
