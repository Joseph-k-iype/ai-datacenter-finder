# Data Dictionary

Every column in `cell_features_res{7,8}`, with units and provenance.

| Column | Units | Source | Notes |
|---|---|---|---|
| `h3_id` | h3index | — | PK; res-7 or res-8 cell ID |
| `state_code` | text | GADM L1 | 2-letter Indian state code |
| `is_excluded` | bool | derived | true if any hard-exclusion fires |
| `exclusion_reasons` | text[] | derived | machine-readable tags |
| `in_seismic_zone_v` | bool | GSHAP PGA | PGA > 0.36 g |
| `flood_occurrence_pct` | % | JRC GSW | months with surface water, 1984-present |
| `max_slope_deg` | degrees | SRTM 30 m | terrain slope reducer |
| `in_wdpa` | bool | UNEP-WCMC WDPA | any polygon overlap |
| `urban_cover_pct` | % | ESA WorldCover | class 50 fraction |
| `forest_cover_pct` | % | ESA WorldCover | classes 10 + 95 fraction |
| `coast_buffer_km` | km | derived | distance to coastline |
| `nearest_hv_line_km` | km | OSM | nearest 220+ kV line |
| `nearest_hv_line_distinct_subgrid_km` | km | OSM + NetworkX | **Tier-4 metric** |
| `nearest_substation_km` | km | OSM | nearest HV substation |
| `nearest_water_km` | km | OSM | nearest water body |
| `nearest_river_km` | km | OSM | nearest river/stream |
| `nearest_highway_km` | km | OSM | nearest motorway/trunk |
| `nearest_railway_km` | km | OSM | nearest railway |
| `nearest_metro_km` | km | curated | nearest major metro |
| `nearest_cable_landing_km` | km | curated | nearest submarine cable landing |
| `annual_pvout_kwh_per_kwp` | kWh/kWp/yr | GSA/ERA5 | annual c-Si fixed-tilt yield |
| `ghi_kwh_per_m2` | kWh/m²/yr | GSA/ERA5 | annual global horizontal irradiance |
| `mean_temp_c` | °C | ERA5-Land | annual mean 2 m air temperature |
| `mean_rh_pct` | % | ERA5-Land Magnus | annual mean relative humidity |
| `pop_density_per_km2` | persons/km² | WorldPop | 100 m → per-cell density |
| `computed_at` | timestamptz | — | when this row was (re)computed |
| `pipeline_run_id` | uuid | governance | linkable to `ingestion_runs` |
