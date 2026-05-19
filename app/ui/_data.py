"""Cached data loaders for the Streamlit app.

Loads cell_features + latest scores into Polars/Pandas at startup; the
Tuner page rescores in-memory (no DB round-trip per slider change).
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from sqlalchemy import text

from app.core.db import session_scope


@st.cache_data(ttl=300, show_spinner="Loading feature snapshot…")
def load_features_df(resolution: int = 7) -> pd.DataFrame:
    """Read cell_features_res{R} into a DataFrame for in-memory scoring."""
    sql = text(
        f"""
        SELECT
            h3_to_string(h3_id) AS h3_id,
            state_code,
            is_excluded,
            exclusion_reasons,
            nearest_hv_line_km,
            nearest_hv_line_distinct_subgrid_km,
            nearest_substation_km,
            nearest_water_km,
            nearest_highway_km,
            nearest_railway_km,
            nearest_metro_km,
            nearest_cable_landing_km,
            annual_pvout_kwh_per_kwp,
            mean_temp_c,
            mean_rh_pct,
            pop_density_per_km2
        FROM dc_india.cell_features_res{resolution}
        """
    )
    with session_scope() as session:
        rows = session.execute(sql).all()
    cols = [
        "h3_id", "state_code", "is_excluded", "exclusion_reasons",
        "nearest_hv_line_km", "nearest_hv_line_distinct_subgrid_km",
        "nearest_substation_km", "nearest_water_km", "nearest_highway_km",
        "nearest_railway_km", "nearest_metro_km", "nearest_cable_landing_km",
        "annual_pvout_kwh_per_kwp", "mean_temp_c", "mean_rh_pct",
        "pop_density_per_km2",
    ]
    df = pd.DataFrame(rows, columns=cols)
    return df


@st.cache_data(ttl=300, show_spinner="Loading top sites…")
def load_top_sites(score_run_id: str | None = None) -> pd.DataFrame:
    if score_run_id is None:
        with session_scope() as session:
            score_run_id = session.execute(
                text(
                    "SELECT score_run_id FROM dc_india.scoring_runs "
                    "WHERE finished_at IS NOT NULL "
                    "ORDER BY started_at DESC LIMIT 1"
                )
            ).scalar_one_or_none()
        if score_run_id is None:
            return pd.DataFrame()
    sql = text(
        """
        SELECT h3_to_string(h3_id) AS h3_id,
               state_code, rank, score,
               ST_X(centroid) AS lon, ST_Y(centroid) AS lat
        FROM dc_india.top_sites_res7
        WHERE score_run_id = :run_id
        ORDER BY state_code, rank
        """
    )
    with session_scope() as session:
        rows = session.execute(sql, {"run_id": str(score_run_id)}).all()
    return pd.DataFrame(rows, columns=["h3_id", "state_code", "rank", "score", "lon", "lat"])


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
def get_ingestion_runs() -> pd.DataFrame:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT source, started_at, finished_at, status,
                       row_count, rows_rejected, duration_seconds,
                       upstream_source, schema_hash
                FROM dc_india.ingestion_runs
                ORDER BY started_at DESC
                LIMIT 200
                """
            )
        ).all()
    return pd.DataFrame(
        rows,
        columns=[
            "source", "started_at", "finished_at", "status",
            "row_count", "rows_rejected", "duration_seconds",
            "upstream_source", "schema_hash",
        ],
    )


@st.cache_data(ttl=300)
def get_dlq_counts() -> pd.DataFrame:
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT source, COUNT(*) AS dlq_count "
                "FROM dc_india.dead_letter_queue GROUP BY source"
            )
        ).all()
    return pd.DataFrame(rows, columns=["source", "dlq_count"])


@st.cache_data(ttl=300)
def get_schema_contracts() -> pd.DataFrame:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT source, schema_hash, version, updated_at, description
                FROM dc_india.schema_contracts
                ORDER BY source
                """
            )
        ).all()
    return pd.DataFrame(
        rows,
        columns=["source", "schema_hash", "version", "updated_at", "description"],
    )


def fetch_cell_detail(h3_id: str, resolution: int = 7) -> dict:
    """One-cell detail view: features + score breakdown + nearest infra."""
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
        out["breakdown"] = json.loads(s_row.breakdown)
    return out
