"""SQLAlchemy engine + session factory + low-level helpers."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import PROJECT_ROOT, get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        future=True,
        connect_args={"options": f"-csearch_path={settings.pg_schema},public"},
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def apply_migrations() -> list[str]:
    """Apply every SQL file in infra/postgis/migrations in lexicographic order.

    Idempotent: each migration uses IF NOT EXISTS guards.
    """
    migrations_dir = PROJECT_ROOT / "infra" / "postgis" / "migrations"
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"Missing migrations dir: {migrations_dir}")

    applied: list[str] = []
    engine = get_engine()
    with engine.begin() as conn:
        # Force superuser-level connection here is not required because the
        # postgres image runs all init scripts as the POSTGRES_USER which has
        # the necessary CREATE EXTENSION privilege within its database.
        for path in sorted(migrations_dir.glob("*.sql")):
            sql = path.read_text()
            conn.execute(text(sql))
            applied.append(path.name)
    return applied


def bulk_execute(
    sql: str,
    rows: Sequence[dict[str, Any]],
    *,
    chunk_size: int = 5000,
) -> int:
    """Execute one parameterized statement against many parameter dicts.

    SQLAlchemy's ``Session.execute(text(sql), [dict1, dict2, ...])`` triggers
    DBAPI ``executemany``, which the psycopg3 driver translates into a
    single multi-row roundtrip per chunk. Orders of magnitude faster than
    a Python ``for row in rows: execute(...)`` loop.

    Returns the total number of parameter dicts processed.
    """
    if not rows:
        return 0
    stmt = text(sql)
    total = 0
    with session_scope() as session:
        for start in range(0, len(rows), chunk_size):
            chunk = list(rows[start : start + chunk_size])
            session.execute(stmt, chunk)
            total += len(chunk)
    return total


def check_health() -> dict[str, str | bool]:
    """Return a dict describing connectivity + key extensions."""
    out: dict[str, str | bool] = {}
    engine = get_engine()
    try:
        with engine.connect() as conn:
            out["postgres"] = conn.execute(text("SELECT version()")).scalar_one()
            out["postgis"] = bool(
                conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='postgis'")).first()
            )
            out["h3"] = bool(
                conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='h3'")).first()
            )
            out["h3_postgis"] = bool(
                conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname='h3_postgis'")
                ).first()
            )
            out["ok"] = True
    except Exception as exc:  # pragma: no cover — surfaced as CLI error
        out["ok"] = False
        out["error"] = str(exc)
    return out
