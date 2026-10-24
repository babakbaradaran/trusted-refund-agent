"""Quick checks against a running deployment. Never approves anything, so no money can move.

    python scripts/smoke_test.py http://127.0.0.1:8000
"""
import os
import sys

import httpx


def main(base: str) -> int:
    key = os.environ.get("SMOKE_AGENT_KEY", "agent-key-123")
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
        if not ok:
            failures.append(name)

    with httpx.Client(base_url=base, timeout=30) as c:
        health = c.get("/api/health")
        body = health.json() if health.status_code == 200 else {}
        check("health", health.status_code == 200 and body.get("status") == "ok", str(body))
        check("data fresh", body.get("data_stale") is False, f"exported {body.get('data_exported_at')}")
        check("page loads", c.get("/").status_code == 200)
        check("auth enforced", c.get("/api/runs").status_code == 401)

        run = c.post("/api/runs", headers={"X-API-Key": key},
                     json={"customer_message": "Order 1003, the blanket has a tear in it."})
        status = run.json().get("status") if run.status_code == 201 else run.text[:200]
        check("agent run", run.status_code == 201 and status in ("completed", "escalated"), str(status))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"))
