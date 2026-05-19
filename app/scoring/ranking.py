"""Top-N selection per state with a diversity constraint.

After scoring lands in scores_res7, this module picks the top-N cells per
state such that no two selected cells are within ``min_km`` of each other.
This prevents a single high-quality corridor (e.g. NH-65 near Pune) from
dominating the "top 5 sites in Maharashtra" callout.
"""
from __future__ import annotations

import math
from collections import defaultdict

from sqlalchemy import text

from app.core.db import bulk_execute, session_scope
from app.core.logging import get_logger

log = get_logger("scoring.ranking")


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def select_top_sites(
    score_run_id: str,
    *,
    n_per_state: int = 5,
    min_km: float = 50.0,
    resolution: int = 7,
) -> int:
    """Greedy diversity-aware top-N. Populates top_sites_res{resolution}."""
    cf = f"cell_features_res{resolution}"
    scores = f"scores_res{resolution}"
    grid = f"h3_cells_res{resolution}"

    with session_scope() as session:
        session.execute(
            text(f"DELETE FROM dc_india.top_sites_res{resolution} WHERE score_run_id = :run_id"),
            {"run_id": score_run_id},
        )

        rows = session.execute(
            text(
                f"""
                SELECT h3_to_string(s.h3_id) AS h3_id, c.state_code, s.score,
                       ST_X(ST_Centroid(g.geom)) AS lon,
                       ST_Y(ST_Centroid(g.geom)) AS lat
                FROM dc_india.{scores} s
                JOIN dc_india.{cf} c   ON c.h3_id = s.h3_id
                JOIN dc_india.{grid} g ON g.h3_id = s.h3_id
                WHERE s.score_run_id = :run_id
                ORDER BY c.state_code, s.score DESC
                """
            ),
            {"run_id": score_run_id},
        ).all()

    by_state: dict[str, list[tuple[str, float, float, float]]] = defaultdict(list)
    for r in rows:
        by_state[r.state_code].append((r.h3_id, float(r.score), float(r.lon), float(r.lat)))

    payload: list[dict] = []
    for state, cands in by_state.items():
        chosen: list[tuple[str, float, float, float]] = []
        for h3_id, score, lon, lat in cands:
            if all(_km(lat, lon, c[3], c[2]) >= min_km for c in chosen):
                chosen.append((h3_id, score, lon, lat))
            if len(chosen) >= n_per_state:
                break
        for rank, (h3_id, score, lon, lat) in enumerate(chosen, start=1):
            payload.append({
                "run_id": score_run_id,
                "state": state,
                "rank": rank,
                "h3": h3_id,
                "score": score,
                "lon": lon,
                "lat": lat,
            })

    inserted = bulk_execute(
        f"""
        INSERT INTO dc_india.top_sites_res{resolution}
          (score_run_id, state_code, rank, h3_id, score, centroid)
        VALUES
          (:run_id, :state, :rank, CAST(:h3 AS h3index), :score,
           ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
        """,
        payload,
    )
    log.info("ranking.top_sites", inserted=inserted, n_per_state=n_per_state)
    return inserted
