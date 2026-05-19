"""Orchestrator for feature computation.

Workflow (matches the multi-res funnel):
  - kind="exclusion": exclusions only (cheap; eliminates ~80% of cells).
  - kind="scoring":   distance + redundancy + coverage + climate/solar/pop.
  - kind="all":       both.

For res 8, ``top_n`` is required and limits computation to the children of
the top-N res-7 cells (drill-down).
"""
from __future__ import annotations

from app.core.db import session_scope
from app.core.logging import get_logger
from app.features.coverage import merge_coverage
from app.features.distances import compute_distances
from app.features.exclusions import compute_exclusions
from app.features.redundancy import compute_redundancy
from app.features.solar import merge_climate_solar_pop

log = get_logger("features.build")


def compute_features(*, resolution: int, kind: str = "all", top_n: int | None = None) -> int:
    """Run the requested feature stages.

    Returns the row count of the deepest table touched.
    """
    if resolution == 8 and top_n is None:
        raise ValueError("res 8 requires --top-n (drill-down only).")
    if resolution == 8:
        from sqlalchemy import text

        from app.grid.builder import populate_drilldown_cells

        with session_scope() as session:
            latest = session.execute(
                text(
                    "SELECT score_run_id FROM dc_india.scoring_runs "
                    "ORDER BY started_at DESC LIMIT 1"
                )
            ).scalar_one_or_none()
        if latest is None:
            raise RuntimeError("No scoring_runs row yet; score res-7 before drilling down.")
        populate_drilldown_cells(str(latest), top_n=top_n)

    total = 0
    if kind in ("exclusion", "all"):
        total = compute_exclusions(resolution)
    if kind in ("scoring", "all"):
        merge_coverage(resolution)
        compute_distances(resolution)
        compute_redundancy(resolution)
        merge_climate_solar_pop(resolution)

    log.info("features.build.done", resolution=resolution, kind=kind, rows=total)
    return total
