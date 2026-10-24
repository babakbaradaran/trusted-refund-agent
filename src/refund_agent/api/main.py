"""HTTP API and the web page. Thin layer: checks input and permissions, then calls the orchestrator."""
from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..agent.llm import LLM
from ..agent.orchestrator import InvalidTransition, Orchestrator
from ..agent.scripted_llm import ScriptedLLM
from ..config import Settings
from ..data_access.repository import ShopRepository
from ..domain.models import RunStatus
from ..observability import configure_logging, logger
from ..state.store import RunNotFound, RunStore
from ..tools.faults import Fault
from ..tools.refund_tools import PaymentsGateway
from .auth import Caller, require
from .schemas import ApproveRequest, CreateRunRequest, OrderPresentation, RejectRequest, RunView, TraceView

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def default_llm_factory(settings: Settings) -> Callable[[], LLM]:
    if settings.llm_provider == "claude":
        from ..agent.claude_llm import ClaudeLLM  # SDK loads only when Claude is used

        return lambda: ClaudeLLM(model=settings.claude_model)
    return ScriptedLLM


def create_app(settings: Settings | None = None, llm_factory: Callable[[], LLM] | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging()
    repo = ShopRepository(settings.data_dir)
    payments = PaymentsGateway()
    orchestrator = Orchestrator(repo, RunStore(settings.db_path), payments, settings,
                                llm_factory or default_llm_factory(settings))

    app = FastAPI(title="Trusted Refund Agent", version="0.1.0")
    app.state.settings = settings
    app.state.repo = repo
    app.state.payments = payments
    app.state.orchestrator = orchestrator

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        try:
            response = await call_next(request)
        except Exception:  # last line of defence: no stack traces to clients
            logger.exception("unhandled", extra={"fields": {"request_id": rid, "path": request.url.path}})
            response = JSONResponse({"detail": "Internal error", "request_id": rid}, status_code=500)
        response.headers["X-Request-ID"] = rid
        return response

    def get_run_or_404(run_id: str):
        try:
            return orchestrator.store.get(run_id)
        except RunNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found") from None

    @app.get("/api/health")
    def health():
        return {"status": "ok", "llm_provider": settings.llm_provider,
                "data_exported_at": repo.exported_at, "data_stale": repo.is_stale(settings.now),
                "data_quality_issues": len(repo.issues), "orders_loaded": len(repo.orders),
                "fault_injection": settings.allow_fault_injection}

    @app.post("/api/runs", status_code=status.HTTP_201_CREATED, response_model=RunView)
    def create_run(body: CreateRunRequest, caller: Caller = Depends(require("agent"))):
        if body.faults:
            if not settings.allow_fault_injection:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fault injection is disabled on this server")
            unknown = set(body.faults) - {f.value for f in Fault}
            if unknown:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown faults: {sorted(unknown)}")
        run = orchestrator.start(body.customer_message, created_by=caller.role, faults=body.faults)
        return RunView.of(run)

    @app.get("/api/runs", response_model=list[RunView])
    def list_runs(status_filter: RunStatus | None = None, _: Caller = Depends(require("agent"))):
        return [RunView.of(r) for r in orchestrator.store.list(status_filter)]

    @app.get("/api/runs/{run_id}", response_model=RunView)
    def get_run(run_id: str, _: Caller = Depends(require("agent"))):
        return RunView.of(get_run_or_404(run_id))

    @app.get("/api/runs/{run_id}/trace", response_model=TraceView)
    def get_trace(run_id: str, _: Caller = Depends(require("agent"))):
        run = get_run_or_404(run_id)
        return TraceView(run_id=run.run_id, status=run.status, facts=run.facts, events=run.events,
                         transcript=run.transcript)

    @app.post("/api/runs/{run_id}/approve", response_model=RunView)
    def approve(run_id: str, body: ApproveRequest, caller: Caller = Depends(require("supervisor"))):
        get_run_or_404(run_id)
        try:
            run = orchestrator.approve(run_id, by=caller.key_hint, note=body.note, customer_reply=body.customer_reply)
        except InvalidTransition as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        return RunView.of(run)

    @app.post("/api/runs/{run_id}/reject", response_model=RunView)
    def reject(run_id: str, body: RejectRequest, caller: Caller = Depends(require("supervisor"))):
        get_run_or_404(run_id)
        try:
            run = orchestrator.reject(run_id, by=caller.key_hint, note=body.note)
        except InvalidTransition as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return RunView.of(run)

    @app.get("/api/orders/{order_id}", response_model=OrderPresentation)
    def get_order(order_id: str, _: Caller = Depends(require("agent"))):
        order = repo.get_order(order_id)
        if order is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Order {order_id} not found")
        return OrderPresentation.of(order, repo.get_customer(order.customer_id))

    @app.get("/api/data-quality")
    def data_quality(_: Caller = Depends(require("supervisor"))):
        return {"issues": [vars(i) for i in repo.issues]}

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory refund_agent.api.main:app_factory`."""
    return create_app()
