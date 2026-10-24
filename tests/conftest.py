from datetime import datetime

import pytest

from refund_agent.config import ROOT, Settings
from refund_agent.data_access.repository import ShopRepository

NOW = datetime.fromisoformat("2026-10-20T12:00:00+00:00")


@pytest.fixture(scope="session")
def repo():
    return ShopRepository(ROOT / "data")


@pytest.fixture
def settings():
    return Settings(tool_timeout_s=0.2, allow_fault_injection=True)
