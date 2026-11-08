from __future__ import annotations

import argparse
import os
from uuid import UUID

from sqlalchemy import create_engine, text


def main() -> int:
    parser = argparse.ArgumentParser(description="Create one explicit local tenant using the migration credential.")
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--name", required=True, help="Non-sensitive local tenant label")
    args = parser.parse_args()
    url = os.environ.get("IDW_MIGRATION_DATABASE_URL")
    if not url:
        parser.error("IDW_MIGRATION_DATABASE_URL must name the isolated local Workbench database")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            database, current_user, database_owner = connection.execute(text("""SELECT current_database(),current_user,
                pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()""")).one()
            if (database, current_user, database_owner) != (
                "channel360_test", "channel360_test_migrator", "channel360_test_migrator"
            ):
                parser.error("local tenant bootstrap is restricted to the owned channel360_test database")
            connection.execute(text("SELECT set_config('app.tenant_id',:tenant,true)"),
                               {"tenant": str(args.tenant_id)})
            existing = connection.execute(text("SELECT name FROM tenants WHERE id=:tenant"),
                                          {"tenant": args.tenant_id}).scalar_one_or_none()
            if existing is None:
                connection.execute(text("INSERT INTO tenants(id,name) VALUES (:tenant,:name)"),
                                   {"tenant": args.tenant_id, "name": args.name.strip()})
                action = "created"
            elif existing == args.name.strip():
                action = "already_exists"
            else:
                parser.error("tenant ID already exists with a different label; no update was made")
        print(f"tenant {args.tenant_id}: {action}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
