"""ESA WorldCover 2021 (v200) per-cell class fractions.

Class codes (v200):
  10=forest, 20=shrub, 30=grass, 40=cropland, 50=urban, 60=bare,
  70=snow/ice, 80=water, 90=wetland, 95=mangrove, 100=moss/lichen
"""
from __future__ import annotations

import ee

from app.core.config import load_pipeline_config
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run, should_skip
from app.ingest.base import validate_and_split
from app.ingest.gee._common import upsert_zonal
from app.ingest.gee.zonal_export import run_zonal_export

log = get_logger("ingest.gee.landcover")

CLASSES = {
    "forest_pct":   [10, 95],          # forest + mangrove
    "cropland_pct": [40],
    "urban_pct":    [50],
    "water_pct":    [80],
    "bareland_pct": [60],
    "wetland_pct":  [90],
}


def _make_class_mask(image: ee.Image, codes: list[int]) -> ee.Image:
    masks = [image.eq(c) for c in codes]
    out = masks[0]
    for m in masks[1:]:
        out = out.Or(m)
    return out.float()


def ingest(resolution: int = 7, *, fresh: bool = False) -> int:
    if existing := should_skip("gee.landcover", fresh=fresh):
        log.info("ingest.skip_recent", source="gee.landcover", existing_run_id=str(existing))
        return 0

    cfg = load_pipeline_config()
    asset = cfg["gee"]["layers"]["landcover"]
    scale = cfg["gee"]["scale_m"]["landcover"]
    contract = get_contract("gee.landcover")

    with ingestion_run(
        source="gee.landcover",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        wc = ee.ImageCollection(asset).first().select("Map")

        bands = []
        for name, codes in CLASSES.items():
            bands.append(_make_class_mask(wc, codes).rename(name))
        combined = ee.Image.cat(bands)

        stats = {"upserted": 0, "rejected": 0}

        def _consume(df) -> None:
            for name in CLASSES:
                df[name] = (df[name].astype(float).fillna(0.0) * 100.0).clip(0.0, 100.0)
            df = df[["h3_id", *CLASSES.keys()]]
            clean, rejected = validate_and_split(
                df, contract, run_id=str(run.run_id), source="gee.landcover"
            )
            stats["upserted"] += upsert_zonal(
                df=clean,
                table="raster_zonal_landcover",
                resolution=resolution,
                run_id=str(run.run_id),
                columns=list(CLASSES.keys()),
            )
            stats["rejected"] += rejected

        run_zonal_export(
            image=combined,
            reducer=ee.Reducer.mean(),
            band_names=list(CLASSES.keys()),
            resolution=resolution,
            export_name="landcover_wc",
            scale_m=scale,
            on_chunk=_consume,
        )
        run.row_count = stats["upserted"]
        run.rows_rejected = stats["rejected"]
        return stats["upserted"]
