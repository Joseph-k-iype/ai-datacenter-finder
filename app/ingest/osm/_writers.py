"""Shared writer helpers for OSM ingest modules."""
from __future__ import annotations

from typing import Any

from shapely.geometry import LineString, Point
from sqlalchemy import text

from app.core.db import bulk_execute, session_scope
from app.core.logging import get_logger

log = get_logger("ingest.osm.writers")


def overpass_ways_to_linestrings(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Overpass 'way' elements with inline geom to LineString WKT rows."""
    out = []
    for el in elements:
        if el.get("type") != "way" or "geometry" not in el:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        out.append(
            {
                "osm_id": el["id"],
                "tags": el.get("tags", {}),
                "wkt": LineString(coords).wkt,
            }
        )
    return out


def overpass_nodes_to_points(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Overpass 'node' (or 'way' with 'center') elements to Point WKT rows."""
    out = []
    for el in elements:
        if el.get("type") == "node":
            lon, lat = el["lon"], el["lat"]
        elif el.get("type") == "way" and "center" in el:
            lon, lat = el["center"]["lon"], el["center"]["lat"]
        else:
            continue
        out.append(
            {
                "osm_id": el["id"],
                "tags": el.get("tags", {}),
                "wkt": Point(lon, lat).wkt,
            }
        )
    return out


def truncate(table: str) -> None:
    with session_scope() as session:
        session.execute(text(f"TRUNCATE dc_india.{table} RESTART IDENTITY"))


def insert_rows(table: str, rows: list[dict[str, Any]], *, chunk_size: int = 5000) -> int:
    """Bulk-insert rows. Each row must contain ``wkt`` (geometry as WKT)
    + ``ingestion_run_id`` + the other table columns.

    The ``wkt`` key is auto-converted to the table's ``geom`` column.
    """
    if not rows:
        return 0

    cols = list(rows[0])
    if "wkt" not in cols or "ingestion_run_id" not in cols:
        raise ValueError("rows must contain 'wkt' and 'ingestion_run_id'")

    target_cols = [("geom" if c == "wkt" else c) for c in cols]
    placeholders = [
        "ST_GeomFromText(:wkt, 4326)" if c == "wkt" else f":{c}" for c in cols
    ]
    sql = (
        f"INSERT INTO dc_india.{table} ({', '.join(target_cols)}) "
        f"VALUES ({', '.join(placeholders)})"
    )
    n = bulk_execute(sql, rows, chunk_size=chunk_size)
    log.info("osm.inserted", table=table, n=n)
    return n
