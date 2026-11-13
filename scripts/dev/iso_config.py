"""Pure validation and SQL quoting helpers for the isolated development stack."""
from __future__ import annotations

import ipaddress
import re
import sys
from uuid import UUID


def validate_database_name(value: str) -> str:
    if not re.fullmatch(r"channel360_iso_[A-Za-z0-9_]+", value or "", flags=re.ASCII):
        raise ValueError("invalid_isolated_database_name")
    if len(value.encode("utf-8")) > 63:
        raise ValueError("isolated_database_name_too_long")
    return value


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def validate_tenant_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("invalid_tenant_uuid") from None


def validate_ports(api: str, web: str) -> tuple[int, int]:
    if not re.fullmatch(r"[0-9]+", api or "") or not re.fullmatch(r"[0-9]+", web or ""):
        raise ValueError("invalid_port")
    a, w = int(api), int(web)
    if not (1 <= a <= 65535 and 1 <= w <= 65535) or a in (8000, 5174) or w in (8000, 5174) or a == w:
        raise ValueError("invalid_port")
    return a, w


def validate_pg_target(host: str, port: str) -> tuple[str, int]:
    try:
        address = ipaddress.ip_address(host)
        number = int(port)
    except (ValueError, TypeError):
        raise ValueError("invalid_postgres_target") from None
    if not address.is_loopback or not 1 <= number <= 65535:
        raise ValueError("invalid_postgres_target")
    return str(address), number


if __name__ == "__main__":
    # Values are printed one per line for POSIX shell assignment through command substitution;
    # no shell code is evaluated from these values.
    command, *args = sys.argv[1:]
    try:
        if command == "database":
            print(validate_database_name(args[0]))
        elif command == "tenant":
            print(validate_tenant_id(args[0]))
        elif command == "ports":
            print(*validate_ports(args[0], args[1]))
        elif command == "pg-target":
            print(*validate_pg_target(args[0], args[1]))
        elif command == "quote-identifier":
            print(quote_identifier(args[0]))
        elif command == "quote-literal":
            print(quote_literal(args[0]))
        else:
            raise SystemExit("invalid_iso_config_command")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
