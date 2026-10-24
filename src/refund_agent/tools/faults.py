"""Named failures that tests and evals can switch on for a single run."""
from __future__ import annotations

from enum import StrEnum


class Fault(StrEnum):
    STALE_DATA = "stale_data"                        # order export is older than policy allows
    LOOKUP_TIMEOUT = "lookup_timeout"                # order lookup hangs past the timeout
    LOOKUP_DOWN = "lookup_down"                      # order service is unavailable
    INVALID_TOOL_RESPONSE = "invalid_tool_response"  # eligibility tool returns broken data
    PAYMENTS_DOWN = "payments_down"                  # payment service fails when refund is issued
    PAYMENTS_TIMEOUT = "payments_timeout"            # payment goes through but the reply comes too late
    MODEL_DOWN = "model_down"                        # model API is unavailable
    MALFORMED_MODEL_OUTPUT = "malformed_model_output"  # model sends a bad final answer once


def parse_faults(names: list[str]) -> set[Fault]:
    return {Fault(n) for n in names}
