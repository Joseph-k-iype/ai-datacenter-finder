"""Thin GCS wrapper. Used to read back GEE export files after Export.table.toCloudStorage."""
from __future__ import annotations

import io
from functools import lru_cache

import pandas as pd
from google.cloud import storage

from app.core.config import get_settings


@lru_cache(maxsize=1)
def _client() -> storage.Client:
    settings = get_settings()
    if settings.gee_service_account_json:
        return storage.Client.from_service_account_json(settings.gee_service_account_json)
    return storage.Client()


def _bucket() -> storage.Bucket:
    settings = get_settings()
    if not settings.gcs_bucket:
        raise RuntimeError("GCS_BUCKET not configured (.env)")
    return _client().bucket(settings.gcs_bucket)


def list_blobs(prefix: str) -> list[storage.Blob]:
    """List all blobs under a given prefix."""
    return list(_bucket().list_blobs(prefix=prefix))


def read_csv(blob_path: str) -> pd.DataFrame:
    """Read a CSV blob into a DataFrame."""
    blob = _bucket().blob(blob_path)
    data = blob.download_as_bytes()
    return pd.read_csv(io.BytesIO(data))


def read_csv_glob(prefix: str) -> pd.DataFrame:
    """Concat all CSVs under a prefix (used because GEE exports shard).

    GEE exports a logical CSV as `name-00000.csv`, `name-00001.csv`, etc.
    """
    blobs = [b for b in list_blobs(prefix) if b.name.endswith(".csv")]
    if not blobs:
        raise FileNotFoundError(f"No CSV blobs at gs://{_bucket().name}/{prefix}")
    return pd.concat([read_csv(b.name) for b in blobs], ignore_index=True)


def upload_bytes(blob_path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload bytes; return the gs:// URI."""
    blob = _bucket().blob(blob_path)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{_bucket().name}/{blob_path}"
