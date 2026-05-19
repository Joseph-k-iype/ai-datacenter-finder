"""Weight tuner — recompute scores in-memory from the cached feature frame.

Slider moves should be <500ms on a laptop because we never touch the DB.
"""
from __future__ import annotations

import pandas as pd
import pydeck as pdk
import streamlit as st

from app.core.config import load_weights
from app.scoring.algorithm import score_dataframe
from app.ui._data import load_features_df

st.set_page_config(page_title="Tuner • DC Hotspots", layout="wide", page_icon="🎚")
st.title("🎚  Weight Tuner")

st.caption("Move the sliders — top-5 per state updates live, in-memory, no DB round-trip.")

base = load_weights("configs/weights/default.yml")
defaults = base["weights"]

with st.sidebar:
    st.header("Weights")
    w_power = st.slider("Power redundancy",      0.0, 1.0, float(defaults["power_redundancy"]), 0.01)
    w_water = st.slider("Water proximity",       0.0, 1.0, float(defaults["water_proximity"]),  0.01)
    w_conn  = st.slider("Connectivity (fiber)",  0.0, 1.0, float(defaults["connectivity"]),     0.01)
    w_solar = st.slider("Solar potential",       0.0, 1.0, float(defaults["solar_potential"]),  0.01)
    w_clim  = st.slider("Climate (cool)",        0.0, 1.0, float(defaults["climate"]),          0.01)
    w_lat   = st.slider("Latency to demand",     0.0, 1.0, float(defaults["latency"]),          0.01)

    st.header("Top-N")
    top_n = st.slider("Sites per state", 3, 20, 5)

cfg = dict(base)
cfg["weights"] = {
    "power_redundancy": w_power,
    "water_proximity":  w_water,
    "connectivity":     w_conn,
    "solar_potential":  w_solar,
    "climate":          w_clim,
    "latency":          w_lat,
}

df = load_features_df(resolution=7)
df = df[~df["is_excluded"]]
if df.empty:
    st.warning("No non-excluded cells loaded. Did you compute features?")
    st.stop()

scored = score_dataframe(df, cfg)
# Top-N per state (no diversity here — fast path for live tuning).
top = (
    scored.sort_values(["state_code", "score"], ascending=[True, False])
    .groupby("state_code", as_index=False)
    .head(top_n)
)

# Centroid coords for the top picks. We need lon/lat for the scatter layer.
# Pull from features (the feature load doesn't include centroid; do a small DB hit cached).
@st.cache_data(ttl=300, show_spinner=False)
def centroid_lookup(h3_ids: tuple[str, ...]) -> pd.DataFrame:
    from sqlalchemy import text

    from app.core.db import session_scope

    if not h3_ids:
        return pd.DataFrame(columns=["h3_id", "lon", "lat"])
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT h3_to_string(h3_id) AS h3_id,
                       ST_X(ST_Centroid(geom)) AS lon,
                       ST_Y(ST_Centroid(geom)) AS lat
                FROM dc_india.h3_cells_res7
                WHERE h3_id::text = ANY(:ids)
                """
            ),
            {"ids": list(h3_ids)},
        ).all()
    return pd.DataFrame(rows, columns=["h3_id", "lon", "lat"])


centroids = centroid_lookup(tuple(top.h3_id.tolist()))
top = top.merge(centroids, on="h3_id", how="left")

c1, c2, c3 = st.columns(3)
c1.metric("Non-excluded cells scored", f"{len(scored):,}")
c2.metric("Mean score", f"{scored['score'].mean():.3f}")
c3.metric("Top decile score", f"{scored['score'].quantile(0.9):.3f}")

deck = pdk.Deck(
    layers=[
        pdk.Layer(
            "H3HexagonLayer",
            data=scored[["h3_id", "score"]].to_dict(orient="records"),
            get_hexagon="h3_id",
            pickable=True,
            filled=True,
            get_fill_color="[255*(1-score), 220*score, 80, 160]",
            opacity=0.6,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=top.dropna(subset=["lon", "lat"]).to_dict(orient="records"),
            get_position="[lon, lat]",
            get_fill_color=[255, 70, 70, 230],
            get_radius=5000,
            radius_min_pixels=6,
            radius_max_pixels=24,
        ),
    ],
    initial_view_state=pdk.ViewState(longitude=78, latitude=22, zoom=4),
    map_style="mapbox://styles/mapbox/dark-v10",
)
st.pydeck_chart(deck, use_container_width=True, height=620)

st.subheader("Top sites under current weights")
st.dataframe(
    top[["state_code", "h3_id", "score", "lon", "lat"]].sort_values("score", ascending=False),
    use_container_width=True,
    hide_index=True,
)
