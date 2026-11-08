from __future__ import annotations

from sqlalchemy import Engine, create_engine

from packages.api.settings import Settings


def create_runtime_engine(settings: Settings) -> Engine:
    """Create only the application/runtime engine; migration credentials are separate."""
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": 3},
    )


def create_worker_engine(settings: Settings) -> Engine | None:
    """Create the separate allowlisted orchestration-worker DB connection."""
    if settings.worker_database_url is None:
        return None
    return create_engine(
        settings.worker_database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        connect_args={"connect_timeout": 3},
    )
