"""WDPA protected areas ingest via GEE.

WCMC/WDPA/current/polygons is the canonical FeatureCollection. We filter by
ISO3='IND', then iterate features through GEE's task export (large
geometries → too big to inline). Output written to ``raw_protected_areas``.
"""
from __future__ import annotations

import time
import uuid

import ee

from app.core.config import get_settings, load_pipeline_config
from app.core.gcs import read_csv_glob
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.gee.client import init_ee
from app.ingest.osm._writers import insert_rows, truncate


def ingest_wdpa(india_only: bool = True) -> int:
    init_ee()
    settings = get_settings()
    cfg = load_pipeline_config()
    asset = cfg["gee"]["layers"]["wdpa"]

    contract = get_contract("wdpa")

    with ingestion_run(
        source="wdpa",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        truncate("raw_protected_areas")
        wdpa = ee.FeatureCollection(asset)
        if india_only:
            wdpa = wdpa.filter(ee.Filter.eq("ISO3", "IND"))

        # Add a WKT column so we can read it back simply.
        def _wkt(f: ee.Feature) -> ee.Feature:
            return f.set("wkt", f.geometry().toString(maxError=10))

        with_wkt = wdpa.map(_wkt).select(["WDPAID", "NAME", "DESIG_ENG", "IUCN_CAT", "wkt"])

        job_uid = uuid.uuid4().hex[:8]
        prefix = f"{settings.gcs_prefix}/wdpa_india_{job_uid}"
        task = ee.batch.Export.table.toCloudStorage(
            collection=with_wkt,
            description=f"wdpa_india_{job_uid}",
            bucket=settings.gcs_bucket,
            fileNamePrefix=prefix,
            fileFormat="CSV",
        )
        task.start()
        while task.status().get("state") not in {"COMPLETED", "FAILED", "CANCELLED"}:
            time.sleep(30)
        if task.status().get("state") != "COMPLETED":
            raise RuntimeError(f"WDPA export failed: {task.status()}")

        df = read_csv_glob(prefix)
        df = df.rename(
            columns={
                "WDPAID": "wdpa_id",
                "NAME": "name",
                "DESIG_ENG": "designation",
                "IUCN_CAT": "iucn_cat",
            }
        )

        # GEE may emit GeoJSON-formatted geometry strings; convert to WKT via shapely.
        import json as _json

        from shapely.geometry import shape

        def _to_wkt(s: str) -> str:
            if not isinstance(s, str) or not s:
                return ""
            try:
                geom = shape(_json.loads(s))
            except Exception:
                return s  # may already be WKT
            return geom.wkt

        df["wkt"] = df["wkt"].apply(_to_wkt)
        df = df[df["wkt"].str.len() > 0]

        clean, rejected = validate_and_split(
            df[["wdpa_id", "name", "designation", "iucn_cat", "wkt"]],
            contract,
            run_id=str(run.run_id),
            source="wdpa",
        )
        clean["ingestion_run_id"] = str(run.run_id)
        n = insert_rows("raw_protected_areas", clean.to_dict(orient="records"))
        run.row_count = n
        run.rows_rejected = rejected
        return n
