"""GEE asset existence + discovery helpers.

Three problems this module solves:

1. ``ee.Image(asset)`` is lazy — evaluation (and thus the "not found" error)
   doesn't fire until ``getInfo()``. We need a cheap up-front existence check
   so layers can fail fast with a clear message OR skip gracefully.

2. The "publish H3 grid once" pattern is fundamental to large-scale zonal
   stats, but ``ee.batch.Export.table.toAsset`` produces *individual sub-asset
   tasks* — there's no single consolidated asset until something merges them.
   This module's ``discover_cells_collection`` does that merge dynamically at
   read time, so zonal_export works whether the user pre-merged or not.

3. Catching ``ee.EEException`` text-matching errors centralises the "is this
   the not-found error or a real auth/network problem?" decision so it
   doesn't need to be re-derived in every layer.
"""
from __future__ import annotations

import ee
from ee import ee_exception

from app.core.logging import get_logger

log = get_logger("ingest.gee.assets")


class AssetNotFoundError(Exception):
    """Raised when an expected GEE asset is missing.

    Layers that can run without the asset should catch this and skip;
    layers that depend on it should let it bubble.
    """


def _is_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in ("not found", "does not exist", "could not be found"))


def asset_exists(asset_id: str) -> bool:
    """Cheap up-front existence check via ``ee.data.getAsset``.

    Returns False on canonical "not found" errors; re-raises on anything
    else (auth, network, quota) so the caller sees the real problem.
    """
    try:
        ee.data.getAsset(asset_id)
        return True
    except ee_exception.EEException as exc:
        if _is_not_found(exc):
            return False
        raise


def list_child_assets(parent_id: str) -> list[str]:
    """List asset IDs under a parent path. Empty list if parent doesn't exist."""
    try:
        out = ee.data.listAssets({"parent": parent_id})
    except ee_exception.EEException as exc:
        if _is_not_found(exc):
            return []
        raise
    return [a.get("name", "") for a in out.get("assets", []) if a.get("name")]


def discover_cells_collection(consolidated_id: str) -> ee.FeatureCollection | None:
    """Resolve the H3 cells FeatureCollection.

    Strategy:
      1. If the consolidated asset exists, return it.
      2. Otherwise, look for sub-assets named ``<consolidated_id>_part_NNNN``
         in the same parent path and merge them.
      3. If neither exists, return None — caller decides how to handle.

    This is the layer that makes ``make push-grid-to-gee`` work without a
    manual ``earthengine asset merge`` follow-up.
    """
    if asset_exists(consolidated_id):
        return ee.FeatureCollection(consolidated_id)

    parent = consolidated_id.rsplit("/", 1)[0]
    base_name = consolidated_id.rsplit("/", 1)[1]
    prefix = f"{parent}/{base_name}_part_"

    children = list_child_assets(parent)
    parts = sorted(c for c in children if c.startswith(prefix))
    if not parts:
        log.warning(
            "gee.cells.missing",
            consolidated=consolidated_id,
            parent=parent,
            note="Neither the consolidated asset nor _part_* sub-assets were found.",
        )
        return None

    log.info("gee.cells.merge_parts", consolidated=consolidated_id, parts=len(parts))
    merged = ee.FeatureCollection(parts[0])
    for p in parts[1:]:
        merged = merged.merge(ee.FeatureCollection(p))
    return merged


def require_cells_collection(consolidated_id: str) -> ee.FeatureCollection:
    """Like ``discover_cells_collection`` but raises a clear error if missing.

    Use this from zonal_export — every raster layer needs the cells asset.
    """
    fc = discover_cells_collection(consolidated_id)
    if fc is None:
        raise AssetNotFoundError(
            f"H3 cells GEE asset not found: '{consolidated_id}' "
            "(and no '_part_*' sub-assets either). Run `make push-grid-to-gee` "
            "first; wait for the upload tasks to finish in the Earth Engine "
            "task manager (https://code.earthengine.google.com/tasks)."
        )
    return fc
