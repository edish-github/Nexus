"""Structured JSON logging.

Each log line is a single JSON object carrying the domain correlation ids
(incident_id, prediction_id, playbook_id) when known.

    logger = log.get_logger("oracle")
    logger.info("prediction emitted", prediction_id=pid, service="payments")
"""
from __future__ import annotations

import json
import logging
import sys
import time

_DOMAIN_KEYS = ("incident_id", "prediction_id", "playbook_id")


class _JsonFormatter(logging.Formatter):
    def __init__(self, agent: str):
        super().__init__()
        self.agent = agent

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "agent": self.agent,
            "msg": record.getMessage(),
        }
        # merge structured extras attached via logger.info(msg, key=value)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _StructuredLogger:
    """Thin wrapper letting callers pass arbitrary keyword fields per line."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, msg: str, **fields) -> None:
        self._logger.log(level, msg, extra={"extra_fields": fields})

    def debug(self, msg: str, **f) -> None:
        self._log(logging.DEBUG, msg, **f)

    def info(self, msg: str, **f) -> None:
        self._log(logging.INFO, msg, **f)

    def warning(self, msg: str, **f) -> None:
        self._log(logging.WARNING, msg, **f)

    def error(self, msg: str, **f) -> None:
        self._log(logging.ERROR, msg, **f)

    def exception(self, msg: str, **f) -> None:
        self._logger.log(logging.ERROR, msg, exc_info=True, extra={"extra_fields": f})


def get_logger(agent: str) -> _StructuredLogger:
    import os

    logger = logging.getLogger(f"nexus.{agent}")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter(agent))
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return _StructuredLogger(logger)
