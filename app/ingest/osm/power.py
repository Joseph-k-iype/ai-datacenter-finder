"""OSM power lines + substations ingest.

Voltage parsing is messy in OSM (sometimes "220000", sometimes "220 kV",
sometimes "220000;400000"). This module canonicalizes to integer kV.

If ``with_topology=True``, also runs sub-grid component labeling
(see ``power_topology``).
"""
from __future__ import annotations

import re
from typing import Any

import pandas as pd
from sqlalchemy import text

from app.core.config import load_pipeline_config, load_sources
from app.core.db import session_scope
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.osm import overpass
from app.ingest.osm._writers import (
    insert_rows,
    overpass_nodes_to_points,
    overpass_ways_to_linestrings,
    truncate,
)

log = get_logger("ingest.osm.power")

_KV_RE = re.compile(r"(\d+)")


def _parse_voltage_kv(raw: str | None) -> int | None:
    """Return the *highest* kV in the OSM voltage tag (which may be multivalued)."""
    if not raw:
        return None
    matches = _KV_RE.findall(raw)
    if not matches:
        return None
    values = [int(m) for m in matches]
    v = max(values)
    # OSM voltage tag convention: volts (e.g. 220000) for power=line.
    if v >= 1000:
        v = v // 1000
    return v


def _coerce_int(s: str | None) -> int | None:
    try:
        return int(s) if s is not None else None
    except (TypeError, ValueError):
        return None


def ingest_power(with_topology: bool = False) -> int:
    sources = load_sources()
    pipeline = load_pipeline_config()
    min_kv = int(min(pipeline["osm"]["hv_voltage_kv"]))
    line_q = sources["osm_overpass"]["power_lines_hv"]["query"]
    sub_q = sources["osm_overpass"]["substations_hv"]["query"]
    contract_lines = get_contract("osm.power_lines")
    contract_subs = get_contract("osm.substations")

    with ingestion_run(
        source="osm.power_lines",
        upstream_source="overpass[power=line,voltage>=220kV]",
        schema_hash=schema_hash(contract_lines),
    ) as run:
        truncate("raw_power_lines")
        raw = overpass.fetch(line_q)
        rows: list[dict[str, Any]] = []
        for r in overpass_ways_to_linestrings(raw.get("elements", [])):
            tags = r["tags"]
            kv = _parse_voltage_kv(tags.get("voltage"))
            if kv is None or kv < min_kv:
                continue
            rows.append(
                {
                    "osm_id": r["osm_id"],
                    "voltage_kv": kv,
                    "operator": tags.get("operator"),
                    "circuits": _coerce_int(tags.get("circuits")),
                    "wkt": r["wkt"],
                }
            )
        df = pd.DataFrame(rows)
        clean, rejected = validate_and_split(
            df, contract_lines, run_id=str(run.run_id), source="osm.power_lines"
        )
        clean["ingestion_run_id"] = str(run.run_id)
        insert_rows("raw_power_lines", clean.to_dict(orient="records"))
        run.row_count = len(clean)
        run.rows_rejected = rejected
        line_count = len(clean)

    with ingestion_run(
        source="osm.substations",
        upstream_source="overpass[power=substation,voltage>=220kV]",
        schema_hash=schema_hash(contract_subs),
    ) as run:
        truncate("raw_substations")
        raw = overpass.fetch(sub_q)
        rows = []
        for r in overpass_nodes_to_points(raw.get("elements", [])):
            tags = r["tags"]
            kv = _parse_voltage_kv(tags.get("voltage"))
            rows.append(
                {
                    "osm_id": r["osm_id"],
                    "name": tags.get("name"),
                    "voltage_kv": kv,
                    "operator": tags.get("operator"),
                    "wkt": r["wkt"],
                }
            )
        df = pd.DataFrame(rows)
        clean, rejected = validate_and_split(
            df, contract_subs, run_id=str(run.run_id), source="osm.substations"
        )
        clean["ingestion_run_id"] = str(run.run_id)
        insert_rows("raw_substations", clean.to_dict(orient="records"))
        run.row_count = len(clean)
        run.rows_rejected = rejected

    if with_topology:
        from app.ingest.osm.power_topology import label_subgrid_components

        label_subgrid_components()

    return line_count


def parallel_circuit_cluster() -> int:
    """Merge parallel circuits within ``parallel_cluster_eps_deg`` via ST_ClusterDBSCAN.

    Sets ``cluster_id`` on raw_power_lines. Lines sharing a cluster_id are
    on the same right-of-way (parallel circuits on the same tower string)
    and must NOT be considered independent feeds for Tier-4 redundancy.
    """
    cfg = load_pipeline_config()["power_topology"]
    eps_deg = float(cfg["parallel_cluster_eps_deg"])
    with session_scope() as session:
        result = session.execute(
            text(
                """
                WITH clustered AS (
                    SELECT id,
                           ST_ClusterDBSCAN(geom, eps := :eps, minpoints := 1)
                             OVER () AS cluster_id
                    FROM dc_india.raw_power_lines
                )
                UPDATE dc_india.raw_power_lines p
                SET cluster_id = c.cluster_id
                FROM clustered c
                WHERE p.id = c.id
                """
            ),
            {"eps": eps_deg},
        )
        n = result.rowcount or 0
    log.info("power.cluster.assigned", rows_updated=n)
    return n
