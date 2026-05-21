"""FalkorDB client wrapper.

Same connection-singleton pattern as ``app/core/db.py``: ``lru_cache`` on a
factory function so the rest of the codebase can call ``get_graph()``
without worrying about lifecycle.

The Postgres engine is heavy (connection pool, statement cache); FalkorDB
is just a Redis socket — but we still cache it so structured logs share
a stable client name and so MERGE-heavy projectors don't pay handshake
cost on every batch.
"""
from __future__ import annotations

import math
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("graph.client")


# ---------------------------------------------------------------------------
# Payload sanitization — FalkorDB's parameter encoder stringifies floats
# directly. ``float('nan')`` becomes the literal token ``nan`` which the
# Cypher parser rejects mid-query with ``Invalid input ... expected `…``.
# Pandas extension types (``pd.NA``, ``pandas.NaT``) compare unequal to
# themselves, same trick as ``app/governance/dlq.py::_json_safe``.
# ---------------------------------------------------------------------------
def _scrub(value: Any) -> Any:
    """Recursively replace NaN / inf / pd.NA / NaT with None.

    Called on every params dict before passing to FalkorDB. Cheap O(n)
    walk; the typical payload is a dict ``{"rows": [<list of dicts>]}``
    so we recurse one level into lists and dicts.
    """
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    # Pandas extension scalars: ``pd.NA != pd.NA`` returns ``<NA>``,
    # and ``bool(<NA>)`` raises TypeError ("boolean value of NA is
    # ambiguous"). Catch that and treat it as missing — semantically
    # what the caller meant by including pd.NA in the payload.
    # numpy NaN compares unequal to itself and yields a True bool.
    try:
        if value != value:  # noqa: PLR0124 — NaN self-inequality
            return None
    except TypeError:
        return None
    return value


# ---------------------------------------------------------------------------
# Lazy imports so the test suite can run without the falkordb dep installed
# (e.g. unit tests of constants in app.graph.schema).
# ---------------------------------------------------------------------------
def _import_falkordb():  # pragma: no cover — thin shim
    try:
        from falkordb import FalkorDB  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "falkordb client not installed. Run `uv sync` to install it."
        ) from exc
    return FalkorDB


@lru_cache(maxsize=1)
def get_client():
    """Return a cached FalkorDB connection.

    FalkorDB() opens a Redis connection lazily; the first query call
    triggers the actual socket. Connection failures surface there, not
    here — keep that in mind when wiring health checks.
    """
    FalkorDB = _import_falkordb()
    settings = get_settings()
    log.info(
        "graph.connect",
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        graph=settings.falkordb_graph,
    )
    return FalkorDB(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
    )


def get_graph():
    """Return the named graph (``dc_india`` by default)."""
    settings = get_settings()
    return get_client().select_graph(settings.falkordb_graph)


def reset_client_cache() -> None:
    """Clear the cached client. Used by tests + ``dc graph rebuild``
    to force reconnect after a config or graph-name change."""
    get_client.cache_clear()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------
def query(cypher: str, params: dict[str, Any] | None = None):
    """Run a Cypher query against the configured graph.

    Params are scrubbed of NaN/inf/pd.NA before being stringified into
    the GRAPH.QUERY command — otherwise FalkorDB's parser bails. Returns
    the raw FalkorDB ``QueryResult`` — callers should iterate
    ``.result_set`` (list of rows) or read ``.statistics``.

    Retries once on a dropped TCP connection: FalkorDB 8.6.x has been
    observed to segfault inside EnforceUniqueEntity and auto-restart
    from RDB within ~1s, leaving our cached client holding a dead
    socket. A single reconnect-and-retry papers over the brief outage
    so callers running long batched rebuilds don't have to.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError

    scrubbed = _scrub(params) if params else {}
    try:
        return get_graph().query(cypher, scrubbed)
    except RedisConnectionError:
        log.warning("graph.query.reconnect")
        reset_client_cache()
        return get_graph().query(cypher, scrubbed)


def query_rows(cypher: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
    """Convenience: return the result set as a list of rows."""
    return list(query(cypher, params).result_set)


def default_batch_size() -> int:
    """Tunable batch size for UNWIND projectors.

    5,000 is the sweet spot we've measured against pan-India res-7
    (~600k cells × ~16 properties). Bigger batches amortize the MERGE
    planner overhead; beyond ~10k the GRAPH.QUERY command exceeds Redis
    inline-arg limits and queries start failing intermittently.

    Override per-rebuild via ``configs/pipeline.yml::graph.batch_size``
    when needed (e.g. lower it on memory-constrained dev boxes).
    """
    from app.core.config import load_pipeline_config

    return int(load_pipeline_config().get("graph", {}).get("batch_size", 5000))


@contextmanager
def batched_write(
    label: str,
    rows: Iterable[dict[str, Any]],
    *,
    batch_size: int | None = None,
) -> Generator[Iterable[list[dict[str, Any]]], None, None]:
    """Yield successive batches of rows for a Cypher UNWIND projector.

    Pattern:
        with batched_write("Cell", all_cells) as chunks:
            for chunk in chunks:
                query("UNWIND $rows AS r MERGE (c:Cell {h3_id: r.h3_id}) SET c += r",
                      {"rows": chunk})

    The context manager only exists to capture timing + log a summary;
    the actual batching is a plain generator. ``batch_size=None`` (the
    default) reads from ``configs/pipeline.yml::graph.batch_size``.
    """
    import time

    if batch_size is None:
        batch_size = default_batch_size()

    started = time.monotonic()
    total = 0

    def _gen() -> Iterable[list[dict[str, Any]]]:
        nonlocal total
        batch: list[dict[str, Any]] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                total += len(batch)
                yield batch
                batch = []
        if batch:
            total += len(batch)
            yield batch

    try:
        yield _gen()
    finally:
        log.info(
            "graph.batched_write",
            label=label,
            rows=total,
            duration_seconds=round(time.monotonic() - started, 2),
            batch_size=batch_size,
        )


# ---------------------------------------------------------------------------
# Health + bootstrap
# ---------------------------------------------------------------------------
def health() -> dict[str, Any]:
    """Lightweight ping + count summary. Mirrors ``app.core.db.check_health``."""
    settings = get_settings()
    try:
        # FalkorDB exposes Redis's connection_pool; ping via the client.
        client = get_client()
        pong = client.connection.ping()
        # Count nodes in the graph for a quick sanity signal.
        node_count_row = query_rows("MATCH (n) RETURN count(n)")
        node_count = int(node_count_row[0][0]) if node_count_row else 0
        return {
            "ok": bool(pong),
            "host": settings.falkordb_host,
            "port": settings.falkordb_port,
            "graph": settings.falkordb_graph,
            "node_count": node_count,
        }
    except Exception as exc:
        return {
            "ok": False,
            "host": settings.falkordb_host,
            "port": settings.falkordb_port,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _is_already_exists_error(exc: Exception) -> bool:
    """True if the error message indicates an index/constraint already
    exists. FalkorDB's wording varies between versions ("already
    indexed", "constraint already exists", "duplicate")."""
    msg = str(exc).lower()
    return any(tok in msg for tok in ("already", "exists", "duplicate"))


def ensure_indexes() -> list[str]:
    """Create all declared indexes + unique constraints. Idempotent.

    Two APIs, one per case:

      * Plain index → Cypher ``CREATE INDEX FOR (n:Label) ON (n.prop)``.
        Works in FalkorDB.

      * Unique constraint → ``graph.create_node_unique_constraint(label,
        prop)`` Python client call. FalkorDB does NOT accept the
        Neo4j-5 ``CREATE CONSTRAINT FOR ... REQUIRE ... IS UNIQUE``
        Cypher syntax (its parser bails with
        ``Invalid input 'F': expected '=' or CREATE CONSTRAINT ON``).

    FalkorDB also requires an index on the property before a unique
    constraint can attach — we create the plain index first regardless,
    then layer the constraint on top when ``unique=True``.
    """
    from app.graph.schema import INDEXES

    g = get_graph()
    created: list[str] = []

    for spec in INDEXES:
        # Step 1: ensure the underlying range index exists.
        try:
            query(spec.index_cypher())
        except Exception as exc:  # noqa: BLE001
            if not _is_already_exists_error(exc):
                raise

        # Step 2: layer the unique constraint on top if requested.
        if spec.unique:
            try:
                g.create_node_unique_constraint(spec.label, spec.property)
            except Exception as exc:  # noqa: BLE001
                if not _is_already_exists_error(exc):
                    # Some FalkorDB builds also return a generic
                    # "pending" / "not supported" message we'd want to
                    # surface, so don't swallow blindly.
                    raise

        created.append(
            f"{spec.label}.{spec.property}{' [unique]' if spec.unique else ''}"
        )

    log.info("graph.indexes.ensured", created=created)
    return created


def drop_graph() -> None:
    """Delete the entire graph. Used by ``dc graph rebuild --reset``.

    Cheap and atomic in FalkorDB — implemented server-side as a single
    Redis DEL on the graph key.
    """
    import contextlib

    settings = get_settings()
    with contextlib.suppress(Exception):
        # Graph may not exist yet on first rebuild — swallow that one.
        query("MATCH (n) DETACH DELETE n")
    log.warning("graph.dropped", graph=settings.falkordb_graph)


def seed_static_nodes() -> int:
    """Insert nodes that are not derived from any ingest (e.g. exclusion reasons).

    Returns the number of nodes upserted.
    """
    from app.graph.schema import EXCLUSION_REASONS, N

    rows = [{"name": name, "description": desc} for name, desc in EXCLUSION_REASONS]
    query(
        f"UNWIND $rows AS r MERGE (e:{N.EXCLUSION_REASON} {{name: r.name}}) "
        "SET e.description = r.description",
        {"rows": rows},
    )
    return len(rows)
