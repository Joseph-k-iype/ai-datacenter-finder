"""Compare two cells side by side."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui._data import fetch_cell_detail, get_latest_run_id, load_top_sites

st.set_page_config(page_title="Compare • DC Hotspots", layout="wide", page_icon="⚖️")
st.title("⚖️ Compare Two Sites")

run_id = get_latest_run_id()
top = load_top_sites(run_id)

c1, c2 = st.columns(2)
with c1:
    a = st.selectbox(
        "Site A",
        options=top.h3_id.tolist() if not top.empty else [],
        index=0 if not top.empty else None,
        key="a",
    )
with c2:
    b = st.selectbox(
        "Site B",
        options=top.h3_id.tolist() if not top.empty else [],
        index=1 if len(top) > 1 else 0,
        key="b",
    )

if not a or not b or a == b:
    st.info("Pick two distinct sites.")
    st.stop()

da = fetch_cell_detail(a, resolution=7)
db = fetch_cell_detail(b, resolution=7)

cols = [
    "state_code",
    "is_excluded",
    "nearest_hv_line_km",
    "nearest_hv_line_distinct_subgrid_km",
    "nearest_substation_km",
    "nearest_water_km",
    "nearest_highway_km",
    "nearest_metro_km",
    "nearest_cable_landing_km",
    "annual_pvout_kwh_per_kwp",
    "mean_temp_c",
    "mean_rh_pct",
    "pop_density_per_km2",
    "flood_occurrence_pct",
    "max_slope_deg",
    "in_seismic_zone_v",
    "in_wdpa",
]
rows = []
for c in cols:
    rows.append({"feature": c, "A": da["features"].get(c), "B": db["features"].get(c)})
rows.append({"feature": "score", "A": da.get("score"), "B": db.get("score")})

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
