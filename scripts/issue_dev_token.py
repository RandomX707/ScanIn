from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

# Keep this script runnable by path from the repository root, just like the
# other host scripts that import the local ``packages`` namespace.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.api.auth import create_development_token
from packages.api.settings import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue a short-lived local/test identity token.")
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--role", required=True, choices=("processor", "reviewer", "administrator"))
    parser.add_argument("--expires-in-seconds", type=int, default=900)
    args = parser.parse_args()
    settings = get_settings()
    try:
        token = create_development_token(settings, subject=args.subject, tenant_id=args.tenant_id,
                                         role=args.role, expires_in_seconds=args.expires_in_seconds)
    except (RuntimeError, ValueError):
        parser.error("development tokens are available only in configured local/test mode")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
