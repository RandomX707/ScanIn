#!/usr/bin/env python3
"""Fresh tenant + owner token in the ISOLATED database only (prints JSON {tenant_id, token})."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from iso_client import new_tenant, token  # noqa: E402

tenant = new_tenant("Isolated browser test run")
print(json.dumps({"tenant_id": tenant, "token": token(tenant, ttl=3500)}))
