from datetime import datetime

import pytest

from refund_agent.agent.orchestrator import Orchestrator
from refund_agent.agent.scripted_llm import ScriptedLLM
from refund_agent.config import ROOT, Settings
from refund_agent.data_access.repository import ShopRepository
from refund_agent.state.store import RunStore
from refund_agent.tools.refund_tools import PaymentsGateway

NOW = datetime.fromisoformat("2026-10-20T12:00:00+00:00")


@pytest.fixture(scope="session")
def repo():
    return ShopRepository(ROOT / "data")


@pytest.fixture
def settings():
    return Settings(tool_timeout_s=0.2, allow_fault_injection=True)


@pytest.fixture
def payments():
    return PaymentsGateway()


@pytest.fixture
def make_orchestrator(repo, settings, payments):
    def factory(llm_factory=ScriptedLLM):
        return Orchestrator(repo, RunStore(), payments, settings, llm_factory)
    return factory


@pytest.fixture
def orch(make_orchestrator):
    return make_orchestrator()
