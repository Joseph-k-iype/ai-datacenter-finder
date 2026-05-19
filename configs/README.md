# `configs/` — All tunables

Nothing in the Python code is "magic-numbered". Every threshold, weight,
asset ID, voltage cutoff, and date range lives in one of these YAML
files.

## Files

| File | What it controls |
|---|---|
| `pipeline.yml` | bbox, H3 resolutions, GEE asset IDs, raster scales, voltage thresholds, topology snap radii, GEE export timing, DQ row-count bounds |
| `exclusions.yml` | hard-mask thresholds (PGA, flood %, slope °, landcover %) |
| `sources.yml` | URLs, Overpass queries, curated cable-landings, metros |
| `weights/default.yml` | balanced scoring weights + transform params |
| `weights/tier4_focused.yml` | heavy power-redundancy weighting |
| `weights/green_focused.yml` | heavy solar / climate weighting |

## `pipeline.yml`

```yaml
india:
  bbox: [68.0, 6.5, 97.5, 37.5]      # west, south, east, north (EPSG:4326)
  gadm_url: "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_IND_1.json"

resolutions:
  exclusion: 6        # coarse first sweep
  scoring:   7        # main scoring resolution
  drilldown: 8        # only for top-N res-7 cells

h3_avg_area_km2:      # canonical from H3 docs
  6: 36.129
  7: 5.161
  8: 0.737

gee:
  india_h3_asset: "projects/{project}/assets/dc_india/h3_cells_res7"
  layers: { … }       # asset IDs per layer
  scale_m: { … }      # reducer scale in meters
  windows:            # date / year ranges per layer
    climate: { start_date: "2023-01-01", end_date: "2024-01-01" }
    population: { year: 2020, country_iso3: "IND" }
  export:
    file_format: CSV
    chunk_size: 5000
    poll_seconds: 30
    timeout_minutes: 90
    tile_scale: 4     # raise to 8 or 16 on "Computed value too large"

osm:
  hv_voltage_kv: [220, 400, 765, 800]   # lowest entry is inclusion threshold

power_topology:
  parallel_cluster_eps_deg: 0.0045     # ~500 m at the equator (DBSCAN ε)
  substation_snap_km: 1.0
  substation_snap_deg: 0.009            # spatial-index pre-filter radius

scoring:
  default_weights_file: "configs/weights/default.yml"
  diversity_min_km: 50.0
  top_n_per_state: 5
  distance_cap_km: 500.0                # KNN cap

dq_row_count_bounds:                     # validator warnings outside these
  h3_cells_res6:    [60000, 200000]
  h3_cells_res7:    [400000, 1500000]
  raw_power_lines:  [5000, 200000]
  raw_highways:     [1000, 100000]
  raw_water_bodies: [1000, 500000]
```

## `exclusions.yml`

Every key here is "if value > threshold, exclude." A cell is excluded if
ANY rule fires.

```yaml
seismic.exclude_pga_g_gt: 0.36            # NASA GSHAP PGA — Zone V boundary
flood.exclude_occurrence_pct_gt: 25.0     # JRC GSW occurrence
slope.exclude_max_slope_deg_gt: 10.0      # SRTM-derived
landcover:
  exclude_urban_pct_gt:   70.0
  exclude_forest_pct_gt:  60.0
  exclude_wetland_pct_gt: 30.0
  exclude_water_pct_gt:   50.0
protected_areas.exclude_in_wdpa: true
coastline.exclude_within_km: 0.5          # NOT YET WIRED — needs MoEFCC coastline data
```

## `sources.yml`

The single source of truth for external URLs and queries. Adding a new
data layer means:
1. Add the URL / query / list here.
2. Add a Pandera contract in `app/governance/contracts.py`.
3. Write the adapter in `app/ingest/`.

Hard-coded items intentionally kept inline:

- `cable_landings_in` — curated list of submarine cable landing stations
  (operators, cables, coordinates). Update when TeleGeography reports a
  new landing.
- `metros_in` — major demand-anchor metros. Update from Census of India.

## `weights/`

Three presets demonstrate the "switch your pitch posture" story:

| File | Posture |
|---|---|
| `default.yml` | Balanced 0.30/0.15/0.15/0.10/0.10/0.20 |
| `tier4_focused.yml` | 0.50 to power redundancy (Uptime audit first) |
| `green_focused.yml` | 0.30 to solar potential (net-zero campus narrative) |

Each file has two sections:

```yaml
weights:                              # the linear-combination weights
  power_redundancy:   0.30
  water_proximity:    0.15
  …

transforms:                            # parameters of the smooth normalizers
  power:
    primary_decay_km:   15.0
    secondary_decay_km: 30.0
    secondary_weight:   0.4
  water:
    decay_km: 10.0
  connectivity:
    highway_decay_km: 5.0
  solar:
    pvout_center: 1400
    pvout_spread: 200
  climate:
    temp_low_c:  18
    temp_high_c: 38
  latency:
    metro_decay_km:  100.0
    cable_decay_km:  200.0
    cable_weight:    0.5
```

The Streamlit Tuner page binds sliders to the `weights:` section and
recomputes scores live; `transforms:` parameters are tweakable per
preset but not exposed in the UI for now.

## Adding a new weights preset

1. Copy `default.yml` to `weights/<your_id>.yml`.
2. Edit the `id:` and `description:` fields.
3. Adjust weights + transform params.
4. Run: `dc score --weights configs/weights/<your_id>.yml --res 7`.
5. Compare in the UI via the Lineage page (each scoring run is logged).
