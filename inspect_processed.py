
from pathlib import Path
import pandas as pd

folder = Path("data/processed")

# STEP DATA
steps_path = folder / "fastrack_steps.csv"
daily_path = folder / "daily_steps.csv"
hr_path = folder / "heart_rate.csv"

steps = pd.read_csv(steps_path)
steps["value"] = pd.to_numeric(steps["value"], errors="coerce")

print("\n========== STEP RECORD DIAGNOSTICS ==========")
print("Total records:", len(steps))
print("Zero-valued records:", (steps["value"] == 0).sum())
print("Positive-valued records:", (steps["value"] > 0).sum())
print("Negative-valued records:", (steps["value"] < 0).sum())
print("Unique step values:", steps["value"].nunique())

print("\nLargest 10 step records:")
print(
    steps.nlargest(10, "value")[
        ["timestamp_utc", "end_timestamp_utc", "value"]
    ].to_string(index=False)
)

daily = pd.read_csv(daily_path)
daily["steps_from_deltas"] = pd.to_numeric(
    daily["steps_from_deltas"], errors="coerce"
)

print("\n========== DAILY SUMMARY DIAGNOSTICS ==========")
print("Days in file:", len(daily))
print("Zero-total days:", (daily["steps_from_deltas"] == 0).sum())
print("Positive-total days:", (daily["steps_from_deltas"] > 0).sum())

print("\nAll daily summaries:")
print(daily.to_string(index=False))

heart = pd.read_csv(hr_path)
heart["value"] = pd.to_numeric(heart["value"], errors="coerce")

print("\n========== HEART-RATE DIAGNOSTICS ==========")
print("Total records:", len(heart))
print("Unique timestamps:", heart["timestamp_utc"].nunique())
print("Missing values:", heart["value"].isna().sum())
print("Values below 40:", (heart["value"] < 40).sum())
print("Values above 220:", (heart["value"] > 220).sum())
print("\nHeart-rate percentiles:")
print(heart["value"].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]))
