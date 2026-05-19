"""GEE asset utility tests (no network — patches ee.data calls)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from ee import ee_exception

from app.ingest.gee._assets import (
    AssetNotFoundError,
    _is_not_found,
    asset_exists,
    discover_cells_collection,
    require_cells_collection,
)


def test_is_not_found_recognizes_canonical_phrases():
    for phrase in (
        "Image asset 'foo' not found",
        "Asset does not exist",
        "Could not be found",
    ):
        assert _is_not_found(Exception(phrase))


def test_is_not_found_does_not_swallow_auth_errors():
    assert not _is_not_found(Exception("PERMISSION_DENIED"))
    assert not _is_not_found(Exception("quota exceeded"))


def test_asset_exists_true_when_get_returns():
    with patch("app.ingest.gee._assets.ee.data.getAsset", return_value={"name": "foo"}):
        assert asset_exists("foo") is True


def test_asset_exists_false_on_not_found():
    with patch(
        "app.ingest.gee._assets.ee.data.getAsset",
        side_effect=ee_exception.EEException("Asset 'foo' not found"),
    ):
        assert asset_exists("foo") is False


def test_asset_exists_reraises_real_errors():
    with patch(
        "app.ingest.gee._assets.ee.data.getAsset",
        side_effect=ee_exception.EEException("PERMISSION_DENIED"),
    ), pytest.raises(ee_exception.EEException):
        asset_exists("foo")


def test_discover_cells_collection_returns_none_when_nothing_exists():
    with patch("app.ingest.gee._assets.asset_exists", return_value=False), patch(
        "app.ingest.gee._assets.list_child_assets", return_value=[]
    ):
        assert discover_cells_collection("projects/x/assets/dc/h3_cells_res7") is None


def test_require_cells_collection_raises_clear_message_when_missing():
    with patch("app.ingest.gee._assets.discover_cells_collection", return_value=None):
        with pytest.raises(AssetNotFoundError) as exc:
            require_cells_collection("projects/x/assets/dc/h3_cells_res7")
        assert "push-grid-to-gee" in str(exc.value)
