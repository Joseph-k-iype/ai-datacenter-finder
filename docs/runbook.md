# Runbook — reproduce end-to-end

Target: a teammate with this repo, a Google Cloud account with Earth
Engine + a GCS bucket, and Docker, gets to a running Streamlit UI in
under 2 hours of wall time (~60 min unattended GEE export wait).

## Prerequisites

- Docker Desktop (Linux/Mac) or Docker Engine
- `uv` (https://github.com/astral-sh/uv) for Python deps
- A Google Cloud project with:
  - Earth Engine API enabled (`earthengine-api`)
  - A GCS bucket the EE service account can write to
  - Service-account JSON key downloaded locally

## Step-by-step

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env
# Set:
#   GEE_SERVICE_ACCOUNT_JSON  → path to your SA key
#   GEE_PROJECT               → your GCP project ID
#   GCS_BUCKET                → your bucket name
#   PG_PASSWORD               → a strong password

# 2. Bring up Postgres
make up                       # ~3 min first time (builds h3-pg)
make init-db                  # idempotent
dc health                     # confirm postgis + h3 + h3_postgis

# 3. Install Python deps
make install                  # uv sync

# 4. Build the grid
make build-grid               # ~1 min for res 6+7

# 5. Publish H3 cells as a GEE asset (one-time)
make push-grid-to-gee         # kicks off async upload tasks
# Wait for tasks to complete (see Earth Engine task manager).
# Then optionally merge sub-assets into a single FeatureCollection
# via `earthengine asset merge` if you want a single asset ID.

# 6. Ingest everything
make ingest-all               # 30-60 min total
# This runs:
#   - 7 GEE zonal exports (poll asynchronously)
#   - 5 OSM Overpass queries (cached on disk)
#   - WDPA polygons via GEE
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

- **"GEE export task FAILED — Computed value too large":** raise
  `tileScale` in `app/ingest/gee/zonal_export.py` from 4 to 8 or 16.
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
