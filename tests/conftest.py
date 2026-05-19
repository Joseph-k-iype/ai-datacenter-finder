"""Shared fixtures. Postgres-via-testcontainer for integration tests."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def pg_container():
    """Spin up a PostGIS container only when integration tests run.

    Note: requires Docker on the host. Skips gracefully if testcontainers
    isn't installed or Docker isn't available.
    """
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer

    container = (
        PostgresContainer("postgis/postgis:16-3.4")
        .with_env("POSTGRES_DB", "dc_india")
        .with_env("POSTGRES_USER", "dc")
        .with_env("POSTGRES_PASSWORD", "test")
    )
    container.start()
    try:
        # Patch env so app.core.config picks it up.
        os.environ["PG_HOST"] = container.get_container_host_ip()
        os.environ["PG_PORT"] = str(container.get_exposed_port(5432))
        os.environ["PG_DB"] = "dc_india"
        os.environ["PG_USER"] = "dc"
        os.environ["PG_PASSWORD"] = "test"
        os.environ["PG_SCHEMA"] = "dc_india"
        yield container
    finally:
        container.stop()
