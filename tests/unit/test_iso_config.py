import pytest

from scripts.dev.iso_config import (
    quote_identifier,
    quote_literal,
    validate_database_name,
    validate_pg_target,
    validate_ports,
    validate_tenant_id,
)


@pytest.mark.parametrize("name", ["channel360_iso_dev", "channel360_iso_run_20261114", "channel360_iso_A9"])
def test_accepts_simple_isolated_database_identifiers(name):
    assert validate_database_name(name) == name


@pytest.mark.parametrize("name", [
    "channel360_iso_", "channel360_test", "channel360_iso_x y", "channel360_iso_x;DROP_DATABASE",
    "channel360_iso_x' OR 1=1 --", "channel360_iso_../../secrets", "channel360_iso_x\nDROP",
    "channel360_iso_x$HOME", "channel360_iso_x-odd",
])
def test_rejects_empty_suffix_injection_path_traversal_and_other_invalid_names(name):
    with pytest.raises(ValueError):
        validate_database_name(name)


def test_rejects_postgres_identifiers_longer_than_63_bytes():
    with pytest.raises(ValueError):
        validate_database_name("channel360_iso_" + "x" * 49)


def test_quotes_identifiers_and_literals_independently_of_validation():
    assert quote_identifier('name"; DROP DATABASE channel360_test;--') == '"name""; DROP DATABASE channel360_test;--"'
    assert quote_literal("x'; DROP DATABASE channel360_test;--") == "'x''; DROP DATABASE channel360_test;--'"


def test_tenant_id_must_be_a_uuid():
    assert validate_tenant_id("bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb") == "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
    for bad in ("", "not-a-uuid", "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb'; DROP TABLE tenants;--"):
        with pytest.raises(ValueError):
            validate_tenant_id(bad)


def test_ports_are_numeric_in_range_distinct_and_not_shared():
    assert validate_ports("8010", "5180") == (8010, 5180)
    for pair in (("8000", "5180"), ("8010", "5174"), ("8010", "8010"), ("0", "5180"),
                 ("65536", "5180"), ("api", "5180"), ("8010;echo", "5180")):
        with pytest.raises(ValueError):
            validate_ports(*pair)


def test_postgres_target_is_loopback_only_and_uses_valid_port():
    assert validate_pg_target("127.0.0.1", "5432") == ("127.0.0.1", 5432)
    for host, port in (("db.example.com", "5432"), ("127.0.0.1;DROP", "5432"), ("localhost", "0")):
        with pytest.raises(ValueError):
            validate_pg_target(host, port)
