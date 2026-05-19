"""Lineage page — ingestion runs, schema contracts, DLQ volumes."""
from __future__ import annotations

import streamlit as st

from app.ui._data import get_dlq_counts, get_ingestion_runs, get_schema_contracts

st.set_page_config(page_title="Lineage • DC Hotspots", layout="wide", page_icon="📚")
st.title("📚 Pipeline Lineage & Health")

st.subheader("Ingestion runs (latest 200)")
runs = get_ingestion_runs()
if runs.empty:
    st.info("No ingestion runs logged yet.")
else:
    failed = runs[runs.status == "failed"]
    if not failed.empty:
        st.error(f"{len(failed)} failed runs — see table below.")
    st.dataframe(runs, use_container_width=True, hide_index=True)

st.subheader("Schema contracts")
contracts = get_schema_contracts()
if contracts.empty:
    st.warning(
        "No contracts registered. Run `dc validate` to register and check schemas."
    )
else:
    st.dataframe(contracts, use_container_width=True, hide_index=True)

st.subheader("Dead-letter queue volumes (by source)")
dlq = get_dlq_counts()
if dlq.empty:
    st.success("DLQ is empty — every ingested row passed validation.")
else:
    st.dataframe(dlq, use_container_width=True, hide_index=True)
