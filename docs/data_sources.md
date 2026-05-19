# Data Sources (citations + licenses + refresh cadence)

All canonical paths live in `configs/sources.yml`. Edit that file (not code)
to change an asset ID, URL, or Overpass query.

## Boundary

| Source | License | Refresh |
|---|---|---|
| GADM v4.1 (L1, India) | Free for academic & non-commercial use | static |

## Hazards (GEE)

| Layer | Asset | License | Native scale |
|---|---|---|---|
| NASA SEDAC GSHAP PGA | (uploaded asset) | CC BY 4.0 (NASA) | 30 arc-sec |
| JRC Global Surface Water v1.4 | `JRC/GSW1_4/GlobalSurfaceWater` | © European Union 1995-2024 | 30 m |
| SRTM 30m | `USGS/SRTMGL1_003` | Public domain | 30 m |
| ESA WorldCover 2021 v200 | `ESA/WorldCover/v200` | CC BY 4.0 (ESA) | 10 m |
| WDPA (UNEP-WCMC) | `WCMC/WDPA/current/polygons` | CC BY 4.0 attribution | monthly |

## Infrastructure (OSM Overpass)

OpenStreetMap — © OpenStreetMap contributors, ODbL.

| Layer | Tag | Notes |
|---|---|---|
| HV transmission | `power=line, voltage>=220kV` | voltage parsed for `220/400/765/800` kV |
| Substations | `power=substation, voltage>=220kV` | |
| Highways | `highway=motorway\|trunk` | proxy for fiber RoW |
| Railways | `railway=rail` | secondary fiber RoW |
| Water | `natural=water OR waterway=river` | cooling proximity |

## Solar / Climate / Population (GEE)

| Layer | Asset | License |
|---|---|---|
| Global Solar Atlas PVOUT | (uploaded asset) or ERA5-Land fallback | World Bank Group, CC BY 4.0 |
| ERA5-Land | `ECMWF/ERA5_LAND/MONTHLY` | © Copernicus |
| WorldPop 100m | `WorldPop/GP/100m/pop` | CC BY 4.0 (University of Southampton) |

## Curated lists (static)

Maintained in `configs/sources.yml`. Sources:
- **Cable landings:** TeleGeography submarine cable map (publicly viewable);
  coordinates approximate landing station positions per ICPC reporting.
- **Metros:** Census of India 2011 + GHS-POP / WorldPop 2024 updates.

## Refresh policy

| Cadence | Layers |
|---|---|
| Static (rebuild manually) | boundary, cable landings, metros |
| Monthly | WDPA, OSM (Overpass cache invalidated by `make ingest-all`) |
| Annual | ESA WorldCover, ERA5-Land aggregation window, WorldPop |
| As released | NASA SEDAC PGA, JRC GSW (v1.x), SRTM, Global Solar Atlas |
