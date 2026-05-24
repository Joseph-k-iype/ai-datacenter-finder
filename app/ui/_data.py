"""Cached data loaders for the unified Streamlit app.

All Postgres reads happen here and are memoised via ``st.cache_data`` so
slider changes never re-hit the DB. Tables are deliberately kept thin
(h3_id + score + lat + lon for the map; small per-state aggregates for
the stats panel) — pulling 600k rows over the wire would defeat the
whole single-page rebuild.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from sqlalchemy import text

from app.core.db import session_scope


# ---------------------------------------------------------------------------
# Score-run discovery + summary
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300)
def get_latest_run_id() -> str | None:
    with session_scope() as session:
        return session.execute(
            text(
                "SELECT score_run_id FROM dc_india.scoring_runs "
                "WHERE finished_at IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1"
            )
        ).scalar_one_or_none()


@st.cache_data(ttl=300)
def list_scoring_runs() -> pd.DataFrame:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT score_run_id, weights_id, resolution,
                       started_at, finished_at, cells_scored
                FROM dc_india.scoring_runs
                ORDER BY started_at DESC
                LIMIT 100
                """
            )
        ).all()
    return pd.DataFrame(
        rows,
        columns=[
            "score_run_id", "weights_id", "resolution",
            "started_at", "finished_at", "cells_scored",
        ],
    )


@st.cache_data(ttl=300)
def list_states() -> pd.DataFrame:
    """Return state_code → state_name lookup. Small (<40 rows)."""
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT state_code, state_name FROM dc_india.india_states "
                "ORDER BY state_name"
            )
        ).all()
    return pd.DataFrame(rows, columns=["state_code", "state_name"])


# ---------------------------------------------------------------------------
# Map payload — H3 cells + scores + centroids
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner="Loading scored hexagons…")
def load_score_map(
    run_id: str,
    state_code: str | None = None,
    min_score: float = 0.0,
    limit: int | None = None,
) -> pd.DataFrame:
    """Return ``h3_id, state_code, score, lat, lon`` for the map layer.

    Always filters to ``is_excluded = FALSE`` so the choropleth shows
    only viable cells. State + min-score + limit filters narrow the
    payload before it crosses the wire — at pan-India scale the
    unfiltered result is ~300k rows / ~25 MB JSON.

    Implementation note: lat/lon come straight from ``h3_cell_to_lat_lng``
    on the cell_features table. We do NOT join ``h3_cells_res7`` and call
    ``ST_Centroid(geom)`` — that 3-way join on 600k × 600k × 600k rows
    forces Postgres into a parallel hash join that blows the container's
    /dev/shm. The h3-pg function returns the canonical hex centroid
    directly from the index value.
    """
    # LATERAL evaluates h3_cell_to_lat_lng() once per row; field access on
    # the postgres `point` type uses [0]=x=lon, [1]=y=lat.
    sql = """
        SELECT s.h3_id::text AS h3_id,
               c.state_code,
               s.score,
               (pt.p)[0] AS lon,
               (pt.p)[1] AS lat
        FROM dc_india.scores_res7 s
        JOIN dc_india.cell_features_res7 c ON c.h3_id = s.h3_id
        CROSS JOIN LATERAL (SELECT h3_cell_to_lat_lng(s.h3_id) AS p) pt
        WHERE s.score_run_id = :run_id
          AND c.is_excluded = FALSE
          AND s.score >= :min_score
    """
    params: dict = {"run_id": run_id, "min_score": float(min_score)}
    if state_code:
        sql += " AND c.state_code = :state"
        params["state"] = state_code
    sql += " ORDER BY s.score DESC"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

    with session_scope() as session:
        rows = session.execute(text(sql), params).all()
    return pd.DataFrame(
        rows, columns=["h3_id", "state_code", "score", "lon", "lat"]
    )


@st.cache_data(ttl=300, show_spinner="Loading top sites…")
def load_top_sites(run_id: str, state_code: str | None = None) -> pd.DataFrame:
    """Diversity-aware top-N (~50 km min separation per state).

    Pulls the score breakdown alongside each row so the UI can render
    per-site reasoning without an extra round-trip.
    """
    sql = """
        SELECT t.h3_id::text AS h3_id,
               t.state_code, t.rank, t.score,
               ST_X(t.centroid) AS lon, ST_Y(t.centroid) AS lat,
               sc.breakdown::text AS breakdown_json
        FROM dc_india.top_sites_res7 t
        LEFT JOIN dc_india.scores_res7 sc
               ON sc.h3_id = t.h3_id AND sc.score_run_id = t.score_run_id
        WHERE t.score_run_id = :run_id
    """
    params: dict = {"run_id": run_id}
    if state_code:
        sql += " AND t.state_code = :state"
        params["state"] = state_code
    sql += " ORDER BY t.state_code, t.rank"

    with session_scope() as session:
        rows = session.execute(text(sql), params).all()
    return pd.DataFrame(
        rows,
        columns=["h3_id", "state_code", "rank", "score", "lon", "lat", "breakdown_json"],
    )


# ---------------------------------------------------------------------------
# Stats panel — small aggregates, fast to compute server-side
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner="Computing state stats…")
def load_state_stats(run_id: str) -> pd.DataFrame:
    sql = text(
        """
        SELECT c.state_code,
               AVG(s.score)                AS mean_score,
               PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY s.score) AS p90_score,
               MAX(s.score)                AS max_score,
               COUNT(*) FILTER (WHERE c.is_excluded = FALSE) AS cells_eligible,
               COUNT(*) FILTER (WHERE c.is_excluded = TRUE)  AS cells_excluded
        FROM dc_india.scores_res7 s
        JOIN dc_india.cell_features_res7 c ON c.h3_id = s.h3_id
        WHERE s.score_run_id = :run_id
        GROUP BY c.state_code
        ORDER BY mean_score DESC
        """
    )
    with session_scope() as session:
        rows = session.execute(sql, {"run_id": run_id}).all()
    return pd.DataFrame(
        rows,
        columns=[
            "state_code", "mean_score", "p90_score", "max_score",
            "cells_eligible", "cells_excluded",
        ],
    )


@st.cache_data(ttl=300)
def load_score_histogram(run_id: str, bins: int = 30) -> pd.DataFrame:
    sql = text(
        """
        WITH s AS (
            SELECT score
            FROM dc_india.scores_res7
            WHERE score_run_id = :run_id
        )
        SELECT width_bucket(score, 0, 1, :bins) AS bucket,
               COUNT(*) AS n,
               MIN(score) AS bin_min,
               MAX(score) AS bin_max
        FROM s
        GROUP BY bucket
        ORDER BY bucket
        """
    )
    with session_scope() as session:
        rows = session.execute(sql, {"run_id": run_id, "bins": int(bins)}).all()
    return pd.DataFrame(rows, columns=["bucket", "n", "bin_min", "bin_max"])


@st.cache_data(ttl=300)
def load_kpis(run_id: str) -> dict:
    """One-row summary for the header strip."""
    sql = text(
        """
        SELECT
            COUNT(*)                                AS cells_scored,
            AVG(s.score)                            AS mean_score,
            MAX(s.score)                            AS max_score,
            PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY s.score) AS p90_score,
            SUM(CASE WHEN c.is_excluded THEN 1 ELSE 0 END)::float
              / NULLIF(COUNT(*), 0)                 AS excluded_share
        FROM dc_india.scores_res7 s
        JOIN dc_india.cell_features_res7 c ON c.h3_id = s.h3_id
        WHERE s.score_run_id = :run_id
        """
    )
    with session_scope() as session:
        row = session.execute(sql, {"run_id": run_id}).first()
    if row is None:
        return {
            "cells_scored": 0,
            "mean_score": 0.0,
            "max_score": 0.0,
            "p90_score": 0.0,
            "excluded_share": 0.0,
        }
    return {
        "cells_scored": int(row[0] or 0),
        "mean_score": float(row[1] or 0.0),
        "max_score": float(row[2] or 0.0),
        "p90_score": float(row[3] or 0.0),
        "excluded_share": float(row[4] or 0.0),
    }


# ---------------------------------------------------------------------------
# Per-cell detail (for the right-side "reasoning" panel when a site is picked)
# ---------------------------------------------------------------------------
def fetch_cell_detail(h3_id: str, resolution: int = 7) -> dict:
    cf = f"cell_features_res{resolution}"
    sc = f"scores_res{resolution}"
    with session_scope() as session:
        f_row = session.execute(
            text(
                f"""
                SELECT * FROM dc_india.{cf}
                WHERE h3_id = CAST(:h3 AS h3index)
                """
            ),
            {"h3": h3_id},
        ).mappings().first()
        s_row = session.execute(
            text(
                f"""
                SELECT score, breakdown::text AS breakdown
                FROM dc_india.{sc}
                WHERE h3_id = CAST(:h3 AS h3index)
                ORDER BY score_run_id DESC LIMIT 1
                """
            ),
            {"h3": h3_id},
        ).first()
    out: dict = {"features": dict(f_row) if f_row else {}}
    if s_row:
        out["score"] = float(s_row.score)
        out["breakdown"] = json.loads(s_row.breakdown) if s_row.breakdown else {}
    return out


# ---------------------------------------------------------------------------
# Power-grid overlay (small — line geometries are kept for the map)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner="Loading HV grid overlay…")
def load_hv_lines_geojson() -> dict:
    """GeoJSON FeatureCollection of HV transmission lines (~20k features).

    Tiny enough to ship to the browser as a Kepler layer; used for "why
    is this site good?" context.
    """
    sql = text(
        """
        SELECT osm_id, voltage_kv,
               ST_AsGeoJSON(ST_Simplify(geom, 0.005)) AS gj
        FROM dc_india.raw_power_lines
        """
    )
    try:
        with session_scope() as session:
            rows = session.execute(sql).all()
    except Exception:
        return {"type": "FeatureCollection", "features": []}
    features = []
    for r in rows:
        try:
            geom = json.loads(r[2])
        except (ValueError, TypeError):
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "osm_id": int(r[0]) if r[0] is not None else None,
                "voltage_kv": int(r[1]) if r[1] is not None else None,
            },
            "geometry": geom,
        })
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Helpers for the "reasoning" copy
# ---------------------------------------------------------------------------
SUBSCORE_LABELS = {
    "sub_power": "Power redundancy",
    "sub_water": "Water proximity",
    "sub_conn":  "Connectivity (fiber)",
    "sub_solar": "Solar potential",
    "sub_clim":  "Climate (cool)",
    "sub_lat":   "Demand latency",
}


def summarise_breakdown(breakdown_json: str | dict | None) -> list[tuple[str, float]]:
    """Return the (label, value) pairs sorted by contribution descending."""
    if not breakdown_json:
        return []
    if isinstance(breakdown_json, str):
        try:
            data = json.loads(breakdown_json)
        except (ValueError, TypeError):
            return []
    else:
        data = breakdown_json
    pairs = [
        (SUBSCORE_LABELS.get(k, k), float(v))
        for k, v in data.items()
        if isinstance(v, (int, float))
    ]
    return sorted(pairs, key=lambda p: p[1], reverse=True)


def reasoning_sentence(score: float, breakdown_json: str | dict | None) -> str:
    """One-line plain-English explanation of why a cell scored where it did."""
    pairs = summarise_breakdown(breakdown_json)
    if not pairs:
        return f"Composite score {score:.3f}."
    top = pairs[:2]
    bottom = pairs[-1] if len(pairs) > 2 else None
    parts = [f"composite **{score:.3f}**"]
    parts.append("driven by " + " + ".join(f"{label.lower()} ({val:.2f})" for label, val in top))
    if bottom and bottom[1] < 0.4:
        parts.append(f"held back by weak {bottom[0].lower()} ({bottom[1]:.2f})")
    return " — ".join(parts).capitalize() + "."
