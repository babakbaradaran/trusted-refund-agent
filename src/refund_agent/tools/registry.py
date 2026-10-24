"""Tool registry: checks inputs and outputs, enforces timeouts, and turns failures into typed errors."""
from __future__ import annotations

import concurrent.futures as cf
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

READ_POOL = cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")
ACTION_POOL = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="action")  # hung reads can't block payouts


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ToolResult:
    name: str
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    attempts: int = 1
    duration_ms: float = 0.0
    any_timeout: bool = False  # an earlier attempt timed out, so its side effect may have happened

    def for_model(self) -> dict[str, Any]:
        if self.ok:
            return self.data or {}
        return {"error": self.error_code, "message": self.error_message}


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    func: Callable[[BaseModel], Any]

    def schema(self) -> dict[str, Any]:
        """Tool definition in the Anthropic Messages API format."""
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        schema["additionalProperties"] = False
        return {"name": self.name, "description": self.description, "input_schema": schema}


class ToolRegistry:
    def __init__(self, timeout_s: float = 2.0, retries: int = 1, pool: cf.ThreadPoolExecutor = READ_POOL):
        self.timeout_s = timeout_s
        self.retries = retries
        self.pool = pool
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        spec = self._tools.get(name)
        if spec is None:
            return self._fail(name, start, ToolError("UNKNOWN_TOOL", f"No tool named {name!r}"))
        try:
            parsed = spec.input_model.model_validate(args)
        except ValidationError as exc:
            return self._fail(name, start, ToolError("INVALID_ARGUMENTS", _brief(exc)))

        attempts = 0
        timed_out = False
        while True:
            attempts += 1
            try:
                raw = self._run_with_timeout(spec, parsed)
                data = spec.output_model.model_validate(raw).model_dump(mode="json")
                return ToolResult(name, True, data=data, attempts=attempts, duration_ms=_ms(start),
                                  any_timeout=timed_out)
            except ValidationError as exc:
                err = ToolError("INVALID_TOOL_RESPONSE", f"Tool returned data in the wrong shape: {_brief(exc)}")
            except ToolError as exc:
                err = exc
            except Exception as exc:  # a bug or crash inside the tool
                err = ToolError("TOOL_CRASHED", f"{type(exc).__name__}: {exc}")
            timed_out = timed_out or err.code == "TIMEOUT"
            if not (err.retryable and attempts <= self.retries):
                result = self._fail(name, start, err)
                result.attempts = attempts
                result.any_timeout = timed_out
                return result

    def _run_with_timeout(self, spec: ToolSpec, parsed: BaseModel) -> Any:
        future = self.pool.submit(spec.func, parsed)
        try:
            return future.result(timeout=self.timeout_s)
        except cf.TimeoutError as exc:
            future.cancel()
            raise ToolError("TIMEOUT", f"{spec.name} took longer than {self.timeout_s}s", retryable=True) from exc

    @staticmethod
    def _fail(name: str, start: float, err: ToolError) -> ToolResult:
        return ToolResult(name, False, error_code=err.code, error_message=err.message,
                          retryable=err.retryable, duration_ms=_ms(start))


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _brief(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'value'}: {e['msg']}" for e in exc.errors())[:300]
