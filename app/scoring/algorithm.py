"""Main scoring driver: feature DataFrame → per-cell score + breakdown.

Reads cell_features_res{R} (non-excluded cells only), applies the smooth
transforms from ``scoring.transforms``, weighted-sums them per the given
weights YAML, and writes ``scores_res{R}`` rows linked to a ``scoring_runs``
row.

The breakdown JSONB stored per cell records both the *normalized
sub-scores* and the *weighted contributions*, so the UI can explain WHY a
cell scored high.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.config import load_weights
from app.core.db import bulk_execute, session_scope
from app.core.logging import get_logger
from app.scoring.transforms import (
    climate_score,
    exp_decay,
    latency_score,
    power_redundancy_score,
    sigmoid,
)

log = get_logger("scoring.algorithm")


def _normalize_weights(w: dict[str, float]) -> dict[str, float]:
    total = sum(w.values())
    if total <= 0:
        raise ValueError("Sum of weights must be positive")
    return {k: v / total for k, v in w.items()}


def _load_features(resolution: int) -> pd.DataFrame:
    sql = text(
        f"""
        SELECT
            h3_id::text AS h3_id,
            state_code,
            nearest_hv_line_km,
            nearest_hv_line_distinct_subgrid_km,
            nearest_water_km,
            nearest_highway_km,
            nearest_metro_km,
            nearest_cable_landing_km,
            annual_pvout_kwh_per_kwp,
            mean_temp_c
        FROM dc_india.cell_features_res{resolution}
        WHERE is_excluded = FALSE
        """
    )
    with session_scope() as session:
        rows = session.execute(sql).all()
    df = pd.DataFrame(rows, columns=[
        "h3_id", "state_code",
        "nearest_hv_line_km", "nearest_hv_line_distinct_subgrid_km",
        "nearest_water_km", "nearest_highway_km",
        "nearest_metro_km", "nearest_cable_landing_km",
        "annual_pvout_kwh_per_kwp", "mean_temp_c",
    ])
    # Replace nulls with conservative (worse-than-anything) defaults so cells
    # with missing data still score, just lower.
    fillna_map = {
        "nearest_hv_line_km": 9999.0,
        "nearest_hv_line_distinct_subgrid_km": 9999.0,
        "nearest_water_km": 9999.0,
        "nearest_highway_km": 9999.0,
        "nearest_metro_km": 9999.0,
        "nearest_cable_landing_km": 9999.0,
        "annual_pvout_kwh_per_kwp": 1000.0,
        "mean_temp_c": 50.0,
    }
    for k, v in fillna_map.items():
        df[k] = df[k].fillna(v)
    return df


def score_dataframe(df: pd.DataFrame, weights_cfg: dict[str, Any]) -> pd.DataFrame:
    """Pure function: score an in-memory DataFrame. Used by the Streamlit
    tuner for sub-second rescore without round-tripping to the DB.
    """
    w = _normalize_weights({k: float(v) for k, v in weights_cfg["weights"].items()})
    t = weights_cfg.get("transforms", {})

    power_t = t.get("power", {})
    water_t = t.get("water", {})
    conn_t  = t.get("connectivity", {})
    solar_t = t.get("solar", {})
    clim_t  = t.get("climate", {})
    lat_t   = t.get("latency", {})

    sub_power = power_redundancy_score(
        nearest_km=df["nearest_hv_line_km"].to_numpy(),
        nearest_distinct_subgrid_km=df["nearest_hv_line_distinct_subgrid_km"].to_numpy(),
        primary_decay_km=power_t.get("primary_decay_km", 15.0),
        secondary_decay_km=power_t.get("secondary_decay_km", 30.0),
        secondary_weight=power_t.get("secondary_weight", 0.4),
    )
    sub_water = exp_decay(df["nearest_water_km"].to_numpy(), decay_km=water_t.get("decay_km", 10.0))
    sub_conn  = exp_decay(df["nearest_highway_km"].to_numpy(), decay_km=conn_t.get("highway_decay_km", 5.0))
    sub_solar = sigmoid(
        df["annual_pvout_kwh_per_kwp"].to_numpy(),
        center=solar_t.get("pvout_center", 1400.0),
        spread=solar_t.get("pvout_spread", 200.0),
    )
    sub_clim  = climate_score(
        mean_temp_c=df["mean_temp_c"].to_numpy(),
        temp_low_c=clim_t.get("temp_low_c", 18.0),
        temp_high_c=clim_t.get("temp_high_c", 38.0),
    )
    sub_lat   = latency_score(
        nearest_metro_km=df["nearest_metro_km"].to_numpy(),
        nearest_cable_km=df["nearest_cable_landing_km"].to_numpy(),
        metro_decay_km=lat_t.get("metro_decay_km", 100.0),
        cable_decay_km=lat_t.get("cable_decay_km", 200.0),
        cable_weight=lat_t.get("cable_weight", 0.5),
    )

    score = (
        w["power_redundancy"] * sub_power
        + w["water_proximity"] * sub_water
        + w["connectivity"]    * sub_conn
        + w["solar_potential"] * sub_solar
        + w["climate"]         * sub_clim
        + w["latency"]         * sub_lat
    )

    out = df.copy()
    out["sub_power"] = sub_power
    out["sub_water"] = sub_water
    out["sub_conn"]  = sub_conn
    out["sub_solar"] = sub_solar
    out["sub_clim"]  = sub_clim
    out["sub_lat"]   = sub_lat
    out["score"]     = np.clip(score, 0.0, 1.0)
    return out


def score_cells(
    weights_path: str = "configs/weights/default.yml",
    resolution: int = 7,
) -> tuple[str, int]:
    """Score every non-excluded cell and persist results.

    Returns (score_run_id, n_cells).
    """
    weights_cfg = load_weights(weights_path)
    weights_id = weights_cfg.get("id", "default")
    df = _load_features(resolution)
    if df.empty:
        raise RuntimeError("No non-excluded cells to score. Did features run?")

    scored = score_dataframe(df, weights_cfg)

    run_id = str(uuid.uuid4())
    with session_scope() as session:
        session.execute(
            text(
                """
                INSERT INTO dc_india.scoring_runs
                  (score_run_id, weights_id, weights_payload, resolution, cells_scored)
                VALUES (:run_id, :weights_id, CAST(:payload AS JSONB), :res, :n)
                """
            ),
            {
                "run_id": run_id,
                "weights_id": weights_id,
                "payload": json.dumps(weights_cfg),
                "res": resolution,
                "n": len(scored),
            },
        )

        session.execute(
            text(
                "UPDATE dc_india.scoring_runs SET finished_at = now() "
                "WHERE score_run_id = :run_id"
            ),
            {"run_id": run_id},
        )

    # Bulk insert outside the open session — bulk_execute manages its own.
    # itertuples() is ~10× cheaper than iterrows() and produces a stream of
    # named-tuples — no per-row pandas Series allocation.
    target = f"scores_res{resolution}"
    rows: list[dict] = [
        {
            "h3": t.h3_id,
            "run_id": run_id,
            "score": float(t.score),
            "breakdown": json.dumps({
                "sub_power": float(t.sub_power),
                "sub_water": float(t.sub_water),
                "sub_conn":  float(t.sub_conn),
                "sub_solar": float(t.sub_solar),
                "sub_clim":  float(t.sub_clim),
                "sub_lat":   float(t.sub_lat),
            }),
        }
        for t in scored.itertuples(index=False)
    ]
    bulk_execute(
        f"""
        INSERT INTO dc_india.{target} (h3_id, score_run_id, score, breakdown)
        VALUES (CAST(:h3 AS h3index), :run_id, :score, CAST(:breakdown AS JSONB))
        """,
        rows,
    )

    log.info("scoring.complete", run_id=run_id, cells=len(scored))

    # Auto-project this run to FalkorDB if enabled. Soft-fails so a graph
    # outage never breaks scoring (the parity job + manual rebuild repair
    # any drift).
    from app.core.config import get_settings

    if get_settings().falkordb_auto_sync:
        try:
            from app.graph.projector.scoring import hook_after_scoring_run

            hook_after_scoring_run(str(run_id), resolution=resolution)
        except Exception as exc:  # noqa: BLE001
            log.warning("scoring.graph_sync_skipped", error=str(exc))

    return run_id, len(scored)
