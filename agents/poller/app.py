"""Fallback poller: republish pending predictions when the changefeed sink fails.

Disabled by default; enable the schedule in infra/template.yaml if the webhook
path is unavailable.
"""
from __future__ import annotations

import json

import boto3

from nexus_common import config, db, log

logger = log.get_logger("poller")
_events = boto3.client("events", region_name=config.AWS_REGION)

_PENDING_SQL = """
    SELECT id, service_name, predicted_outcome, predicted_severity,
           alpha, beta, created_at
    FROM predictions
    WHERE prevention_status = 'pending'
      AND created_at > now() - INTERVAL '5 minutes'
    ORDER BY created_at
    LIMIT 25
"""


def handler(event: dict | None = None, _context=None) -> dict:
    rows = db.query(_PENDING_SQL)
    entries = [
        {
            "Source": "nexus.poller",
            "DetailType": "prediction.created",
            "EventBusName": config.EVENT_BUS_NAME,
            "Detail": json.dumps(
                {
                    "idempotency_key": f"poll:{r[0]}",
                    "prediction": {
                        "id": str(r[0]),
                        "service_name": r[1],
                        "predicted_outcome": r[2],
                        "predicted_severity": r[3],
                        "alpha": r[4],
                        "beta": r[5],
                    },
                }
            ),
        }
        for r in rows
    ]
    published = 0
    for i in range(0, len(entries), 10):
        batch = entries[i : i + 10]
        resp = _events.put_events(Entries=batch)
        published += len(batch) - resp.get("FailedEntryCount", 0)
    logger.info("poll cycle", pending=len(rows), published=published)
    return {"pending": len(rows), "published": published}
