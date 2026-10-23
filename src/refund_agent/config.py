"""Settings read from environment variables, with safe defaults for local runs."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The sample data is a fixed export, so the app uses a fixed "now" by default.
DEMO_NOW = "2026-10-20T12:00:00+00:00"

DEFAULT_KEYS = "agent-key-123:agent,supervisor-key-456:supervisor"


def _parse_keys(raw: str) -> dict[str, str]:
    pairs = (item.split(":", 1) for item in raw.split(",") if ":" in item)
    return {key.strip(): role.strip() for key, role in pairs}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    db_path: str = ":memory:"
    now: datetime = datetime.fromisoformat(DEMO_NOW)
    llm_provider: str = "scripted"  # scripted | claude
    claude_model: str = "claude-opus-5-5"
    api_keys: dict[str, str] = field(default_factory=lambda: _parse_keys(DEFAULT_KEYS))
    allow_fault_injection: bool = False
    tool_timeout_s: float = 2.0
    tool_retries: int = 1
    max_agent_steps: int = 8

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        provider = env.get("LLM_PROVIDER") or ("claude" if env.get("ANTHROPIC_API_KEY") else "scripted")
        return cls(
            data_dir=Path(env.get("DATA_DIR", ROOT / "data")),
            db_path=env.get("RUNS_DB", str(ROOT / "runs.db")),
            now=datetime.fromisoformat(env.get("APP_NOW", DEMO_NOW)),
            llm_provider=provider,
            claude_model=env.get("CLAUDE_MODEL", "claude-opus-5-5"),
            api_keys=_parse_keys(env.get("API_KEYS", DEFAULT_KEYS)),
            allow_fault_injection=env.get("ALLOW_FAULT_INJECTION", "false").lower() == "true",
            tool_timeout_s=float(env.get("TOOL_TIMEOUT_S", "2.0")),
        )
