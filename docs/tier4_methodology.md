# Tier-4 Methodology

The Uptime Institute's Tier-4 certification requires **99.995% uptime**
(≈26 min/year of permitted downtime). For an AI data-center campus at
100–300 MW scale, this translates into hard infrastructure requirements
that our scoring algorithm operationalizes at country scale.

## Hard exclusions

A cell is disqualified if **any** of:

| Criterion | Threshold | Source |
|---|---|---|
| Seismic Zone V | PGA > 0.36 g | NASA SEDAC / BIS IS-1893 |
| Recurring flood | JRC GSW occurrence > 25 % | JRC Global Surface Water |
| Slope | max > 10° | SRTM 30m derived |
| Land cover: urban | > 70 % | ESA WorldCover |
| Land cover: forest | > 60 % | ESA WorldCover |
| Land cover: wetland | > 30 % | ESA WorldCover |
| Land cover: water | > 50 % | ESA WorldCover |
| Protected area | any WDPA polygon overlap | UNEP-WCMC WDPA |
| Coastline | within 500 m | (CRZ-I proxy) |

## Weighted scoring (post-exclusion)

Each criterion is transformed to `[0, 1]` via a monotonic smooth function,
then weighted-summed.

| Criterion | Transform | Default weight |
|---|---|---|
| Power redundancy | composite (see below) | 0.30 |
| Water proximity | `exp(-d/10km)` | 0.15 |
| Connectivity (highway) | `exp(-d/5km)` | 0.15 |
| Solar potential | `sigmoid((PVOUT-1400)/200)` | 0.10 |
| Climate (cool) | linear `[18°C, 38°C]` inverted | 0.10 |
| Latency | `exp(-d_metro/100km) + 0.5·exp(-d_cable/200km)` | 0.20 |

## The Tier-4 differentiator: dual-feed redundancy

Standard "nearest HV line" is not Tier-4 honest. Two parallel circuits on
the same tower string are one feed — a single tornado or right-of-way
fire takes them both. We instead compute:

```
power_redundancy =
    0.6 · exp(-nearest_hv_line_km / 15)
  + 0.4 · exp(-nearest_hv_line_distinct_subgrid_km / 30)
```

Where `nearest_hv_line_distinct_subgrid_km` is the nearest HV line whose
**topological sub-grid component** differs from the nearest-overall line's.
Sub-grid components are computed by:

1. `ST_ClusterDBSCAN` on `raw_power_lines` with ε=500 m merges parallel
   circuits sharing right-of-way into a single cluster.
2. A NetworkX graph is built where substations are nodes and clusters are
   edges. `networkx.connected_components(G)` labels each cluster with its
   independent-feed component ID.
3. PostGIS `LATERAL DISTINCT ON (subgrid_component)` queries the 2nd
   nearest cluster from a *different* component.

This makes the score Tier-4 honest. Without it, "two nearby 220 kV lines"
would inflate the score even on a single right-of-way.

## Diversity in top-N

When picking top-5 per state, candidates within 50 km of an already-chosen
site are skipped. This prevents one excellent corridor (e.g., NH-65 near
Pune) from dominating a state's recommendations.

## Caveats

- Sub-grid topology depends on OSM substation tagging coverage. Where OSM
  is sparse, we fall back to cluster_id as the component label (logged in
  `ingestion_runs.notes`).
- PVOUT in the ERA5-Land fallback uses a 0.75 conversion factor (typical
  c-Si fixed-tilt yield in Indian climate). Replace with a uploaded
  Global Solar Atlas asset for production.
- Seismic uses GSHAP PGA, not the BIS IS-1893 zones directly. We
  approximate Zone V by PGA > 0.36 g. State officials may want the BIS
  raster ingested separately.
