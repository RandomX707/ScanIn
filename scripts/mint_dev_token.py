#!/usr/bin/env python3
from __future__ import annotations

import argparse
from uuid import UUID

from packages.api.auth import create_development_token
from packages.api.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a short-lived local-only Workbench development token")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--role", choices=("processor", "reviewer", "administrator"), required=True)
    parser.add_argument("--expires-in", type=int, default=900, help="Lifetime in seconds; maximum 3600")
    args = parser.parse_args()
    settings = get_settings()
    print(
        create_development_token(
            settings,
            subject=args.subject,
            tenant_id=args.tenant_id,
            role=args.role,
            expires_in_seconds=args.expires_in,
        )
    )


if __name__ == "__main__":
    main()
