"""Single-page UI for Pan-India AI Data Center site selection.

Built around a kepler.gl map so the entire user journey lives on one
screen:

  • interactive H3 choropleth of scored cells
  • diversity-aware top-N callouts the user can drill into
  • per-site reasoning panel that explains the composite score
  • statistics + per-state aggregates beneath the map

Run with::

    streamlit run app/ui/streamlit_app.py

Backed by FastAPI-free Postgres reads (see ``app/ui/_data.py``). All
heavy queries are wrapped in ``st.cache_data`` so slider interactions
don't re-hit the DB.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from keplergl import KeplerGl
from streamlit_keplergl import keplergl_static

from app.ui._data import (
    fetch_cell_detail,
    get_latest_run_id,
    list_scoring_runs,
    list_states,
    load_hv_lines_geojson,
    load_kpis,
    load_score_histogram,
    load_score_map,
    load_state_stats,
    load_top_sites,
    reasoning_sentence,
    summarise_breakdown,
)

st.set_page_config(
    page_title="Sovereign AI Hotspots — India",
    layout="wide",
    initial_sidebar_state="expanded",
    page_icon="🛰",
)


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
      .stApp { background-color: #0b0f1a; color: #e6e9ef; }
      section[data-testid="stSidebar"] { background-color: #11162a; }
      h1, h2, h3, h4 { color: #f5f7fb; }
      [data-testid="stMetric"] { background: #11162a; border-radius: 10px; padding: 10px 14px; }
      .site-card {
          background: #141a2e;
          border-left: 4px solid #ff5a6e;
          padding: 10px 14px;
          margin-bottom: 8px;
          border-radius: 6px;
      }
      .site-rank { color: #ff5a6e; font-weight: 600; font-size: 0.85rem; }
      .site-score { color: #f5f7fb; font-weight: 600; font-size: 1.15rem; }
      .site-reason { color: #b9c0d4; font-size: 0.85rem; line-height: 1.35; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Data prerequisites
# ---------------------------------------------------------------------------
try:
    run_id = get_latest_run_id()
except Exception as exc:  # noqa: BLE001  — surface any DB error as a UI banner
    st.title("🛰  Sovereign AI Hotspots — Pan-India")
    st.error(
        f"Cannot reach Postgres: `{type(exc).__name__}: {exc}`\n\n"
        "Start the stack first:\n\n"
        "```bash\nmake up && make init-db\n```",
        icon="🛑",
    )
    st.stop()

if not run_id:
    st.title("🛰  Sovereign AI Hotspots — Pan-India")
    st.warning(
        "No completed scoring run found. Run, in order, from the project root:\n\n"
        "```bash\n"
        "make up && make init-db\n"
        "make build-grid && make push-grid-to-gee\n"
        "make ingest-all && make compute-features\n"
        "make score-default\n"
        "```",
        icon="⚠️",
    )
    st.stop()


# ---------------------------------------------------------------------------
# Sidebar — filters + score-run picker
# ---------------------------------------------------------------------------
states_df = list_states()
runs_df = list_scoring_runs()

with st.sidebar:
    st.header("🛰  AI Hotspots")
    st.caption("Tier-4 data-center site selection across India.")

    if not runs_df.empty and len(runs_df) > 1:
        run_choices = runs_df["score_run_id"].astype(str).tolist()
        labels = {
            rid: f"{rid[:8]} · {wts}"
            for rid, wts in zip(runs_df["score_run_id"].astype(str), runs_df["weights_id"], strict=False)
        }
        chosen = st.selectbox(
            "Scoring run",
            options=run_choices,
            index=0,
            format_func=lambda r: labels.get(r, r[:8]),
        )
        run_id = chosen

    state_choice = st.selectbox(
        "Filter by state",
        options=["(All India)"] + states_df["state_name"].tolist(),
        index=0,
    )
    state_code = (
        states_df.loc[states_df["state_name"] == state_choice, "state_code"].iloc[0]
        if state_choice != "(All India)"
        else None
    )

    min_score = st.slider(
        "Minimum score to show",
        0.0, 1.0, 0.0, 0.05,
        help="Hide hexagons scoring below this — useful at pan-India zoom.",
    )
    top_n = st.slider(
        "Top-N callouts (per state)",
        3, 20, 5,
        help="Diversity-aware: callouts are ≥50 km apart.",
    )
    max_hexes = st.slider(
        "Max hexagons to render",
        5_000, 100_000, 30_000, 5_000,
        help=(
            "Browsers choke past ~100k H3 cells. The map keeps the highest-"
            "scoring N hexes under the filters. Raise the minimum-score "
            "slider to see specific bands at full pan-India scale."
        ),
    )
    show_hv = st.checkbox("Overlay HV transmission grid", value=False)


# ---------------------------------------------------------------------------
# KPIs strip
# ---------------------------------------------------------------------------
st.title("🛰  Sovereign AI Hotspots — Pan-India")
st.caption(
    "Composite score across power dual-feed, water, fiber RoW, "
    "solar, climate, and demand-latency. Hex resolution H3-7 (~5 km²)."
)

kpis = load_kpis(run_id)
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Run", str(run_id)[:8])
c2.metric("Cells scored", f"{kpis['cells_scored']:,}")
c3.metric("Mean score", f"{kpis['mean_score']:.3f}")
c4.metric("Top decile", f"{kpis['p90_score']:.3f}")
c5.metric("Excluded share", f"{kpis['excluded_share'] * 100:.1f}%")

st.divider()


# ---------------------------------------------------------------------------
# Map + top-sites panel
# ---------------------------------------------------------------------------
try:
    hex_df = load_score_map(
        run_id, state_code=state_code, min_score=min_score, limit=max_hexes,
    )
    top_df = load_top_sites(run_id, state_code=state_code)
except Exception as exc:  # noqa: BLE001 — render the DB error as a banner, never a stack trace
    st.error(
        f"Query failed: `{type(exc).__name__}: {exc}`\n\n"
        "If the message mentions `/dev/shm` or `DiskFull`, bump the "
        "Postgres container's shared memory (already set to 2 GB in "
        "`docker-compose.yml` — `docker compose up -d --force-recreate "
        "postgis` to apply).",
        icon="🛑",
    )
    st.stop()

if not top_df.empty:
    top_df = top_df.sort_values("score", ascending=False).head(top_n).reset_index(drop=True)

map_col, side_col = st.columns([3, 1])


def _build_kepler(hex_data: pd.DataFrame, sites: pd.DataFrame) -> KeplerGl:
    """Configure the kepler.gl instance for the score choropleth + top callouts.

    Kepler supports H3 cells natively — we pass the ``h3_id`` column and
    let it tessellate. The state filter narrows the payload before we
    upload it to the browser.
    """
    initial_view = {
        "longitude": 78.0,
        "latitude": 22.0,
        "zoom": 3.6 if state_code is None else 5.5,
        "pitch": 0,
        "bearing": 0,
    }
    config = {
        "version": "v1",
        "config": {
            "visState": {
                "filters": [],
                "layers": [
                    {
                        "id": "score_h3",
                        "type": "hexagonId",
                        "config": {
                            "dataId": "scores",
                            "label": "Score",
                            "color": [255, 90, 110],
                            "columns": {"hex_id": "h3_id"},
                            "isVisible": True,
                            "visConfig": {
                                "opacity": 0.7,
                                "colorRange": {
                                    "name": "Sovereign AI",
                                    "type": "sequential",
                                    "category": "Custom",
                                    "colors": [
                                        "#1f2547",
                                        "#3a407a",
                                        "#5e64aa",
                                        "#f5b14b",
                                        "#f57b3a",
                                        "#ff4d4d",
                                    ],
                                },
                                "coverage": 1,
                                "enable3d": False,
                            },
                        },
                        "visualChannels": {
                            "colorField": {"name": "score", "type": "real"},
                            "colorScale": "quantile",
                        },
                    },
                    {
                        "id": "top_sites",
                        "type": "point",
                        "config": {
                            "dataId": "top_sites",
                            "label": "Top sites",
                            "color": [255, 255, 255],
                            "columns": {"lat": "lat", "lng": "lon", "altitude": None},
                            "isVisible": True,
                            "visConfig": {
                                "radius": 14,
                                "fixedRadius": False,
                                "opacity": 0.95,
                                "outline": True,
                                "thickness": 2,
                                "strokeColor": [255, 255, 255],
                                "filled": True,
                            },
                        },
                        "visualChannels": {
                            "colorField": {"name": "score", "type": "real"},
                            "colorScale": "quantile",
                            "sizeField": {"name": "score", "type": "real"},
                            "sizeScale": "linear",
                        },
                    },
                ],
                "interactionConfig": {
                    "tooltip": {
                        "fieldsToShow": {
                            "scores": [
                                {"name": "h3_id"},
                                {"name": "state_code"},
                                {"name": "score"},
                            ],
                            "top_sites": [
                                {"name": "rank"},
                                {"name": "state_code"},
                                {"name": "score"},
                                {"name": "h3_id"},
                            ],
                        },
                        "enabled": True,
                    },
                    "brush": {"enabled": False},
                    "geocoder": {"enabled": False},
                },
            },
            "mapState": initial_view,
            "mapStyle": {
                "styleType": "dark",
            },
        },
    }
    kmap = KeplerGl(height=720, config=config)
    if not hex_data.empty:
        kmap.add_data(
            data=hex_data[["h3_id", "state_code", "score"]].copy(),
            name="scores",
        )
    if not sites.empty:
        kmap.add_data(
            data=sites[["h3_id", "state_code", "rank", "score", "lat", "lon"]].copy(),
            name="top_sites",
        )
    if show_hv:
        gj = load_hv_lines_geojson()
        if gj["features"]:
            kmap.add_data(data=gj, name="hv_grid")
    return kmap


with map_col:
    if hex_df.empty:
        st.info(
            "No scored, non-excluded cells under the current filters. "
            "Lower the min-score slider or pick a different state."
        )
    else:
        kmap = _build_kepler(hex_df, top_df)
        keplergl_static(kmap, height=720, center_map=True)
        st.caption(
            f"Showing **{len(hex_df):,}** scored hexagons. "
            f"Top-{len(top_df)} callouts respect ≥50 km diversity."
            + ("  ·  HV grid overlay on." if show_hv else "")
        )

with side_col:
    st.subheader("🏆  Top sites")
    if top_df.empty:
        st.info("No top sites for this filter.")
    else:
        # Use bracket access throughout — `row.rank` resolves to the
        # pandas Series method `Series.rank` rather than the "rank" column.
        for _, row in top_df.iterrows():
            reason = reasoning_sentence(row["score"], row["breakdown_json"])
            st.markdown(
                f"""
                <div class="site-card">
                  <div class="site-rank">#{int(row["rank"])}  ·  {row["state_code"]}</div>
                  <div class="site-score">{row["score"]:.3f}</div>
                  <div class="site-reason">{reason}</div>
                  <div style="color:#6e7691;font-size:.75rem;margin-top:4px;">
                    {row["h3_id"]}  ·  {row["lat"]:.3f}°N {row["lon"]:.3f}°E
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        site_options = top_df["h3_id"].tolist()
        site_labels = {
            r["h3_id"]: f"#{int(r['rank'])} · {r['state_code']} · {r['score']:.3f}"
            for _, r in top_df.iterrows()
        }
        chosen_h3 = st.selectbox(
            "Drill into a site",
            options=site_options,
            format_func=lambda h: site_labels.get(h, h),
            key="site_picker",
        )
        if chosen_h3:
            detail = fetch_cell_detail(chosen_h3)
            with st.expander("Score breakdown", expanded=True):
                pairs = summarise_breakdown(detail.get("breakdown"))
                if pairs:
                    bd_df = pd.DataFrame(pairs, columns=["criterion", "value"])
                    st.bar_chart(bd_df.set_index("criterion"), height=220)
                feats = detail.get("features", {})
                if feats:
                    keys = [
                        ("nearest_hv_line_km",                  "HV line"),
                        ("nearest_hv_line_distinct_subgrid_km", "Dual-feed HV"),
                        ("nearest_substation_km",               "Substation"),
                        ("nearest_water_km",                    "Water"),
                        ("nearest_highway_km",                  "Highway"),
                        ("nearest_cable_landing_km",            "Cable landing"),
                        ("nearest_metro_km",                    "Metro"),
                        ("annual_pvout_kwh_per_kwp",            "Solar PVOUT"),
                        ("mean_temp_c",                         "Mean temp °C"),
                        ("pop_density_per_km2",                 "Pop density"),
                    ]
                    rows = []
                    for k, label in keys:
                        v = feats.get(k)
                        if v is None:
                            continue
                        rows.append({"feature": label, "value": float(v)})
                    if rows:
                        st.dataframe(
                            pd.DataFrame(rows), hide_index=True,
                            use_container_width=True,
                        )


# ---------------------------------------------------------------------------
# Statistics + analysis row
# ---------------------------------------------------------------------------
st.divider()
st.subheader("📊  Statistics & analysis")

stats_left, stats_right = st.columns([1, 1])

with stats_left:
    st.markdown("**Score distribution**")
    hist = load_score_histogram(run_id, bins=30)
    if hist.empty:
        st.info("No score histogram available.")
    else:
        hist_plot = hist.assign(
            bin_label=lambda d: d["bin_min"].round(2).astype(str)
        ).set_index("bin_label")[["n"]]
        st.bar_chart(hist_plot, height=260)
        st.caption(
            f"Mean **{kpis['mean_score']:.3f}**  ·  "
            f"P90 **{kpis['p90_score']:.3f}**  ·  "
            f"max **{kpis['max_score']:.3f}**"
        )

with stats_right:
    st.markdown("**Per-state score (mean vs. P90)**")
    state_stats = load_state_stats(run_id)
    if state_stats.empty:
        st.info("No per-state aggregates yet.")
    else:
        named = state_stats.merge(states_df, on="state_code", how="left")
        named["label"] = named["state_name"].fillna(named["state_code"])
        chart_df = named.sort_values("mean_score", ascending=False).head(15)
        st.bar_chart(
            chart_df.set_index("label")[["mean_score", "p90_score"]],
            height=260,
        )
        st.caption("Top 15 states by mean composite score.")

st.markdown("**Detailed per-state breakdown**")
if "state_stats" in locals() and not state_stats.empty:
    show = state_stats.merge(states_df, on="state_code", how="left")
    show = show[[
        "state_code", "state_name", "mean_score", "p90_score", "max_score",
        "cells_eligible", "cells_excluded",
    ]].round({"mean_score": 4, "p90_score": 4, "max_score": 4})
    st.dataframe(show, hide_index=True, use_container_width=True)

st.divider()
st.caption(
    "Source of truth: PostGIS · Reasoning: composite of weighted sub-scores · "
    "Map: kepler.gl. Edit weights under ``configs/weights/default.yml`` and "
    "rerun ``make score-default`` to recompute."
)
