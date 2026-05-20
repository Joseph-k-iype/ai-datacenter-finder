"""Full rebuild of the FalkorDB projection from PostGIS.

Idempotent end-to-end refresh. Use when:
  - Bringing up a fresh FalkorDB
  - Recovering from drift between the graph and Postgres
  - Validating projector correctness via parity tests

Order matters — later projectors create edges into earlier nodes:

    1. seed_static_nodes (ExclusionReason — independent of any ingest)
    2. cells          — Cell, State, ProtectedArea, infrastructure stubs
    3. power          — Substation, Line, SubGrid, CONNECTS, PARALLEL_TO
    4. lineage        — IngestionRun, SchemaContract, RejectedRow
    5. scoring        — ScoringRun, Score, Weight, DERIVED_FROM
    6. stakeholder    — Operator extraction + SEZ + Hyperscaler

Each step is independently skippable for incremental rebuilds.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.graph.client import drop_graph, ensure_indexes, seed_static_nodes

log = get_logger("graph.full_rebuild")


@dataclass
class RebuildResult:
    duration_seconds: float
    counts: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def rebuild_all(*, reset: bool = False, only: list[str] | None = None) -> RebuildResult:
    """Rebuild the entire FalkorDB projection from PostGIS.

    Parameters
    ----------
    reset
        Drop the graph first. Otherwise MERGE on top of existing state
        (idempotent but slower for first-time bulk loads).
    only
        Subset of phases to run. None = all. Example: ``["power", "lineage"]``.
    """
    started = time.monotonic()
    counts: dict[str, int] = {}
    skipped: list[str] = []

    if reset:
        drop_graph()

    ensure_indexes()
    counts["exclusion_reasons"] = seed_static_nodes()

    phases: list[tuple[str, Callable[[], dict[str, int]]]] = []

    # Imports are lazy because each projector module pulls in heavy deps
    # (geopandas, sqlalchemy text queries) that we don't want to pay if
    # the user is rebuilding only one slice.
    if only is None or "cells" in only:
        from app.graph.projector.cells import project_cells

        phases.append(("cells", project_cells))
    if only is None or "power" in only:
        from app.graph.projector.power import project_power

        phases.append(("power", project_power))
    if only is None or "lineage" in only:
        from app.graph.projector.lineage import project_lineage

        phases.append(("lineage", project_lineage))
    if only is None or "scoring" in only:
        from app.graph.projector.scoring import project_scoring

        phases.append(("scoring", project_scoring))
    if only is None or "stakeholder" in only:
        from app.graph.projector.stakeholder import project_stakeholder

        phases.append(("stakeholder", project_stakeholder))

    for name, fn in phases:
        try:
            log.info("graph.rebuild.phase.start", phase=name)
            counts.update(fn())
            log.info("graph.rebuild.phase.done", phase=name)
        except Exception as exc:  # noqa: BLE001
            # We don't want one missing source (e.g. WDPA not ingested
            # yet) to abort the whole rebuild — log and continue.
            log.error("graph.rebuild.phase.failed", phase=name, error=str(exc))
            skipped.append(name)

    duration = time.monotonic() - started
    log.info(
        "graph.rebuild.complete",
        duration_seconds=round(duration, 2),
        counts=counts,
        skipped=skipped,
    )
    return RebuildResult(duration_seconds=duration, counts=counts, skipped=skipped)
