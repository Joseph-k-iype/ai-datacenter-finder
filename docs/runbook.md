# Runbook — reproduce end-to-end

Target: a teammate with this repo, a Google Cloud project with Earth
Engine enabled, and Docker, gets to a running Streamlit UI in under
2 hours of wall time. **No GCS bucket required.**

## Prerequisites

- Docker Desktop (Linux/Mac) or Docker Engine
- `uv` (https://github.com/astral-sh/uv) for Python deps
- A Google Cloud project with **Earth Engine API enabled**.
  - Either user Earth Engine credentials (`make gee-auth`) or a
    service-account JSON key downloaded locally.

## Step-by-step

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env
# Set:
#   GEE_SERVICE_ACCOUNT_JSON  → optional path to your SA key (omit for user creds)
#   GEE_PROJECT               → your GCP project ID, not your email address
#   PG_PASSWORD               → a strong password

# If using your own Google login instead of a service account:
make gee-auth                  # choose your Earth Engine-enabled Google account

# 2. Bring up Postgres
make up                       # ~3 min first time (builds h3-pg)
make init-db                  # idempotent
dc health                     # confirm postgis + h3 + h3_postgis

# 3. Install Python deps
make install                  # uv sync

# 4. Build the grid
make build-grid               # ~1 min for res 6+7

# 5. (Optional) Upload custom raster assets for seismic + solar
# These layers reference user-uploaded GEE assets; if absent, the pipeline:
#   - SKIPS seismic (logs a warning; downstream treats all cells as NOT
#     in Zone V — Himalayan cells may surface as eligible)
#   - FALLS BACK to ERA5-derived PVOUT for solar (lower accuracy than GSA)
#
# Recommended uploads (one-time, requires staging files in a GCS bucket
# briefly to call `earthengine upload`):
#   earthengine upload image \
#       --asset_id projects/<your-project>/assets/nasa_gshap_pga \
#       gs://<your-bucket>/gshap_pga.tif
#   earthengine upload image \
#       --asset_id projects/<your-project>/assets/global_solar_atlas_pvout \
#       gs://<your-bucket>/pvout_specific_lta.tif
#
# `make push-grid-to-gee` is no longer required — the default ingest
# pipeline ships cells inline per chunk from Postgres. It's preserved as
# an optional command for users who want the published-asset pattern.

# 6. Ingest everything
make ingest-all               # 30-60 min on first run; seconds on repeats
# IDEMPOTENT by source: each layer checks ingestion_runs for a recent
# successful run (default TTL 24h; see configs/pipeline.yml::ingestion).
# To force a fresh re-ingest:
#   make ingest-fresh-all                    # all sources
#   make ingest-all FRESH=1                  # equivalent
#   dc ingest gee --layer flood --fresh      # single source
#
# First-run cost breakdown:
#   - 7 GEE zonal exports (chunked sync → Parquet cache under data/interim/gee/)
#   - 5 OSM Overpass queries (cached on disk by query hash)
#   - WDPA polygons via paginated GEE getInfo
#   - Static cable landings + metros

# 7. Validate
make validate                 # schema contracts + DQ checks
# Read the output; any errors should be addressed before scoring.

# 8. Compute features  
make compute-features         # ~10 min for res 6 + res 7

# 9. Score
make score-default            # produces scores_res7

# 10. Serve
make serve                    # streamlit on :8501
```

## Common pitfalls

- **"Image asset 'projects/…/nasa_gshap_pga' not found":** you skipped
  step 6. Either upload a GSHAP / BIS PGA raster to that asset path, or
  let the pipeline skip seismic (it now does so cleanly with a warning).
- **"Computed value too large" on a chunk:** raise
  `gee.export.tile_scale` in `configs/pipeline.yml` from 4 to 8 or 16,
  OR lower `gee.export.chunk_size` from 2000 to 1000.
- **A chunk Parquet under `data/interim/gee/<layer>/` looks corrupt:**
  delete it (or pass `--fresh` if/when wired) and re-run the layer; the
  pipeline is resumable per chunk.
- **"Overpass 429" loops:** the public Overpass endpoint rate-limits; the
  client backs off automatically (5 retries up to 2 min). If persistent,
  point `OVERPASS_URL` at a private/Kumi endpoint.
- **"`relation "h3index" does not exist`":** the `h3-pg` extension didn't
  build in the Docker image. Rebuild with `docker compose build --no-cache
  postgis`.
- **"Streamlit map is blank":** you scored before computing features, or
  no cells survived exclusion. Inspect `cell_features_res7` row counts
  in pgAdmin (port 5050).

## Tearing it all down

```bash
make down
docker volume rm ai-data-center_postgres-data     # ⚠ destroys data
```
