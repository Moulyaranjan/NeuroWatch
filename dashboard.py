
from pathlib import Path

import pandas as pd
import streamlit as st

BASE = Path.home() / "NeuroWatch" / "data" / "processed"

st.set_page_config(
    page_title="NeuroWatch",
    page_icon="📈",
    layout="wide",
)

st.title("NeuroWatch")
st.caption(
    "Personalized movement monitoring | Initial Google Fit data import"
)

steps_path = BASE / "fastrack_steps.csv"
hr_path = BASE / "heart_rate.csv"
daily_path = BASE / "daily_steps.csv"


def load_csv(path):
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception as exc:
        st.warning(f"Could not read {path.name}: {exc}")
        return pd.DataFrame()


steps = load_csv(steps_path)
heart = load_csv(hr_path)
daily = load_csv(daily_path)

st.sidebar.header("Data sources")
st.sidebar.write("Fastrack Astra → Google Fit → Takeout")
st.sidebar.write("Data folder:")
st.sidebar.code(str(BASE))

if steps.empty and heart.empty and daily.empty:
    st.error("No records found. Run convert_fit_data.py first.")
    st.stop()

# Convert values and timestamps carefully.
if not steps.empty and "value" in steps:
    steps["value"] = pd.to_numeric(steps["value"], errors="coerce")
    if "timestamp_utc" in steps:
        steps["timestamp_utc"] = pd.to_datetime(
            steps["timestamp_utc"], utc=True, errors="coerce"
        )

if not heart.empty and "value" in heart:
    heart["value"] = pd.to_numeric(heart["value"], errors="coerce")
    if "timestamp_utc" in heart:
        heart["timestamp_utc"] = pd.to_datetime(
            heart["timestamp_utc"], utc=True, errors="coerce"
        )

if not daily.empty and "steps_from_deltas" in daily:
    daily["steps_from_deltas"] = pd.to_numeric(
        daily["steps_from_deltas"], errors="coerce"
    )

# Header metrics
c1, c2, c3 = st.columns(3)

with c1:
    st.metric("Fastrack step records", f"{len(steps):,}")

with c2:
    st.metric("Heart-rate records", f"{len(heart):,}")

with c3:
    st.metric("Daily summaries", f"{len(daily):,}")

st.divider()

# Daily steps
st.subheader("Daily step activity")

if not daily.empty and {"date", "steps_from_deltas"}.issubset(daily.columns):
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    daily = daily.dropna(subset=["date", "steps_from_deltas"])
    daily = daily.sort_values("date")

    if not daily.empty:
        st.bar_chart(
            daily.set_index("date")["steps_from_deltas"],
            x_label="Date",
            y_label="Step delta sum",
        )
        st.caption(
            "These are sums of exported Fastrack step deltas, "
            "not yet validated against Google Fit daily totals."
        )
    else:
        st.info("No valid daily step summaries to chart.")
else:
    st.info("Daily step data is not available.")

# Heart rate
st.subheader("Heart-rate trend")

if (
    not heart.empty
    and {"timestamp_utc", "value"}.issubset(heart.columns)
):
    hr_chart = heart.dropna(subset=["timestamp_utc", "value"]).copy()
    hr_chart = hr_chart.sort_values("timestamp_utc")

    if not hr_chart.empty:
        st.line_chart(
            hr_chart.set_index("timestamp_utc")["value"],
            x_label="Timestamp (UTC)",
            y_label="Heart rate (bpm, if source confirms)",
        )
        st.caption(
            "Confirm the source and units before interpreting this chart. "
            "It is for research and visualization, not diagnosis."
        )
    else:
        st.info("No valid heart-rate records to chart.")
else:
    st.warning(
        "Heart-rate CSV is empty or lacks expected columns. "
        "Inspect its source JSON before drawing conclusions."
    )

# Inspect actual imported data
with st.expander("View imported step records"):
    if not steps.empty:
        st.dataframe(steps, use_container_width=True)
    else:
        st.write("No step records loaded.")

with st.expander("View imported heart-rate records"):
    if not heart.empty:
        st.dataframe(heart, use_container_width=True)
    else:
        st.write("No heart-rate records loaded.")

with st.expander("Data quality notes"):
    st.write(
        "- Exported records may have gaps or duplicate intervals.\n"
        "- Step-delta sums need validation against daily totals.\n"
        "- Heart-rate source, units, and timestamps need verification.\n"
        "- Google Fit export is a snapshot, not a live connection.\n"
        "- No diagnosis or disease detection is performed."
    )
