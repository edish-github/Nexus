"""Changefeed webhook receiver: validate, parse, and republish to EventBridge."""
from __future__ import annotations

import hmac
import json
from typing import Any

import boto3
from nexus_common import config, log

logger = log.get_logger("receiver")
_events = boto3.client("events", region_name=config.AWS_REGION)


def _unauthorized(reason: str) -> dict:
    logger.warning("rejected webhook", reason=reason)
    return {"statusCode": 401, "body": json.dumps({"error": "unauthorized"})}


def _check_auth(headers: dict[str, str]) -> bool:
    provided = headers.get("authorization") or headers.get("Authorization") or ""
    expected = f"Bearer {config.changefeed_shared_secret()}"
    return hmac.compare_digest(provided, expected)


def _publish(rows: list[dict[str, Any]]) -> int:
    """Publish prediction rows to EventBridge in batches of 10 (API limit)."""
    entries = []
    for row in rows:
        pred = row.get("after")
        if not pred:
            continue
        idem = f"{pred.get('id')}:{row.get('updated', '')}"
        entries.append(
            {
                "Source": "nexus.changefeed",
                "DetailType": "prediction.created",
                "EventBusName": config.EVENT_BUS_NAME,
                "Detail": json.dumps({"idempotency_key": idem, "prediction": pred}),
            }
        )
    published = 0
    for i in range(0, len(entries), 10):
        batch = entries[i : i + 10]
        resp = _events.put_events(Entries=batch)
        failed = resp.get("FailedEntryCount", 0)
        published += len(batch) - failed
        if failed:
            logger.error("eventbridge partial failure", failed=failed, entries=resp["Entries"])
    return published


def handler(event: dict, _context=None) -> dict:
    headers = event.get("headers") or {}
    if not _check_auth(headers):
        return _unauthorized("bad or missing shared secret")

    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": json.dumps({"error": "invalid json"})}

    if "resolved" in envelope and "payload" not in envelope:
        logger.info("resolved checkpoint", resolved=envelope["resolved"])
        return {"statusCode": 200, "body": json.dumps({"ok": True, "resolved": True})}

    rows = envelope.get("payload", [])
    published = _publish(rows)
    logger.info("changefeed batch", received=len(rows), published=published)
    return {"statusCode": 200, "body": json.dumps({"ok": True, "published": published})}
