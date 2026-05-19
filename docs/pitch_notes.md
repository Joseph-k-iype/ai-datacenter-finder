# Pitch Notes — Talking Points for MeitY / State IT Ministers

## The framing

"India is buying GPUs faster than it's clearing land for them. A single
H100 superpod needs 6–10 MW and a Tier-4 facility around it. State CIOs
keep asking us 'where do we put this?' and the honest answer takes a
geospatial team six weeks per state. This software collapses that to 30
seconds, country-wide."

## The three slides

### 1. The bottleneck isn't compute, it's siting

- 2024–2026: India committed to 10,000+ GPUs under the IndiaAI Mission.
- Tier-4 mandates 99.995% uptime → **dual-redundant power from distinct
  sub-grids**, zero flood exposure, low seismic, dark-fiber adjacency,
  cooling water, ideally captive solar.
- These conditions are satisfied by fewer than ~3% of pan-India hexagons
  by our scoring. **Knowing which 3% is the entire game.**

### 2. What the software does (1 slide demo)

- Live map of India, ~644,000 H3 hexagons, each ~5 km².
- Six sliders: Power Redundancy, Water, Connectivity, Solar, Climate,
  Latency. Move a slider — top-5 sites per state recompute in <500ms.
- Click any hex → full breakdown of why it scored where it did, distances
  to HV lines, water, fiber-RoW, and the **two distinct sub-grids** that
  satisfy Tier-4 dual-feed.

### 3. Why it's defensible

- Built on authoritative sources only — GADM, JRC, ESA, NASA, OSM, WDPA,
  WorldPop, ECMWF ERA5. Every layer is cited, schema-contracted, and
  versioned via a lineage table. No black-box ML claims.
- Recipe is open: weights, transforms, exclusions are all YAML.
- Reproducible: a teammate with the repo and a GCP project can rebuild
  every number in under 2 hours.

## What to ask for after the demo

1. **A pilot state.** Show top-5 sites; have the state's IT team
   ground-truth them. Iterate weights with them in the meeting.
2. **An ingestion of state-specific data** that we don't have country-wide
   yet — state DISCOM transmission GIS, state DPR draft data center
   policies, IIT/IIM faculty / talent pool layers.
3. **A handoff path** to the state's CIO office: containerized stack,
   their POSTGIS, their auth.

## Things NOT to oversell

- This finds **candidate parcels**, not specific land titles. Title work
  is downstream.
- Our seismic data is global GSHAP, not BIS IS-1893. If a state objects,
  we'll ingest the BIS raster within a sprint.
- Submarine cable landings are TeleGeography-sourced; specific operator
  contracts are not modeled (and shouldn't be without their consent).
