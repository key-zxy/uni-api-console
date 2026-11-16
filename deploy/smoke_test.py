#!/usr/bin/env python3
"""Local smoke test for the loopback-only audit console deployment."""
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def read_env(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def main() -> None:
    env = read_env(Path(".env"))
    token = env["AUDIT_ADMIN_TOKEN"]
    base = "http://127.0.0.1:8787"
    with urlopen(base + "/health", timeout=3) as response:
        health = json.load(response)
        assert response.status == 200 and health.get("status") == "ok"
    with urlopen(base + "/_audit", timeout=3) as response:
        html = response.read()
        assert response.status == 200 and len(html) > 100
    try:
        urlopen(base + "/_audit/api/quotas", timeout=3)
        raise AssertionError("console API allowed an unauthenticated request")
    except HTTPError as error:
        assert error.code == 401
    request = Request(
        base + "/_audit/api/quotas",
        headers={"Authorization": "Bearer " + token},
    )
    with urlopen(request, timeout=3) as response:
        rows = json.load(response)
        assert response.status == 200 and isinstance(rows, list)
    print(
        json.dumps(
            {
                "health": "ok",
                "ui_status": 200,
                "ui_bytes": len(html),
                "unauthenticated_status": 401,
                "authenticated_status": 200,
                "quota_rows": len(rows),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
