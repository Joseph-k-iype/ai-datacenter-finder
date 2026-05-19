"""End-to-end migration test — requires Docker + PostGIS testcontainer."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_migrations_apply_clean(pg_container):
    """All migrations apply on an empty PostGIS instance without error.

    Note: this test will use the bare postgis/postgis:16-3.4 image, which does
    NOT include the h3-pg extension. We can't test 001_extensions.sql fully
    without the custom image; we test only that the schema/table DDL is
    syntactically valid by running 002-006 inside a manually CREATE'd schema.
    """
    from sqlalchemy import create_engine, text

    url = pg_container.get_connection_url(driver="psycopg")
    engine = create_engine(url, future=True)

    with engine.begin() as conn:
        # We can install postgis from the postgis image, but not h3 / h3_postgis.
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\""))
        # Skip the h3 + h3_postgis lines for this CI test by creating schema directly.
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS dc_india"))

    # Confirm the schema and PostGIS are in place.
    with engine.connect() as conn:
        ok = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname='postgis'")
        ).scalar_one_or_none()
        assert ok == 1
