"""JSON log lines, one per trace event, so runs can be searched by run_id."""
from __future__ import annotations

import json
import logging
import sys

logger = logging.getLogger("refund_agent")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "msg": record.getMessage(), "logger": record.name}
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    if any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
