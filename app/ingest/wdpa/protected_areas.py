"""WDPA protected areas ingest via GEE — paginated synchronous fetch.

We pull ``WCMC/WDPA/current/polygons`` filtered to ISO3='IND' (a few
thousand polygons) using ``.toList(page_size, offset).getInfo()`` in
small pages so each response stays well under the GEE 10 MB cap.

No GCS, no async export, no service-account requirements beyond EE auth.
"""
from __future__ import annotations

import json as _json

import ee
import pandas as pd
from shapely.geometry import shape

from app.core.config import load_pipeline_config
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.gee.client import init_ee
from app.ingest.osm._writers import insert_rows, truncate

log = get_logger("ingest.wdpa")

PAGE_SIZE_DEFAULT = 50      # WDPA polygons are large; small pages avoid >10MB responses
PROPS = ["WDPAID", "NAME", "DESIG_ENG", "IUCN_CAT"]


def _geometry_dict_to_wkt(geom: dict | str | None) -> str:
    """GEE may serialize geometry as GeoJSON dict or a JSON string."""
    if not geom:
        return ""
    if isinstance(geom, str):
        try:
            geom = _json.loads(geom)
        except _json.JSONDecodeError:
            return geom
    try:
        return shape(geom).wkt
    except Exception:
        return ""


def ingest_wdpa(india_only: bool = True, page_size: int = PAGE_SIZE_DEFAULT) -> int:
    init_ee()
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

        try:
            total = int(wdpa.size().getInfo())
        except Exception as exc:
            raise RuntimeError(f"Failed to query WDPA size: {exc}") from exc
        log.info("wdpa.size", total=total, page_size=page_size)

        rows: list[dict] = []
        for offset in range(0, total, page_size):
            page = wdpa.toList(page_size, offset).getInfo()
            for feat in page:
                props = dict(feat.get("properties", {}))
                wkt = _geometry_dict_to_wkt(feat.get("geometry"))
                if not wkt:
                    continue
                rows.append(
                    {
                        "wdpa_id": props.get("WDPAID"),
                        "name": props.get("NAME"),
                        "designation": props.get("DESIG_ENG"),
                        "iucn_cat": props.get("IUCN_CAT"),
                        "wkt": wkt,
                    }
                )
            log.info(
                "wdpa.page.done",
                offset=offset,
                page_rows=len(page),
                accumulated=len(rows),
            )

        if not rows:
            log.warning("wdpa.empty")
            return 0

        df = pd.DataFrame(rows)
        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="wdpa"
        )
        clean["ingestion_run_id"] = str(run.run_id)
        n = insert_rows("raw_protected_areas", clean.to_dict(orient="records"))
        run.row_count = n
        run.rows_rejected = rejected
        return n
