
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path.home() / "NeuroWatch"
FIT_DIR = BASE / "data" / "raw" / "Fit"
OUT_DIR = BASE / "data" / "processed"

OUT_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {path.name}: {exc}")
        return None


def timestamp_from_ns(value):
    if value is None:
        return ""
    try:
        # Google Fit timestamps are nanoseconds since Unix epoch.
        seconds = int(value) / 1_000_000_000
        return datetime.fromtimestamp(
            seconds, tz=timezone.utc
        ).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def extract_value(point):
    """Read intVal or fpVal from a Google Fit data point."""
    fit_values = point.get("fitValue", [])
    if not fit_values:
        return None

    value_obj = fit_values[0].get("value", {})
    if "intVal" in value_obj:
        return value_obj["intVal"]
    if "fpVal" in value_obj:
        return value_obj["fpVal"]
    return None


def extract_records(path, metric):
    data = read_json(path)
    if not isinstance(data, dict):
        return []

    points = data.get("Data Points", [])
    if not isinstance(points, list):
        return []

    records = []

    for point in points:
        if not isinstance(point, dict):
            continue

        value = extract_value(point)
        if value is None:
            continue

        start_ns = point.get("startTimeNanos")
        end_ns = point.get("endTimeNanos", start_ns)

        records.append({
            "timestamp_utc": timestamp_from_ns(start_ns),
            "end_timestamp_utc": timestamp_from_ns(end_ns),
            "metric": metric,
            "value": value,
            "source_file": path.name,
            "source_id": point.get("originDataSourceId", ""),
        })

    return records


def write_csv(path, records, columns):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)


def main():
    print("NEUROWATCH DATA CONVERTER")
    print("Input:", FIT_DIR)
    print("Output:", OUT_DIR)

    if not FIT_DIR.exists():
        print("ERROR: Fit folder not found.")
        return

    all_data = FIT_DIR / "All data"
    if not all_data.exists():
        print("ERROR: 'All data' folder not found.")
        return

    json_files = list(all_data.glob("*.json"))
    print("JSON files in All data:", len(json_files))

    # Prefer the watch's own step source to avoid double-counting
    # the merged Google Fit step stream.
    step_files = [
        p for p in json_files
        if "step_count.delta" in p.name.lower()
        and "titan.fastrack" in p.name.lower()
        and "derived_" not in p.name.lower()
    ]

    if not step_files:
        print("WARNING: No Fastrack-specific step file found.")
        print("Check filenames in the All data folder.")
        step_records = []
    else:
        step_records = []
        for path in step_files:
            step_records.extend(extract_records(path, "steps"))

    # Prefer Fastrack-origin heart-rate data when present.
    hr_candidates = [
        p for p in json_files
        if "heart_rate.bpm" in p.name.lower()
        and "derived_" not in p.name.lower()
    ]

    fastrack_hr = [
        p for p in hr_candidates
        if "titan.fastrack" in p.name.lower()
    ]

    if fastrack_hr:
        hr_files = fastrack_hr
        print("Using Fastrack-specific heart-rate files.")
    else:
        hr_files = hr_candidates
        print(
            "WARNING: No Fastrack-specific heart-rate filename found. "
            "Using raw heart-rate files; source needs verification."
        )

    hr_records = []
    for path in hr_files:
        hr_records.extend(extract_records(path, "heart_rate_bpm"))

    # Remove exact duplicate records within each metric.
    def deduplicate(records):
        seen = set()
        result = []
        for r in records:
            key = (
                r["timestamp_utc"],
                r["end_timestamp_utc"],
                r["metric"],
                r["value"],
                r["source_file"],
            )
            if key not in seen:
                seen.add(key)
                result.append(r)
        return sorted(
            result,
            key=lambda r: (
                r["timestamp_utc"],
                r["source_file"]
            )
        )

    step_records = deduplicate(step_records)
    hr_records = deduplicate(hr_records)

    columns = [
        "timestamp_utc",
        "end_timestamp_utc",
        "metric",
        "value",
        "source_file",
        "source_id",
    ]

    write_csv(OUT_DIR / "fastrack_steps.csv", step_records, columns)
    write_csv(OUT_DIR / "heart_rate.csv", hr_records, columns)

    # Sum step deltas by local calendar date.
    # This summary uses only Fastrack-specific step records.
    daily = defaultdict(int)
    for r in step_records:
        if not r["timestamp_utc"]:
            continue
        dt = datetime.fromisoformat(r["timestamp_utc"])
        local_date = dt.astimezone().date().isoformat()
        daily[local_date] += int(r["value"])

    daily_rows = [
        {"date": date, "steps_from_deltas": total}
        for date, total in sorted(daily.items())
    ]

    write_csv(
        OUT_DIR / "daily_steps.csv",
        daily_rows,
        ["date", "steps_from_deltas"],
    )

    print("\nCONVERSION COMPLETE")
    print("Fastrack step records:", len(step_records))
    print("Heart-rate records:", len(hr_records))
    print("Daily summaries:", len(daily_rows))
    print("\nCreated files:")
    print(OUT_DIR / "fastrack_steps.csv")
    print(OUT_DIR / "heart_rate.csv")
    print(OUT_DIR / "daily_steps.csv")

    if not hr_records:
        print(
            "\nNo heart-rate records extracted. "
            "We need to inspect the heart-rate filenames and structure."
        )


if __name__ == "__main__":
    main()
