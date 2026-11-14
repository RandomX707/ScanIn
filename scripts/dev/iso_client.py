"""Test client for the ISOLATED stack only (database channel360_iso_*, API :8010). Mints a fresh tenant per client."""
from __future__ import annotations

import json, os, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import jwt
import psycopg

ROOT = Path(__file__).resolve().parents[2]
DB = os.environ.get("C360_ISO_DB", "channel360_iso_dev")
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
if not DB.startswith("channel360_iso_"):
    raise SystemExit("REFUSED: iso_client only talks to databases named channel360_iso_*")
if urllib.parse.urlsplit(API).port in (8000, 5174, None):
    raise SystemExit(f"REFUSED: {API} looks like the shared application")


def _password(role: str) -> str:
    own = ROOT / ".runtime/iso-stack" / ("pgpass" if DB == "channel360_iso_dev" else f"pgpass.{DB}")      # the stack script writes one file per isolated database
    for line in (own if own.is_file() else ROOT / ".runtime/iso-stack/pgpass").read_text().splitlines():
        host, port, _db, user, pw = line.split(":", 4)
        if user == role:
            return pw
    raise SystemExit(f"no password for {role}")


def new_tenant(name: str = "Isolated test tenant") -> str:
    tenant = str(uuid4())
    with psycopg.connect(host="127.0.0.1", dbname=DB, user="channel360_test_migrator", password=_password("channel360_test_migrator")) as conn:
        with conn.transaction():
            conn.execute("select set_config('app.tenant_id',%s,true)", (tenant,))
            conn.execute("insert into tenants(id,name) values (%s,%s)", (tenant, name))
    return tenant


def token(tenant: str, role: str = "OWNER", subject: str = "channel360-demo-owner", ttl: int = 3000) -> str:
    secret = (ROOT / ".runtime/dev/development-auth-secret").read_text().strip()
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": subject, "tenant_id": tenant, "role": role, "iss": "idw-local-development", "aud": "idw-api",
                       "iat": now, "exp": now + timedelta(seconds=ttl)}, secret, algorithm="HS256")


class Client:
    def __init__(self, tenant: str | None = None, role: str = "OWNER"):
        self.tenant = tenant or new_tenant()
        self.tok = token(self.tenant, role)

    def call(self, method, path, body=None, headers=None, raw=False):
        req = urllib.request.Request(API + path, data=body, method=method, headers={"Authorization": f"Bearer {self.tok}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = r.read()
                return r.status, (data if raw else (json.loads(data) if data else None)), dict(r.headers)
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                return e.code, json.loads(data), dict(e.headers)
            except Exception:
                return e.code, data, dict(e.headers)

    def json(self, path):
        return self.call("GET", path)[1]

    def post(self, path, obj):
        return self.call("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"})

    def upload(self, path: Path):
        s, j, _ = self.call("PUT", "/api/documents?filename=" + urllib.parse.quote(Path(path).name), Path(path).read_bytes(), {"Content-Type": "application/octet-stream"})
        if s != 200:
            return s, j, None
        s2, j2, _ = self.call("POST", f"/api/documents/{j['document_id']}/inspect")
        return s2, j, j2

    def file(self, d, v):
        return self.call("GET", f"/api/documents/{d}/versions/{v}/file", raw=True)
