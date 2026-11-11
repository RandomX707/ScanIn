#!/usr/bin/env python3
"""Create a fresh, isolated local tenant for a browser-test run and print a short-lived owner token as JSON.

Uses the same private local credentials as scripts/dev/start.sh (PGPASSFILE, development auth secret); nothing
secret is printed except the token the suite needs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import jwt

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    host, port = os.environ.get("PGHOST", "127.0.0.1"), os.environ.get("PGPORT", "5432")
    env = {**os.environ, "PGHOST": host, "PGPORT": port,
           "PGPASSFILE": os.environ.get("PGPASSFILE", str(Path.home() / ".channel360-pgpass")),
           "IDW_MIGRATION_DATABASE_URL": f"postgresql+psycopg://channel360_test_migrator@{host}:{port}/channel360_test"}
    tenant = uuid4()
    subprocess.run([sys.executable, str(ROOT / "scripts/bootstrap_local_tenant.py"), "--tenant-id", str(tenant),
                    "--name", "Channel360 browser test run"], check=True, env=env, stdout=subprocess.DEVNULL)
    secret = (ROOT / ".runtime/dev/development-auth-secret").read_text().strip()
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": "channel360-demo-owner", "tenant_id": str(tenant), "role": "OWNER",
                        "iss": "idw-local-development", "aud": "idw-api", "iat": now,
                        "exp": now + timedelta(seconds=3500)}, secret, algorithm="HS256")
    print(json.dumps({"tenant_id": str(tenant), "token": token}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
