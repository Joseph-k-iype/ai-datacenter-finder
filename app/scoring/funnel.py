"""Multi-resolution funnel orchestrator.

Per the architectural review: features are computed *independently per
resolution* (no averaging up or down). The funnel uses the H3 hierarchy
only as a navigational filter on cell IDs.

Flow:
  1. res 6 exclusion sweep → mark coarsely-bad cells.
  2. res 7 = h3_cell_to_children of all non-excluded res-6 cells.
  3. res 7 full features + scoring.
  4. res 8 = h3_cell_to_children of top-N res-7 cells.
  5. res 8 full features + scoring (for the chosen top-N drill-down).
"""
from __future__ import annotations

from app.core.config import load_pipeline_config
from app.core.logging import get_logger
from app.features.build import compute_features
from app.scoring.algorithm import score_cells
from app.scoring.ranking import select_top_sites

log = get_logger("scoring.funnel")


def run_full_funnel(
    weights_path: str | None = None,
    *,
    drill_top_n: int | None = None,
) -> dict[str, str | int]:
    """Run the full res-6 → res-7 → res-8 funnel.

    Returns a dict describing the run.
    """
    cfg = load_pipeline_config()
    scoring_cfg = cfg.get("scoring", {})
    weights_path = weights_path or scoring_cfg.get(
        "default_weights_file", "configs/weights/default.yml"
    )
    drill_top_n = drill_top_n or int(scoring_cfg.get("drill_top_n", 200))
    diversity_min_km = float(scoring_cfg.get("diversity_min_km", 50.0))
    n_per_state = int(scoring_cfg.get("top_n_per_state", 5))

    # Stage 1: coarse exclusion at res 6.
    compute_features(resolution=6, kind="exclusion")

    # Stage 2: full features at res 7.
    compute_features(resolution=7, kind="all")

    # Stage 3: score res 7.
    score_run_id, n7 = score_cells(weights_path=weights_path, resolution=7)
    select_top_sites(
        score_run_id,
        n_per_state=n_per_state,
        min_km=diversity_min_km,
        resolution=7,
    )

    # Stage 4: drill-down to res 8.
    compute_features(resolution=8, kind="all", top_n=drill_top_n)
    score_run_8, n8 = score_cells(weights_path=weights_path, resolution=8)

    log.info(
        "funnel.complete",
        score_run_id_res7=score_run_id,
        score_run_id_res8=score_run_8,
        cells_res7=n7,
        cells_res8=n8,
    )
    return {
        "score_run_id_res7": score_run_id,
        "score_run_id_res8": score_run_8,
        "cells_res7_scored": n7,
        "cells_res8_scored": n8,
    }
