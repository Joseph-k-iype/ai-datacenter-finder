"""Full rebuild of the FalkorDB projection from PostGIS.

Idempotent end-to-end refresh. Use when:
  - Bringing up a fresh FalkorDB
  - Recovering from drift between the graph and Postgres
  - Validating projector correctness via parity tests

## Phase dependency graph

    seed_static_nodes  (always first; ExclusionReason vocab)
           │
           ▼
    ┌─── cells ───┐      ┌─── lineage ───┐
    │             │      │               │
    └──── power ──┘      └─── scoring ───┘
           │                    │
           ▼                    ▼
        stakeholder (reads Operator names from power + scoring runs)

The two "columns" are independent. ``cells`` and ``power`` write to
disjoint label sets (Cell/State/ProtectedArea vs Substation/Line/
SubGrid) but the cells projector materializes Cell-[:NEAREST_LINE]->Line
edges that require Line nodes to exist — so we run ``cells`` AFTER
``power`` is finished. ``lineage`` and ``scoring`` touch only
IngestionRun/SchemaContract/RejectedRow/ScoringRun/Score/Weight —
no overlap with the cells column.

FalkorDB serializes graph-level writes server-side (Redis module API
constraint), so running the columns concurrently helps mostly by
overlapping the Postgres reads + Python serialization with each other.
The wall-time gain is ~20-30% in practice.

## Tuning

Configure under ``configs/pipeline.yml::graph``:

    batch_size      — UNWIND batch size (default 5000)
    rebuild_workers — max concurrent phase columns (default 3)
"""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from app.core.config import load_pipeline_config
from app.core.logging import get_logger
from app.graph.client import drop_graph, ensure_indexes, seed_static_nodes

log = get_logger("graph.full_rebuild")


PhaseFn = Callable[[], dict[str, int]]


@dataclass
class _Phase:
    name: str
    fn: PhaseFn
    depends_on: tuple[str, ...] = ()


@dataclass
class RebuildResult:
    duration_seconds: float
    counts: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def _load_phases(only: list[str] | None) -> list[_Phase]:
    """Build the phase list with explicit dependencies for parallel scheduling.

    Lazy imports keep the orchestrator light when ``--only`` skips
    whole phases.
    """
    phases: list[_Phase] = []

    if only is None or "power" in only:
        from app.graph.projector.power import project_power

        phases.append(_Phase("power", project_power))
    if only is None or "cells" in only:
        from app.graph.projector.cells import project_cells

        # ``cells`` materializes Cell-[:NEAREST_LINE]->Line edges, so
        # Lines must exist first.
        phases.append(_Phase("cells", project_cells, depends_on=("power",)))
    if only is None or "lineage" in only:
        from app.graph.projector.lineage import project_lineage

        phases.append(_Phase("lineage", project_lineage))
    if only is None or "scoring" in only:
        from app.graph.projector.scoring import project_scoring

        # Score nodes link to Cell nodes via :DERIVED_FROM, so cells
        # column must be done first.
        phases.append(_Phase("scoring", project_scoring, depends_on=("cells",)))
    if only is None or "stakeholder" in only:
        from app.graph.projector.stakeholder import project_stakeholder

        # Operator nodes link to Line/Substation, SEZ/Hyperscaler to Cell.
        phases.append(
            _Phase("stakeholder", project_stakeholder, depends_on=("power", "cells"))
        )

    return phases


def rebuild_all(
    *,
    reset: bool = False,
    only: list[str] | None = None,
    workers: int | None = None,
) -> RebuildResult:
    """Rebuild the entire FalkorDB projection from PostGIS.

    Phases run with respect to their dependency graph (see module
    docstring). Independent phases run concurrently up to ``workers``
    threads.

    Parameters
    ----------
    reset
        Drop the graph first. Otherwise MERGE on top of existing state
        (idempotent but slower for first-time bulk loads).
    only
        Subset of phases to run. None = all. Example: ``["power", "lineage"]``.
    workers
        Max concurrent phases. Default from
        ``configs/pipeline.yml::graph.rebuild_workers`` (3).
    """
    started = time.monotonic()
    counts: dict[str, int] = {}
    skipped: list[str] = []
    cfg = load_pipeline_config().get("graph", {})
    if workers is None:
        workers = int(cfg.get("rebuild_workers", 3))

    if reset:
        drop_graph()

    ensure_indexes()
    counts["exclusion_reasons"] = seed_static_nodes()

    phases = _load_phases(only)
    if not phases:
        log.warning("graph.rebuild.no_phases")
        return RebuildResult(duration_seconds=time.monotonic() - started, counts=counts)

    log.info(
        "graph.rebuild.start",
        phases=[p.name for p in phases],
        workers=workers,
    )

    # Topological scheduler. Each phase becomes available when all its
    # dependencies have completed (or failed — we still try downstream
    # so the user sees every projector's status). ThreadPoolExecutor
    # lets independent phases overlap their Postgres reads.
    done: set[str] = set()
    failed: set[str] = set()
    pending = {p.name: p for p in phases}

    def _ready() -> list[_Phase]:
        out = []
        for ph in list(pending.values()):
            if all(dep in done or dep in failed for dep in ph.depends_on):
                out.append(ph)
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        in_flight: dict[Future, str] = {}

        def _submit_ready():
            for ph in _ready():
                if ph.name in in_flight.values() or len(in_flight) >= workers:
                    continue
                # Pop before submitting so we don't double-schedule.
                pending.pop(ph.name, None)
                log.info("graph.rebuild.phase.start", phase=ph.name)
                fut = pool.submit(ph.fn)
                in_flight[fut] = ph.name

        _submit_ready()
        while in_flight:
            for fut in as_completed(list(in_flight.keys())):
                name = in_flight.pop(fut)
                try:
                    counts.update(fut.result())
                    done.add(name)
                    log.info("graph.rebuild.phase.done", phase=name)
                except Exception as exc:  # noqa: BLE001
                    failed.add(name)
                    skipped.append(name)
                    log.error(
                        "graph.rebuild.phase.failed",
                        phase=name,
                        error=str(exc),
                    )
                _submit_ready()
                break  # re-enter as_completed with the updated in_flight dict

    duration = time.monotonic() - started
    log.info(
        "graph.rebuild.complete",
        duration_seconds=round(duration, 2),
        counts=counts,
        skipped=skipped,
    )
    return RebuildResult(duration_seconds=duration, counts=counts, skipped=skipped)
