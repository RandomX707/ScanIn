from __future__ import annotations

from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.engine import Connection

from packages.db.tenant import tenant_transaction


def test_tenant_transaction_refuses_connection_with_existing_transaction():
    connection = Mock(spec=Connection)
    connection.in_transaction.return_value = True

    with pytest.raises(RuntimeError, match="tenant_transaction_requires_idle_connection"):
        with tenant_transaction(connection, uuid4()):
            pytest.fail("tenant transaction must not be entered on a busy connection")

    connection.begin.assert_not_called()
