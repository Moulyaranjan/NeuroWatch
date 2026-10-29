
from pathlib import Path
import json
from streamlit_autorefresh import st_autorefresh

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
    st.info("No historical records found yet. Live sensor data can still be displayed.")



# ---- NeuroWatch live MPU6050 section ----

st.divider()
st.subheader("Live IMU Sensor")
st.caption("Real-time acceleration and gyroscope readings from the ESP8266.")

# Refresh this Streamlit page every second.
st_autorefresh(interval=1000, key="imu_live_refresh")

live_file = Path(__file__).parent / "data" / "live" / "imu_readings.jsonl"
records = []

if live_file.exists():
    try:
        # Read recent records from the receiver's JSON Lines file.
        with live_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()[-300:]

        for line in lines:
            try:
                record = json.loads(line)
                values = record.get("values", {})

                records.append({
                    "timestamp": record.get("timestamp"),
                    "ax": values.get("ax"),
                    "ay": values.get("ay"),
                    "az": values.get("az"),
                    "gx": values.get("gx"),
                    "gy": values.get("gy"),
                    "gz": values.get("gz"),
                })
            except (json.JSONDecodeError, AttributeError):
                continue

    except OSError as exc:
        st.warning(f"Could not read live sensor file: {exc}")

if records:
    imu = pd.DataFrame(records)

    for col in ["ax", "ay", "az", "gx", "gy", "gz"]:
        imu[col] = pd.to_numeric(imu[col], errors="coerce")

    imu["timestamp"] = pd.to_datetime(
        imu["timestamp"], utc=True, errors="coerce"
    )

    imu = imu.dropna(subset=["timestamp", "ax", "ay", "az", "gx", "gy", "gz"])
    imu = imu.sort_values("timestamp")

    if not imu.empty:
        latest = imu.iloc[-1]

        c1, c2, c3 = st.columns(3)
        c1.metric("Accel X (m/s²)", f"{latest['ax']:.2f}")
        c2.metric("Accel Y (m/s²)", f"{latest['ay']:.2f}")
        c3.metric("Accel Z (m/s²)", f"{latest['az']:.2f}")

        c4, c5, c6 = st.columns(3)
        c4.metric("Gyro X (°/s)", f"{latest['gx']:.2f}")
        c5.metric("Gyro Y (°/s)", f"{latest['gy']:.2f}")
        c6.metric("Gyro Z (°/s)", f"{latest['gz']:.2f}")

        st.caption(
            f"Latest received reading: "
            f"{latest['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

        st.markdown("**Acceleration over time**")
        st.line_chart(
            imu.set_index("timestamp")[["ax", "ay", "az"]],
            y_label="Acceleration (m/s²)",
        )

        st.markdown("**Gyroscope over time**")
        st.line_chart(
            imu.set_index("timestamp")[["gx", "gy", "gz"]],
            y_label="Angular velocity (°/s)",
        )

        st.markdown("**Recent sensor records**")
        st.dataframe(imu.tail(20), use_container_width=True)

        csv_data = imu.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Export live IMU readings as CSV",
            data=csv_data,
            file_name="neurowatch_imu_readings.csv",
            mime="text/csv",
        )
    else:
        st.info("Waiting for valid sensor records.")
else:
    st.info(
        "Waiting for real MPU6050 data. Start the receiver and connect "
        "the ESP8266 to Wi-Fi."
    )

# ---- End live MPU6050 section ----


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
