"""Per-cell deep dive: feature table + score radar + nearest-infra list."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from app.ui._data import fetch_cell_detail, get_latest_run_id, load_top_sites

st.set_page_config(page_title="Site Detail • DC Hotspots", layout="wide", page_icon="📍")
st.title("📍 Site Detail")

run_id = get_latest_run_id()
top = load_top_sites(run_id)

c1, c2 = st.columns([2, 1])
with c1:
    h3_id = st.text_input("H3 cell ID (res 7)", "")
with c2:
    if not top.empty:
        pick = st.selectbox(
            "…or pick from top sites",
            options=["(type above)"] + top.h3_id.tolist(),
        )
        if pick != "(type above)" and not h3_id:
            h3_id = pick

if not h3_id:
    st.info("Enter an H3 cell id or pick from the top sites dropdown.")
    st.stop()

detail = fetch_cell_detail(h3_id, resolution=7)
if not detail.get("features"):
    st.error("No cell found with that ID.")
    st.stop()

features = detail["features"]
score = detail.get("score")
breakdown = detail.get("breakdown", {})

col_a, col_b, col_c = st.columns(3)
col_a.metric("Composite score", f"{score:.3f}" if score is not None else "—")
col_b.metric("State", features.get("state_code", "—"))
col_c.metric(
    "Status",
    "EXCLUDED" if features.get("is_excluded") else "Eligible",
    delta=", ".join(features.get("exclusion_reasons") or []) or None,
    delta_color="inverse",
)

st.subheader("Feature snapshot")
fcol1, fcol2 = st.columns(2)
with fcol1:
    st.markdown("**Power**")
    st.metric("Nearest HV line (km)", f"{features.get('nearest_hv_line_km', 0):.2f}")
    st.metric(
        "Nearest distinct-subgrid HV line (km)",
        f"{features.get('nearest_hv_line_distinct_subgrid_km', 0):.2f}",
        help="Tier-4 dual-feed metric. This is a different sub-grid from the nearest line.",
    )
    st.metric("Nearest substation (km)", f"{features.get('nearest_substation_km', 0):.2f}")

    st.markdown("**Connectivity / Cooling**")
    st.metric("Nearest highway (km)", f"{features.get('nearest_highway_km', 0):.2f}")
    st.metric("Nearest water body (km)", f"{features.get('nearest_water_km', 0):.2f}")
    st.metric("Nearest metro (km)", f"{features.get('nearest_metro_km', 0):.2f}")
    st.metric("Nearest cable landing (km)", f"{features.get('nearest_cable_landing_km', 0):.2f}")

with fcol2:
    st.markdown("**Climate / Solar / Density**")
    st.metric("PVOUT (kWh/kWp/yr)", f"{features.get('annual_pvout_kwh_per_kwp', 0):.0f}")
    st.metric("Mean temp (°C)", f"{features.get('mean_temp_c', 0):.1f}")
    st.metric("Mean RH (%)", f"{features.get('mean_rh_pct', 0):.0f}")
    st.metric("Population density (per km²)", f"{features.get('pop_density_per_km2', 0):.0f}")

    st.markdown("**Exclusion signals**")
    st.metric("In Seismic Zone V", "Yes" if features.get("in_seismic_zone_v") else "No")
    st.metric("Flood occurrence (%)", f"{features.get('flood_occurrence_pct', 0):.1f}")
    st.metric("Max slope (°)", f"{features.get('max_slope_deg', 0):.1f}")
    st.metric("In WDPA", "Yes" if features.get("in_wdpa") else "No")

if breakdown:
    st.subheader("Score breakdown")
    fig = go.Figure()
    cats = ["power", "water", "conn", "solar", "clim", "lat"]
    labels = ["Power redundancy", "Water", "Connectivity", "Solar", "Climate", "Latency"]
    values = [breakdown.get(f"sub_{c}", 0.0) for c in cats]
    fig.add_trace(
        go.Scatterpolar(
            r=values + [values[0]],
            theta=labels + [labels[0]],
            fill="toself",
            line=dict(color="#ff5050"),
        )
    )
    fig.update_layout(
        polar=dict(radialaxis=dict(range=[0, 1], visible=True)),
        showlegend=False,
        height=400,
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e6e9ef"),
    )
    st.plotly_chart(fig, use_container_width=True)
