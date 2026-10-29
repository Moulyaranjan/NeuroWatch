from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from neuroml.features import FEATURE_DEFINITIONS, FEATURE_VERSION, summarize_features


AXES = ("ax", "ay", "az", "gx", "gy", "gz")
ACCEL_AXES = ("ax", "ay", "az")
GYRO_AXES = ("gx", "gy", "gz")
MIN_CALIBRATION_SAMPLES = 30
MAX_CALIBRATION_SECONDS = 180
STALE_AFTER_SECONDS = 3.0
MIN_WINDOW_SAMPLES = 5
Z_SCORE_FLOOR = 1e-6
REVIEW_THRESHOLD = 2.5
MARKED_THRESHOLD = 4.0
PERSISTENCE_WINDOWS = 3
PERSISTENT_CHANGE_MIN_SESSIONS = int(os.environ.get("NEUROWATCH_CHANGE_MIN_SESSIONS", "3"))
PERSISTENT_CHANGE_LOOKBACK_SESSIONS = int(os.environ.get("NEUROWATCH_CHANGE_LOOKBACK_SESSIONS", "5"))
PERSISTENT_CHANGE_Z_THRESHOLD = float(os.environ.get("NEUROWATCH_CHANGE_Z_THRESHOLD", "2.0"))
if (
    PERSISTENT_CHANGE_MIN_SESSIONS < 2
    or PERSISTENT_CHANGE_LOOKBACK_SESSIONS < PERSISTENT_CHANGE_MIN_SESSIONS
    or not math.isfinite(PERSISTENT_CHANGE_Z_THRESHOLD)
    or PERSISTENT_CHANGE_Z_THRESHOLD <= 0
):
    raise ValueError("Longitudinal change-detection settings are invalid.")

CALIBRATION_TEST_PROTOCOL = (
    "Personal reference collection for the selected calibration duration; sensor placement and activity protocol "
    "are user-directed and must be kept consistent across sessions."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Optional[datetime] = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _axis_statistics(samples: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for axis in AXES:
        values = [float(sample["values"][axis]) for sample in samples]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        result[axis] = {
            "mean": mean,
            "standard_deviation": math.sqrt(variance),
            "minimum": min(values),
            "maximum": max(values),
            "sample_count": len(values),
        }
    return result


def _magnitude_features(samples: List[Dict[str, Any]], axes: tuple[str, str, str], unit: str) -> Dict[str, Any]:
    magnitudes = [
        math.sqrt(sum(float(sample["values"][axis]) ** 2 for axis in axes))
        for sample in samples
    ]
    mean = sum(magnitudes) / len(magnitudes)
    variance = sum((value - mean) ** 2 for value in magnitudes) / len(magnitudes)
    return {
        "mean": mean,
        "standard_deviation": math.sqrt(variance),
        "minimum": min(magnitudes),
        "maximum": max(magnitudes),
        "range": max(magnitudes) - min(magnitudes),
        "rms": math.sqrt(sum(value * value for value in magnitudes) / len(magnitudes)),
        "sample_count": len(magnitudes),
        "unit": unit,
    }


def _movement_feature_comparisons(
    current: Dict[str, Any],
    baseline: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if current.get("status") != "available" or not baseline or baseline.get("status") != "available":
        return {
            "status": "insufficient_data",
            "features": {},
            "message": current.get("message") or "Comparable feature summaries are unavailable.",
        }

    comparisons: Dict[str, Dict[str, Any]] = {}
    current_statistics = current.get("feature_statistics", {})
    baseline_statistics = baseline.get("feature_statistics", {})
    for name in sorted(set(current_statistics) & set(baseline_statistics)):
        measured = current_statistics[name]
        reference = baseline_statistics[name]
        measured_mean = float(measured["mean"])
        baseline_mean = float(reference["mean"])
        baseline_sd = float(reference["standard_deviation"])
        difference = measured_mean - baseline_mean
        unit = FEATURE_DEFINITIONS.get(name, {}).get("unit", "unit unknown")
        comparisons[name] = {
            "baseline_mean": baseline_mean,
            "baseline_standard_deviation": baseline_sd,
            "session_mean": measured_mean,
            "absolute_difference": abs(difference),
            "signed_difference": difference,
            "relative_change_percent": (
                difference / abs(baseline_mean) * 100
                if abs(baseline_mean) > 1e-12
                else None
            ),
            "standardized_difference": (
                difference / baseline_sd if baseline_sd > 1e-9 else None
            ),
            "unit": unit,
        }
    return {
        "status": "available" if comparisons else "insufficient_data",
        "features": comparisons,
        "message": None if comparisons else "No shared feature values were available for comparison.",
    }


def _longitudinal_change(
    profile: Dict[str, Any],
    session: Dict[str, Any],
    current_summary: Dict[str, Any],
    baseline_summary: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if (
        current_summary.get("status") != "available"
        or not baseline_summary
        or session.get("quality", {}).get("status") != "good"
    ):
        return {
            "state": "insufficient_data",
            "experimental": True,
            "valid_session_count": 0,
            "required_session_count": PERSISTENT_CHANGE_MIN_SESSIONS,
            "lookback_session_count": PERSISTENT_CHANGE_LOOKBACK_SESSIONS,
            "z_threshold": PERSISTENT_CHANGE_Z_THRESHOLD,
            "message": "Valid feature summaries are required for longitudinal change detection.",
        }

    valid_sessions: List[Dict[str, Any]] = []
    for prior in profile["sessions"]:
        summary = prior.get("movement_feature_summary")
        if summary is None and prior.get("samples"):
            summary = summarize_features(prior["samples"])
        if prior.get("quality", {}).get("status") == "good" and summary and summary.get("status") == "available":
            valid_sessions.append({"session": prior, "summary": summary})
    valid_sessions.append({"session": session, "summary": current_summary})
    valid_sessions = valid_sessions[-PERSISTENT_CHANGE_LOOKBACK_SESSIONS:]

    per_feature: Dict[str, int] = {}
    current_comparisons = _movement_feature_comparisons(current_summary, baseline_summary)["features"]
    for feature in current_comparisons:
        baseline_statistic = baseline_summary.get("feature_statistics", {}).get(feature)
        if not baseline_statistic or float(baseline_statistic["standard_deviation"]) <= 1e-9:
            continue
        deviations = 0
        for entry in valid_sessions:
            value = entry["summary"].get("feature_statistics", {}).get(feature)
            if not value:
                continue
            z_score = (
                float(value["mean"]) - float(baseline_statistic["mean"])
            ) / float(baseline_statistic["standard_deviation"])
            if abs(z_score) >= PERSISTENT_CHANGE_Z_THRESHOLD:
                deviations += 1
        if deviations:
            per_feature[feature] = deviations

    persistent = [
        (feature, count)
        for feature, count in per_feature.items()
        if count >= PERSISTENT_CHANGE_MIN_SESSIONS
    ]
    latest_deviation = any(
        comparison.get("standardized_difference") is not None
        and abs(float(comparison["standardized_difference"])) >= PERSISTENT_CHANGE_Z_THRESHOLD
        for comparison in current_comparisons.values()
    )
    if len(valid_sessions) < PERSISTENT_CHANGE_MIN_SESSIONS:
        state = "insufficient_sessions"
    elif persistent:
        state = "persistent_deviation"
    elif latest_deviation:
        state = "temporary_fluctuation"
    else:
        state = "no_persistent_deviation"
    persistent.sort(key=lambda item: (-item[1], item[0]))
    return {
        "state": state,
        "experimental": True,
        "valid_session_count": len(valid_sessions),
        "required_session_count": PERSISTENT_CHANGE_MIN_SESSIONS,
        "lookback_session_count": PERSISTENT_CHANGE_LOOKBACK_SESSIONS,
        "z_threshold": PERSISTENT_CHANGE_Z_THRESHOLD,
        "features": [
            {"feature": feature, "deviating_session_count": count}
            for feature, count in persistent
        ],
        "message": (
            "Experimental repeated-session deviation flag; thresholds are not clinically validated."
        ),
    }


def _session_analysis(session: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    samples = session["samples"]
    chronological_samples = sorted(
        samples,
        key=lambda sample: _parse_timestamp(sample.get("sensor_timestamp") or sample["timestamp"]),
    )
    baseline = session["baseline"]
    expected_units = {
        "ax": {"m/s^2", "m/s²"},
        "ay": {"m/s^2", "m/s²"},
        "az": {"m/s^2", "m/s²"},
        "gx": {"deg/s", "°/s"},
        "gy": {"deg/s", "°/s"},
        "gz": {"deg/s", "°/s"},
    }
    units_verified = bool(samples) and all(
        sample.get("units", {}).get(axis) in valid_units
        for sample in samples
        for axis, valid_units in expected_units.items()
    )
    axes = _axis_statistics(samples) if samples else {}
    acceleration = _magnitude_features(samples, ACCEL_AXES, "m/s^2") if samples else None
    gyroscope = _magnitude_features(samples, GYRO_AXES, "deg/s") if samples else None
    timestamps = [
        _parse_timestamp(sample["timestamp"]).timestamp()
        for sample in chronological_samples
    ]
    intervals = [right - left for left, right in zip(timestamps, timestamps[1:]) if right > left]
    duration = max(0.0, (timestamps[-1] - timestamps[0])) if len(timestamps) > 1 else 0.0
    sample_rate = (len(samples) - 1) / duration if duration > 0 else None
    gaps = [right - left for left, right in zip(timestamps, timestamps[1:]) if right > left]
    median_interval = sorted(intervals)[len(intervals) // 2] if intervals else None
    large_gaps = [gap for gap in gaps if median_interval and gap > max(1.0, median_interval * 3)]

    comparisons: Dict[str, Dict[str, Any]] = {}
    for axis in AXES:
        reference = baseline["axes"][axis]
        measured = axes.get(axis)
        if measured is None:
            comparisons[axis] = {"unit": "m/s^2" if axis in ACCEL_AXES else "deg/s"}
            continue
        difference = measured["mean"] - reference["mean"]
        denominator = abs(reference["mean"])
        baseline_sd = reference["standard_deviation"]
        comparisons[axis] = {
            "baseline_mean": reference["mean"],
            "baseline_standard_deviation": baseline_sd,
            "baseline_minimum": reference["minimum"],
            "baseline_maximum": reference["maximum"],
            "session_mean": measured["mean"],
            "session_standard_deviation": measured["standard_deviation"],
            "session_minimum": measured["minimum"],
            "session_maximum": measured["maximum"],
            "absolute_difference": abs(difference),
            "signed_difference": difference,
            "percentage_difference": abs(difference) / denominator * 100 if denominator > 1e-12 else None,
            "standardized_deviation": difference / baseline_sd if baseline_sd > 1e-9 else None,
            "variability_difference": measured["standard_deviation"] - baseline_sd,
            "variability_percentage_difference": (
                abs(measured["standard_deviation"] - baseline_sd) / baseline_sd * 100
                if baseline_sd > 1e-9 else None
            ),
            "unit": "m/s^2" if axis in ACCEL_AXES else "deg/s",
        }

    magnitude_comparisons: Dict[str, Dict[str, Any]] = {}
    for name, measured in (("acceleration_magnitude", acceleration), ("gyroscope_magnitude", gyroscope)):
        if measured is None:
            magnitude_comparisons[name] = {"session": None, "baseline": baseline["features"][name]}
            continue
        reference = baseline["features"][name]
        difference = measured["mean"] - reference["mean"]
        magnitude_comparisons[name] = {
            "baseline": reference,
            "session": measured,
            "absolute_difference": abs(difference),
            "signed_difference": difference,
            "percentage_difference": abs(difference) / abs(reference["mean"]) * 100 if abs(reference["mean"]) > 1e-12 else None,
            "standardized_deviation": difference / reference["standard_deviation"] if reference["standard_deviation"] > 1e-9 else None,
            "variability_difference": measured["standard_deviation"] - reference["standard_deviation"],
            "variability_percentage_difference": (
                abs(measured["standard_deviation"] - reference["standard_deviation"])
                / reference["standard_deviation"] * 100
                if reference["standard_deviation"] > 1e-9 else None
            ),
        }

    quality = session["quality"]
    movement_summary = summarize_features(session["samples"]) if samples else {
        "version": FEATURE_VERSION,
        "status": "insufficient_data",
        "message": "No accepted sensor samples were recorded.",
        "sampling_information": {"sample_rate_hz": None, "status": "insufficient_data"},
        "window_seconds": 4.0,
        "window_count": 0,
        "values": {},
        "feature_statistics": {},
        "limitations": ["No accepted sensor samples were recorded."],
    }
    baseline_movement_summary = baseline.get("movement_feature_summary")
    movement_comparisons = _movement_feature_comparisons(
        movement_summary,
        baseline_movement_summary,
    )
    longitudinal_change = _longitudinal_change(
        profile,
        session,
        movement_summary,
        baseline_movement_summary,
    )
    model_analysis = session.get("model_analysis", {
        "state": "disabled",
        "model_name": None,
        "model_version": None,
        "output": None,
        "confidence_available": False,
        "data_quality": movement_summary["status"],
        "message": "Movement classification is unavailable until a compatible trained model is installed.",
    })
    limitations = []
    limitations.append(
        "Packet counts include packets routed to this active monitoring session; malformed or stale requests rejected before routing are not included."
    )
    if not samples:
        limitations.append("No eligible sensor samples were recorded in this session.")
    elif not units_verified:
        limitations.append("One or more stored sample units could not be verified; unit labels are withheld.")
    if large_gaps:
        limitations.append(f"{len(large_gaps)} timestamp gap(s) exceeded the observed sampling interval.")
    if sample_rate is None:
        limitations.append("Effective sampling rate is unavailable because fewer than two timestamped samples were recorded.")
    if quality.get("status") != "good":
        limitations.append(f"Session signal quality was {quality.get('status', 'unavailable')}; interpret comparisons cautiously.")
    if movement_summary["status"] != "available":
        limitations.append(
            "ML movement features were unavailable: "
            + str(movement_summary.get("message") or movement_summary["status"])
        )
    limitations.extend(movement_summary.get("limitations", []))
    if model_analysis.get("state") != "available":
        limitations.append(
            "ML classification is unavailable: "
            + str(model_analysis.get("message") or model_analysis.get("state"))
        )
    if longitudinal_change["state"] == "persistent_deviation":
        limitations.append(
            "A repeated experimental feature deviation was observed; it is not evidence of disease progression. "
            "Consider reviewing persistent concerns with a clinician."
        )
    standardized_changes = [
        {
            "feature": axis.upper(),
            "standardized_deviation": comparison["standardized_deviation"],
            "absolute_difference": comparison["absolute_difference"],
            "unit": comparison["unit"],
            "kind": "mean",
        }
        for axis, comparison in comparisons.items()
        if comparison.get("standardized_deviation") is not None
    ]
    variability_changes = [
        {
            "feature": axis.upper(),
            "variability_difference": comparison["variability_difference"],
            "variability_percentage_difference": comparison["variability_percentage_difference"],
            "unit": comparison["unit"],
        }
        for axis, comparison in comparisons.items()
        if comparison.get("variability_percentage_difference") is not None
    ]
    for name, comparison in magnitude_comparisons.items():
        if comparison.get("standardized_deviation") is not None:
            standardized_changes.append({
                "feature": name.replace("_", " "),
                "standardized_deviation": comparison["standardized_deviation"],
                "absolute_difference": comparison["absolute_difference"],
                "unit": comparison["baseline"]["unit"],
                "kind": "mean",
            })
        if comparison.get("variability_percentage_difference") is not None:
            variability_changes.append({
                "feature": name.replace("_", " "),
                "variability_difference": comparison["variability_difference"],
                "variability_percentage_difference": comparison["variability_percentage_difference"],
                "unit": comparison["baseline"]["unit"],
            })
    standardized_changes.sort(key=lambda item: abs(item["standardized_deviation"]), reverse=True)
    variability_changes.sort(key=lambda item: item["variability_percentage_difference"], reverse=True)
    insights = standardized_changes[:3]
    fallback_insights = [
        {
            "feature": name,
            "absolute_difference": value["absolute_difference"],
            "percentage_difference": value["percentage_difference"],
            "unit": value["unit"],
        }
        for name, value in comparisons.items()
        if value.get("standardized_deviation") is None
        and value.get("absolute_difference") is not None
    ]
    fallback_insights.extend(
        {
            "feature": name.replace("_", " "),
            "absolute_difference": value["absolute_difference"],
            "percentage_difference": value["percentage_difference"],
            "unit": value["baseline"]["unit"],
        }
        for name, value in magnitude_comparisons.items()
        if value.get("standardized_deviation") is None
        and value.get("absolute_difference") is not None
    )
    return {
        "profile": {"profile_id": profile["profile_id"], "display_name": profile["display_name"]},
        "session": {
            key: value for key, value in session.items()
            if key not in {"samples", "baseline", "analysis"}
        },
        "baseline": {
            "calibration_id": baseline["calibration_id"],
            "calibrated_at": baseline["calibrated_at"],
            "completed_at": baseline["completed_at"],
            "sample_count": baseline["sample_count"],
            "axes": baseline["axes"],
            "features": baseline["features"],
            "units": baseline["units"],
            "baseline_version": baseline.get("baseline_version", "legacy"),
            "sensor_placement": baseline.get("sensor_placement"),
            "test_protocol": baseline.get("test_protocol"),
            "sampling_information": baseline.get("sampling_information"),
            "movement_feature_summary": baseline_movement_summary,
        },
        "statistics": {"axes": axes, "acceleration_magnitude": acceleration, "gyroscope_magnitude": gyroscope},
        "comparisons": {"axes": comparisons, "magnitudes": magnitude_comparisons},
        "quality": quality,
        "units_verified": units_verified,
        "generated_at": _timestamp(),
        "packet_counts": {
            "received": session["sample_count"] + session["rejected_packet_count"],
            "accepted": session["sample_count"],
            "rejected": session["rejected_packet_count"],
        },
        "effective_sample_rate_hz": sample_rate,
        "movement_feature_summary": movement_summary,
        "movement_feature_comparisons": movement_comparisons,
        "longitudinal_change": longitudinal_change,
        "model_analysis": model_analysis,
        "timestamp_gap_count": len(large_gaps),
        "limitations": limitations,
        "insights": insights,
        "variability_insights": variability_changes[:2],
        "fallback_insights": fallback_insights,
        "deviation_summary": {
            "status": "insufficient_data" if not samples else "experimental_comparison",
            "score": None,
            "severity_thresholds_used": False,
            "explanation": (
                "No accepted sensor readings were recorded, so this session cannot be compared."
                if not samples
                else (
                    f"Compared with the saved calibration, the largest available difference was "
                    f"{insights[0]['feature']} ({insights[0]['kind']}) based on "
                    f"{len(samples)} accepted sensor samples."
                    if insights
                    else (
                        f"Compared with the saved calibration using {len(samples)} accepted sensor samples. "
                        "A standardized difference was unavailable because baseline variability was near zero."
                    )
                )
            ),
            "interpretation": (
                "This experimental comparison describes measured movement-pattern differences, not a medical diagnosis. "
                "It can be affected by sensor placement, activity, calibration quality, sampling rate, and session duration. "
                "A larger difference does not automatically indicate worse health."
            ),
        },
        "events": session["events"],
        "samples": chronological_samples,
    }


def _timestamp_window(samples: List[Dict[str, Any]], duration_seconds: float = 2.0) -> List[Dict[str, Any]]:
    if not samples:
        return []
    end = _parse_timestamp(samples[-1]["timestamp"]).timestamp()
    start = end - duration_seconds
    return [
        sample for sample in samples
        if start <= _parse_timestamp(sample["timestamp"]).timestamp() <= end
    ]


def _window_means(samples: List[Dict[str, Any]], duration_seconds: float = 2.0) -> List[Dict[str, float]]:
    if len(samples) < MIN_WINDOW_SAMPLES:
        return []
    timestamps = [_parse_timestamp(sample["timestamp"]).timestamp() for sample in samples]
    start = timestamps[0]
    end = timestamps[-1]
    windows: List[Dict[str, float]] = []
    while start + duration_seconds <= end:
        points = [
            sample for sample, timestamp in zip(samples, timestamps)
            if start <= timestamp <= start + duration_seconds
        ]
        if len(points) >= MIN_WINDOW_SAMPLES:
            window: Dict[str, float] = {}
            for name, axes in (("acceleration_magnitude", ACCEL_AXES), ("gyroscope_magnitude", GYRO_AXES)):
                magnitudes = [
                    math.sqrt(sum(float(sample["values"][axis]) ** 2 for axis in axes))
                    for sample in points
                ]
                window[name] = sum(magnitudes) / len(magnitudes)
            windows.append(window)
        start += 1.0
    return windows


def _window_statistics(windows: List[Dict[str, float]], feature: str, unit: str) -> Dict[str, Any]:
    values = [window[feature] for window in windows]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "mean": mean,
        "standard_deviation": math.sqrt(variance),
        "minimum": min(values),
        "maximum": max(values),
        "sample_count": len(values),
        "unit": unit,
    }


def _baseline_features(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    accel = _magnitude_features(samples, ACCEL_AXES, "m/s^2")
    gyro = _magnitude_features(samples, GYRO_AXES, "deg/s")
    axis_means = {
        axis: sum(float(sample["values"][axis]) for sample in samples) / len(samples)
        for axis in ACCEL_AXES
    }
    dynamic_magnitudes = [
        math.sqrt(sum((float(sample["values"][axis]) - axis_means[axis]) ** 2 for axis in ACCEL_AXES))
        for sample in samples
    ]
    dynamic_rms = math.sqrt(sum(value * value for value in dynamic_magnitudes) / len(dynamic_magnitudes))

    time_values = [_parse_timestamp(sample["timestamp"]).timestamp() for sample in samples]
    intervals = [right - left for left, right in zip(time_values, time_values[1:]) if right > left]
    sample_rate = (len(time_values) - 1) / (time_values[-1] - time_values[0]) if len(time_values) > 1 and time_values[-1] > time_values[0] else None
    dominant_frequency: Optional[float] = None
    if len(samples) >= 50 and intervals and sample_rate and sample_rate >= 4:
        average_interval = sum(intervals) / len(intervals)
        interval_cv = math.sqrt(sum((delta - average_interval) ** 2 for delta in intervals) / len(intervals)) / average_interval
        duration = time_values[-1] - time_values[0]
        if interval_cv <= 0.1 and duration >= 10:
            centered = [float(sample["values"]["ax"]) - axis_means["ax"] for sample in samples]
            crossings = sum(
                1
                for left, right in zip(centered, centered[1:])
                if (left < 0 <= right) or (left > 0 >= right)
            )
            estimated = crossings / (2 * duration)
            if 0 < estimated <= sample_rate / 2:
                dominant_frequency = estimated

    windows = _window_means(samples)
    window_statistics = {}
    if windows:
        window_statistics = {
            "acceleration_magnitude": _window_statistics(windows, "acceleration_magnitude", "m/s^2"),
            "gyroscope_magnitude": _window_statistics(windows, "gyroscope_magnitude", "deg/s"),
        }

    return {
        "acceleration_magnitude": accel,
        "gyroscope_magnitude": gyro,
        "window_statistics": window_statistics,
        "window_count": len(windows),
        "dynamic_acceleration_magnitude": {
            "rms": dynamic_rms,
            "unit": "m/s^2",
            "interpretation": "Approximate dynamic component after subtracting the calibration mean orientation/gravity vector.",
        },
        "estimated_dominant_oscillation_frequency_hz": dominant_frequency,
        "sample_rate_hz": sample_rate,
        "window_size_seconds": 2.0,
    }


def _live_features(samples: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    window = _timestamp_window(samples)
    if len(window) < MIN_WINDOW_SAMPLES:
        return None
    accel = [
        math.sqrt(sum(float(sample["values"][axis]) ** 2 for axis in ACCEL_AXES))
        for sample in window
    ]
    gyro = [
        math.sqrt(sum(float(sample["values"][axis]) ** 2 for axis in GYRO_AXES))
        for sample in window
    ]
    return {
        "acceleration_magnitude": sum(accel) / len(accel),
        "gyroscope_magnitude": sum(gyro) / len(gyro),
        "sample_count": len(window),
        "window_seconds": (
            _parse_timestamp(window[-1]["timestamp"]) - _parse_timestamp(window[0]["timestamp"])
        ).total_seconds(),
    }


def _signal_quality(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(samples) < MIN_CALIBRATION_SAMPLES:
        return {"status": "insufficient_data", "sample_count": len(samples), "largest_gap_seconds": None}
    times = [_parse_timestamp(sample["timestamp"]).timestamp() for sample in samples]
    intervals = [right - left for left, right in zip(times, times[1:])]
    largest_gap = max(intervals, default=0.0)
    window_count = len(_window_means(samples))
    return {
        "status": "good" if largest_gap <= 5.0 and window_count >= 10 else "poor",
        "sample_count": len(samples),
        "largest_gap_seconds": largest_gap,
        "timestamp_window_count": window_count,
    }


def _window_quality(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(samples) < MIN_WINDOW_SAMPLES:
        return {"status": "insufficient_data", "sample_count": len(samples)}
    times = [_parse_timestamp(sample["timestamp"]).timestamp() for sample in samples]
    intervals = [right - left for left, right in zip(times, times[1:])]
    largest_gap = max(intervals, default=0.0)
    return {
        "status": "good" if largest_gap <= STALE_AFTER_SECONDS else "poor",
        "sample_count": len(samples),
        "largest_gap_seconds": largest_gap,
    }


class ProfileStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.profiles: Dict[str, Dict[str, Any]] = {}
        self.calibration: Optional[Dict[str, Any]] = None
        self.monitoring: Optional[Dict[str, Any]] = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle, parse_constant=self._reject_json_constant)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Could not load profile data from {self.path}: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("profiles", {}), dict):
            raise RuntimeError(f"Profile data in {self.path} has an unsupported structure.")
        self.profiles = payload.get("profiles", {})
        for profile_id, profile in list(self.profiles.items()):
            if (
                not isinstance(profile_id, str)
                or not isinstance(profile, dict)
                or profile.get("profile_id") != profile_id
                or not isinstance(profile.get("calibrations", []), list)
                or not isinstance(profile.get("sessions", []), list)
            ):
                raise RuntimeError(f"Profile data in {self.path} contains an invalid profile record.")
            profile.setdefault("baseline", None)
            profile.setdefault("display_name", profile_id)
            profile.setdefault("created_at", _timestamp())
            profile.setdefault("calibrations", [])
            profile.setdefault("sessions", [])
            try:
                self.validate_profile_id(profile_id)
            except ValueError as exc:
                raise RuntimeError(f"Profile data in {self.path} contains an invalid profile ID.") from exc
            if not isinstance(profile.get("display_name"), str) or not profile["display_name"].strip():
                raise RuntimeError(f"Profile '{profile_id}' in {self.path} has an invalid display name.")
            if not isinstance(profile.get("created_at"), str):
                raise RuntimeError(f"Profile '{profile_id}' in {self.path} has an invalid creation timestamp.")
            baseline = profile.get("baseline")
            if baseline is not None:
                if (
                    not isinstance(baseline, dict)
                    or baseline.get("profile_id") != profile_id
                    or not isinstance(baseline.get("axes"), dict)
                    or not set(AXES).issubset(baseline["axes"])
                    or not isinstance(baseline.get("features"), dict)
                    or not isinstance(baseline.get("quality"), dict)
                    or baseline.get("quality", {}).get("reliable") is not True
                ):
                    raise RuntimeError(f"Profile '{profile_id}' in {self.path} has an incomplete saved baseline.")
                for axis in AXES:
                    statistics = baseline["axes"][axis]
                    if not isinstance(statistics, dict) or any(
                        not isinstance(statistics.get(key), (int, float))
                        or not math.isfinite(statistics[key])
                        for key in ("mean", "standard_deviation", "minimum", "maximum")
                    ):
                        raise RuntimeError(f"Profile '{profile_id}' in {self.path} has invalid baseline statistics.")
                window_statistics = baseline["features"].get("window_statistics")
                if not isinstance(window_statistics, dict) or any(
                    feature not in window_statistics
                    for feature in ("acceleration_magnitude", "gyroscope_magnitude")
                ):
                    raise RuntimeError(f"Profile '{profile_id}' in {self.path} has incomplete movement features.")
            for record in profile["calibrations"] + profile["sessions"]:
                if not isinstance(record, dict) or not isinstance(record.get("samples"), list):
                    raise RuntimeError(f"Profile '{profile_id}' in {self.path} has an incomplete session record.")

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON number '{value}' is not allowed.")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump({"profiles": self.profiles}, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except (OSError, TypeError, ValueError):
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)
            raise

    @staticmethod
    def validate_profile_id(profile_id: str) -> str:
        normalized = profile_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", normalized):
            raise ValueError("Profile ID must be 1-40 letters, numbers, underscores, or hyphens, and start with a letter or number.")
        return normalized

    def list_profiles(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "profile_id": profile["profile_id"],
                    "display_name": profile["display_name"],
                    "created_at": profile["created_at"],
                    "baseline_available": bool(profile.get("baseline")),
                    "last_calibration_at": (profile.get("baseline") or {}).get("completed_at"),
                }
                for profile in sorted(self.profiles.values(), key=lambda item: item["display_name"].casefold())
            ]

    def create_profile(self, display_name: str, profile_id: Optional[str] = None) -> Dict[str, Any]:
        name = display_name.strip()
        if not name or len(name) > 80:
            raise ValueError("Display name must contain 1-80 characters.")
        with self._lock:
            if profile_id is None:
                slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-_").lower()
                base_id = (slug or "person")[:40]
                candidate = base_id
                suffix = 1
                while candidate in self.profiles:
                    suffix += 1
                    candidate = f"{base_id[: max(1, 39 - len(str(suffix)))]}-{suffix}"
                profile_id = candidate
            else:
                profile_id = self.validate_profile_id(profile_id)
            if profile_id in self.profiles:
                raise ValueError("That profile ID already exists.")
            profile = {
                "profile_id": profile_id,
                "display_name": name,
                "created_at": _timestamp(),
                "baseline": None,
                "calibrations": [],
                "sessions": [],
            }
            self.profiles[profile_id] = profile
            self._save()
            return _copy(profile)

    def profile(self, profile_id: str) -> Dict[str, Any]:
        with self._lock:
            profile = self.profiles.get(profile_id)
            if profile is None:
                raise KeyError(f"Profile '{profile_id}' was not found.")
            return _copy(profile)

    def start_calibration(
        self,
        profile_id: str,
        duration_seconds: int,
        sensor_placement: Optional[str] = None,
    ) -> Dict[str, Any]:
        if duration_seconds not in (120, 150, MAX_CALIBRATION_SECONDS):
            raise ValueError("Calibration duration must be 120, 150, or 180 seconds.")
        if sensor_placement not in {None, "unknown", "wrist", "hand", "forearm", "other"}:
            raise ValueError("Sensor placement must be unknown, wrist, hand, forearm, or other.")
        with self._lock:
            self.profile(profile_id)
            if self.monitoring is not None:
                raise ValueError("End the active monitoring session before starting calibration.")
            if self.calibration and self.calibration["state"] in {"collecting", "paused_stale"}:
                raise ValueError("A calibration session is already active.")
            self.calibration = {
                "calibration_id": f"cal-{uuid.uuid4().hex}",
                "profile_id": profile_id,
                "sensor_placement": None if sensor_placement in {None, "unknown"} else sensor_placement,
                "test_protocol": CALIBRATION_TEST_PROTOCOL,
                "duration_seconds": duration_seconds,
                "started_at": _timestamp(),
                "started_monotonic": time.monotonic(),
                "state": "collecting",
                "samples": [],
                "rejected_packets": 0,
                "sensor_name": None,
                "last_packet_monotonic": None,
                "last_invalid_reason": None,
                "result": None,
                "resumable": False,
            }
            return self.calibration_status()

    def _finish_calibration(self, calibration: Dict[str, Any], status: str, now: datetime) -> None:
        samples = calibration["samples"]
        quality = _signal_quality(samples)
        movement_summary = summarize_features(samples) if samples else {
            "version": FEATURE_VERSION,
            "status": "insufficient_data",
            "message": "No accepted sensor samples were recorded.",
            "sampling_information": {"sample_rate_hz": None, "status": "insufficient_data"},
            "window_seconds": 4.0,
            "window_count": 0,
            "values": {},
            "feature_statistics": {},
            "limitations": ["No accepted sensor samples were recorded."],
        }
        elapsed = min(
            calibration["duration_seconds"],
            max(0.0, time.monotonic() - calibration["started_monotonic"]),
        )
        coverage = 0.0
        if len(samples) >= 2:
            coverage = max(
                0.0,
                (_parse_timestamp(samples[-1]["timestamp"]) - _parse_timestamp(samples[0]["timestamp"])).total_seconds(),
            )
        reliable = (
            status == "completed"
            and len(samples) >= MIN_CALIBRATION_SAMPLES
            and elapsed >= calibration["duration_seconds"]
            and coverage >= calibration["duration_seconds"] * 0.7
            and quality["status"] == "good"
            and movement_summary["status"] == "available"
        )
        profile = self.profiles[calibration["profile_id"]]
        calibration_record = {
            "calibration_id": calibration["calibration_id"],
            "profile_id": calibration["profile_id"],
            "started_at": calibration["started_at"],
            "completed_at": _timestamp(now),
            "duration_seconds": calibration["duration_seconds"],
            "elapsed_seconds": elapsed,
            "accepted_sample_count": len(samples),
            "rejected_packet_count": calibration["rejected_packets"],
            "coverage_seconds": coverage,
            "quality": {**quality, "reliable": reliable},
            "state": "completed" if reliable else ("insufficient_data" if status == "completed" else status),
            "incomplete_reason": (
                movement_summary.get("message")
                or "Calibration did not meet duration, coverage, or sensor-quality requirements."
                if status == "completed" and not reliable
                else None
            ),
            "baseline": None,
            "samples": samples,
            "units": {"acceleration": "m/s^2", "gyroscope": "deg/s"},
            "sensor_placement": calibration.get("sensor_placement"),
            "test_protocol": calibration["test_protocol"],
            "sampling_information": movement_summary["sampling_information"],
            "movement_feature_summary": movement_summary,
        }
        if reliable:
            calibration_record["baseline"] = {
                "profile_id": calibration["profile_id"],
                "sensor": calibration["sensor_name"],
                "calibration_id": calibration["calibration_id"],
                "calibrated_at": calibration["started_at"],
                "completed_at": calibration_record["completed_at"],
                "duration_seconds": calibration["duration_seconds"],
                "sample_count": len(samples),
                "baseline_version": "v2",
                "quality": calibration_record["quality"],
                "axes": _axis_statistics(samples),
                "features": _baseline_features(samples),
                "movement_feature_summary": movement_summary,
                "sensor_placement": calibration.get("sensor_placement"),
                "test_protocol": calibration["test_protocol"],
                "sampling_information": movement_summary["sampling_information"],
                "units": calibration_record["units"],
            }
            profile["baseline"] = calibration_record["baseline"]
        profile["calibrations"].append(calibration_record)
        calibration["state"] = calibration_record["state"]
        calibration["ended_at"] = calibration_record["completed_at"]
        calibration["result"] = {
            key: value for key, value in calibration_record.items() if key != "samples"
        }
        calibration["started_monotonic"] = None
        self._save()

    def _refresh_calibration(self) -> None:
        calibration = self.calibration
        if not calibration or calibration["state"] not in {"collecting", "paused_stale"}:
            return
        now_mono = time.monotonic()
        elapsed = now_mono - calibration["started_monotonic"]
        last_packet = calibration["last_packet_monotonic"]
        stale = (
            now_mono - calibration["started_monotonic"] > STALE_AFTER_SECONDS
            if last_packet is None
            else now_mono - last_packet > STALE_AFTER_SECONDS
        )
        if stale:
            calibration["state"] = "paused_stale"
        else:
            calibration["state"] = "collecting"
        if elapsed >= calibration["duration_seconds"]:
            self._finish_calibration(calibration, "completed", _utc_now())

    def calibration_status(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_calibration()
            if not self.calibration:
                return {"state": "idle", "profile_id": None}
            calibration = self.calibration
            elapsed = min(
                calibration["duration_seconds"],
                max(0.0, time.monotonic() - calibration["started_monotonic"])
                if calibration["started_monotonic"] is not None
                else calibration.get("result", {}).get("elapsed_seconds", 0.0),
            )
            return {
                "calibration_id": calibration["calibration_id"],
                "profile_id": calibration["profile_id"],
                "state": calibration["state"],
                "duration_seconds": calibration["duration_seconds"],
                "elapsed_seconds": round(elapsed, 2),
                "remaining_seconds": round(max(0.0, calibration["duration_seconds"] - elapsed), 2),
                "progress_percent": round(min(100.0, elapsed * 100 / calibration["duration_seconds"]), 1),
                "accepted_sample_count": len(calibration["samples"]),
                "rejected_packet_count": calibration["rejected_packets"],
                "last_invalid_reason": calibration["last_invalid_reason"],
                "started_at": calibration["started_at"],
                "ended_at": calibration.get("ended_at"),
                "result": _copy(calibration["result"]),
            }

    def cancel_calibration(self) -> Dict[str, Any]:
        with self._lock:
            calibration = self.calibration
            if not calibration or calibration["state"] not in {"collecting", "paused_stale"}:
                raise ValueError("There is no active calibration to cancel.")
            self._finish_calibration(calibration, "cancelled", _utc_now())
            return self.calibration_status()

    def start_monitoring(self, profile_id: str) -> Dict[str, Any]:
        with self._lock:
            if self.monitoring is not None:
                raise ValueError("A monitoring session is already active.")
            if self.calibration and self.calibration["state"] in {"collecting", "paused_stale"}:
                raise ValueError("Complete or cancel calibration before monitoring.")
            profile = self.profiles.get(profile_id)
            if profile is None:
                raise KeyError(f"Profile '{profile_id}' was not found.")
            baseline = profile.get("baseline")
            if not baseline:
                raise ValueError("This profile has no saved baseline. Complete calibration first.")
            if not baseline.get("quality", {}).get("reliable"):
                raise ValueError("This profile's saved baseline is not reliable enough for monitoring.")
            self.monitoring = {
                "session_id": f"monitor-{uuid.uuid4().hex}",
                "profile_id": profile_id,
                "started_at": _timestamp(),
                "started_monotonic": time.monotonic(),
                "baseline": _copy(baseline),
                "sensor_name": baseline.get("sensor"),
                "samples": [],
                "events": [],
                "window_states": [],
                "comparison_history": [],
                "active_event_state": None,
                "rejected_packets": 0,
                "last_packet_monotonic": None,
            }
            return self.monitoring_status()

    def end_monitoring(
        self,
        model_analyzer: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if self.monitoring is None:
                raise ValueError("There is no active profile monitoring session.")
            session = self.monitoring
            ended_at = _timestamp()
            record = {
                "session_id": session["session_id"],
                "profile_id": session["profile_id"],
                "started_at": session["started_at"],
                "ended_at": ended_at,
                "duration_seconds": round(max(0.0, time.monotonic() - session["started_monotonic"]), 2),
                "sample_count": len(session["samples"]),
                "rejected_packet_count": session["rejected_packets"],
                "baseline_calibration_id": session["baseline"]["calibration_id"],
                "features": _live_features(session["samples"][-10:]) or {},
                "quality": _signal_quality(session["samples"]),
                "movement_feature_summary": summarize_features(session["samples"])
                if session["samples"]
                else {"version": FEATURE_VERSION, "status": "insufficient_data", "values": {}, "feature_statistics": {}},
                "events": session["events"],
                "samples": session["samples"],
                "baseline": _copy(session["baseline"]),
                "units": {"acceleration": "m/s^2", "gyroscope": "deg/s"},
            }
            if model_analyzer is not None:
                record["model_analysis"] = model_analyzer(record["samples"])
            profile = self.profiles[session["profile_id"]]
            report = _session_analysis(record, profile)
            record["analysis"] = {key: value for key, value in report.items() if key != "samples"}
            profile["sessions"].append(record)
            self.monitoring = None
            self._save()
            report["longitudinal_history"] = self._trends(profile)
            report["session"] = {
                key: value for key, value in record.items()
                if key not in {"samples", "baseline", "analysis"}
            }
            report.update({
                key: value for key, value in record.items()
                if key in {
                    "session_id", "profile_id", "started_at", "ended_at",
                    "duration_seconds", "sample_count", "rejected_packet_count",
                    "baseline_calibration_id", "features", "units",
                    "movement_feature_summary", "model_analysis",
                }
            })
            return _copy(report)

    def completed_session(self, session_id: str, profile_id: Optional[str] = None) -> Dict[str, Any]:
        if not re.fullmatch(r"monitor-[a-f0-9]{32}", session_id):
            raise KeyError(f"Completed session '{session_id}' was not found.")
        with self._lock:
            profiles = [self.profiles[profile_id]] if profile_id in self.profiles else (
                [] if profile_id is not None else list(self.profiles.values())
            )
            for profile in profiles:
                for session in profile["sessions"]:
                    if session.get("session_id") == session_id:
                        if session.get("profile_id") != profile["profile_id"]:
                            raise RuntimeError("Stored session profile association is inconsistent.")
                        archived = _copy(session)
                        if not archived.get("baseline"):
                            calibration = next(
                                (
                                    entry for entry in profile["calibrations"]
                                    if entry.get("calibration_id") == archived.get("baseline_calibration_id")
                                ),
                                None,
                            )
                            archived["baseline"] = _copy(calibration.get("baseline")) if calibration else None
                        if not archived.get("baseline"):
                            raise KeyError(f"Completed session '{session_id}' has no retained baseline report data.")
                        cached_analysis = session.get("analysis")
                        if not cached_analysis or any(
                            key not in cached_analysis
                            for key in ("deviation_summary", "units_verified", "variability_insights")
                        ):
                            report = _session_analysis(archived, profile)
                        else:
                            report = _copy(cached_analysis)
                        report["longitudinal_history"] = self._trends(profile)
                        report["samples"] = sorted(
                            _copy(session["samples"]),
                            key=lambda sample: _parse_timestamp(sample.get("sensor_timestamp") or sample["timestamp"]),
                        )
                        return report
        raise KeyError(f"Completed session '{session_id}' was not found.")

    def completed_session_history(self, profile_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            profiles = [self.profiles[profile_id]] if profile_id in self.profiles else (
                [] if profile_id is not None else list(self.profiles.values())
            )
            sessions = []
            for profile in profiles:
                for session in profile["sessions"]:
                    retained_baseline = session.get("baseline") or any(
                        calibration.get("calibration_id") == session.get("baseline_calibration_id")
                        and calibration.get("baseline")
                        for calibration in profile["calibrations"]
                    )
                    sessions.append({
                        "session_id": session["session_id"],
                        "profile_id": profile["profile_id"],
                        "display_name": profile["display_name"],
                        "started_at": session["started_at"],
                        "ended_at": session["ended_at"],
                        "duration_seconds": session["duration_seconds"],
                        "sample_count": session["sample_count"],
                        "baseline_calibration_id": session["baseline_calibration_id"],
                        "report_available": bool(retained_baseline and isinstance(session.get("samples"), list)),
                    })
            return sorted(sessions, key=lambda entry: entry["ended_at"], reverse=True)

    def monitoring_status(self) -> Dict[str, Any]:
        with self._lock:
            session = self.monitoring
            if not session:
                return {"state": "idle", "profile_id": None, "session_id": None}
            age = None if session["last_packet_monotonic"] is None else time.monotonic() - session["last_packet_monotonic"]
            state = "active" if age is not None and age <= STALE_AFTER_SECONDS else (
                "waiting_for_sensor"
                if age is None and time.monotonic() - session["started_monotonic"] <= STALE_AFTER_SECONDS
                else "stale"
            )
            return {
                "state": state,
                "profile_id": session["profile_id"],
                "session_id": session["session_id"],
                "started_at": session["started_at"],
                "duration_seconds": round(max(0.0, time.monotonic() - session["started_monotonic"]), 2),
                "sample_count": len(session["samples"]),
                "baseline_calibration_id": session["baseline"]["calibration_id"],
                "last_packet_age_seconds": None if age is None else round(age, 2),
                "events": _copy(session["events"][-10:]),
            }

    def packet_context(self) -> Optional[Dict[str, str]]:
        with self._lock:
            if self.calibration and self.calibration["state"] in {"collecting", "paused_stale"}:
                return {
                    "profile_id": self.calibration["profile_id"],
                    "context_id": self.calibration["calibration_id"],
                    "context_type": "calibration",
                }
            if self.monitoring:
                return {
                    "profile_id": self.monitoring["profile_id"],
                    "context_id": self.monitoring["session_id"],
                    "context_type": "monitoring",
                }
            return None

    def record_packet(
        self,
        event: Dict[str, Any],
        received_at: datetime,
        received_monotonic: float,
        eligible: bool,
        invalid_reason: Optional[str] = None,
    ) -> None:
        with self._lock:
            if self.calibration and self.calibration["state"] in {"collecting", "paused_stale"}:
                self._refresh_calibration()
                calibration = self.calibration
                if calibration and calibration["state"] in {"collecting", "paused_stale"}:
                    if not eligible:
                        calibration["rejected_packets"] += 1
                        calibration["last_invalid_reason"] = invalid_reason or "Packet did not meet calibration requirements."
                        return
                    if calibration["sensor_name"] is not None and event.get("sensor") != calibration["sensor_name"]:
                        calibration["rejected_packets"] += 1
                        calibration["last_invalid_reason"] = "Packet came from a different sensor than this calibration."
                        return
                    sample = self._sample(event, received_at)
                    calibration["sensor_name"] = event.get("sensor")
                    calibration["samples"].append(sample)
                    calibration["last_packet_monotonic"] = received_monotonic
                    calibration["last_invalid_reason"] = None
                    calibration["state"] = "collecting"
                    return

            session = self.monitoring
            if session is None:
                return
            if not eligible:
                session["rejected_packets"] += 1
                return
            if event.get("sensor") != session["sensor_name"]:
                session["rejected_packets"] += 1
                return
            sample = self._sample(event, received_at)
            session["samples"].append(sample)
            session["last_packet_monotonic"] = received_monotonic
            self._update_comparison(session, sample)

    @staticmethod
    def _sample(event: Dict[str, Any], received_at: datetime) -> Dict[str, Any]:
        return {
            "timestamp": _timestamp(received_at),
            "sensor_timestamp": event.get("timestamp"),
            "sensor": event.get("sensor"),
            "sequence_number": event.get("sequence_number"),
            "values": {axis: float(event["values"][axis]) for axis in AXES},
            "units": {key: event.get("units", {}).get(key) for key in AXES},
            "sensor_freshness_ms": event.get("sensor_freshness_ms"),
            "source": event.get("source"),
            "temperature_c": event.get("temperature_c"),
        }

    def _update_comparison(self, session: Dict[str, Any], sample: Dict[str, Any]) -> None:
        window = _timestamp_window(session["samples"])
        features = _live_features(window)
        if features is None:
            return
        baseline_features = session["baseline"]["features"]
        reference_features = baseline_features.get("window_statistics") or baseline_features
        deviations: Dict[str, float] = {}
        quality = _window_quality(window)
        if quality["status"] != "good":
            session["window_states"] = []
            session["active_event_state"] = None
            session["latest_comparison"] = {
                "state": "poor_signal_quality",
                "score": None,
                "feature_deviations_z": {},
                "features": features,
                "baseline": {},
                "signal_quality": quality,
                "interpretation": "Signal timing/quality is not adequate for movement comparison.",
            }
            return
        for feature in ("acceleration_magnitude", "gyroscope_magnitude"):
            reference = reference_features[feature]
            floor = max(Z_SCORE_FLOOR, abs(reference["mean"]) * 1e-6)
            deviations[feature] = (features[feature] - reference["mean"]) / max(reference["standard_deviation"], floor)
        score = (abs(deviations["acceleration_magnitude"]) + abs(deviations["gyroscope_magnitude"])) / 2
        severity = "marked deviation" if score >= MARKED_THRESHOLD else "review" if score >= REVIEW_THRESHOLD else "normal"
        session["window_states"].append(severity)
        session["window_states"] = session["window_states"][-5:]
        sustained = severity != "normal" and session["window_states"].count(severity) >= PERSISTENCE_WINDOWS
        if sustained and session["active_event_state"] != severity:
            session["events"].append({
                "timestamp": sample["timestamp"],
                "severity": severity,
                "score": score,
                "feature_deviations_z": deviations,
                "signal_quality": quality,
                "description": "Experimental engineering score for change in measured movement patterns versus the fixed personal baseline; not a medical probability.",
            })
            session["active_event_state"] = severity
        elif not sustained:
            session["active_event_state"] = None
        session["latest_comparison"] = {
            "state": severity,
            "sustained": sustained,
            "persistence_window_count": session["window_states"].count(severity),
            "score": score,
            "feature_deviations_z": deviations,
            "features": features,
            "baseline": {
                feature: {
                    "mean": reference_features[feature]["mean"],
                    "standard_deviation": reference_features[feature]["standard_deviation"],
                    "unit": reference_features[feature]["unit"],
                }
                for feature in ("acceleration_magnitude", "gyroscope_magnitude")
            },
            "signal_quality": quality,
            "thresholds": {"review": REVIEW_THRESHOLD, "marked_deviation": MARKED_THRESHOLD},
            "persistence": {"windows": PERSISTENCE_WINDOWS, "window_length_samples": 10},
            "interpretation": "Experimental engineering score, not a medical probability or diagnosis.",
        }
        session["comparison_history"].append({
            "timestamp": sample["timestamp"],
            "features": features,
            "state": session["latest_comparison"]["state"],
            "score": score,
        })
        session["comparison_history"] = session["comparison_history"][-300:]

    def dashboard_state(self, profile_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            self._refresh_calibration()
            selected = profile_id
            if selected is None and self.monitoring:
                selected = self.monitoring["profile_id"]
            if (
                selected is None
                and self.calibration
                and self.calibration["state"] in {"collecting", "paused_stale"}
            ):
                selected = self.calibration["profile_id"]
            profile = self.profiles.get(selected) if selected else None
            session = self.monitoring if self.monitoring and self.monitoring["profile_id"] == selected else None
            if session:
                context_id = session["session_id"]
                context_type = "monitoring"
                source_samples = session["samples"]
            elif (
                profile
                and self.calibration
                and self.calibration["profile_id"] == selected
                and self.calibration["state"] in {"collecting", "paused_stale"}
            ):
                context_id = self.calibration["calibration_id"]
                context_type = "calibration"
                source_samples = self.calibration["samples"]
            elif profile:
                completed = [
                    (record.get("completed_at") or record.get("ended_at") or "", record, kind)
                    for kind, records in (("calibration", profile["calibrations"]), ("monitoring", profile["sessions"]))
                    for record in records
                ]
                if completed:
                    _, record, context_type = max(completed, key=lambda entry: entry[0])
                    context_id = record.get("calibration_id") or record.get("session_id")
                    source_samples = record.get("samples", [])
                else:
                    context_id = ""
                    context_type = ""
                    source_samples = []
            else:
                context_id = ""
                context_type = ""
                source_samples = []
            live_samples = [
                {
                    **sample,
                    "profile_id": selected,
                    "session_id": context_id,
                    "session_type": context_type,
                }
                for sample in source_samples[-100:]
            ]
            comparison = session.get("latest_comparison") if session else None
            monitoring_status = self.monitoring_status()
            if session and monitoring_status["state"] == "stale":
                comparison = {
                    "state": "poor_signal_quality",
                    "score": None,
                    "feature_deviations_z": {},
                    "features": None,
                    "baseline": {},
                    "signal_quality": {"status": "stale"},
                    "interpretation": "Sensor data is stale; the last comparison is not shown as a current result.",
                }
            if session and comparison is None:
                comparison = {
                    "state": "insufficient_data",
                    "score": None,
                    "feature_deviations_z": {},
                    "features": None,
                    "baseline": {},
                    "signal_quality": {"status": "insufficient_data"},
                    "sample_count": len(live_samples),
                    "interpretation": "Waiting for enough valid packets for a live comparison.",
                }
            return {
                "profiles": self.list_profiles(),
                "selected_profile": None if profile is None else {
                    "profile_id": profile["profile_id"],
                    "display_name": profile["display_name"],
                },
                "baseline": None if profile is None else _copy(profile.get("baseline")),
                "calibration": self.calibration_status(),
                "monitoring": monitoring_status,
                "profile_samples": _copy(live_samples),
                "comparison": _copy(comparison),
                "comparison_history": _copy(session["comparison_history"][-100:]) if session else [],
                "trends": self._trends(profile) if profile else [],
                "calibration_history": self._calibration_history(profile) if profile else [],
                "recent_events": self._recent_events(profile, session) if profile else [],
            }

    @staticmethod
    def _trends(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        trends = []
        for session in profile["sessions"]:
            baseline = session.get("baseline")
            if baseline is None:
                calibration = next(
                    (
                        entry for entry in profile["calibrations"]
                        if entry.get("calibration_id") == session.get("baseline_calibration_id")
                    ),
                    None,
                )
                baseline = calibration.get("baseline") if calibration else None
            if not baseline or not session.get("samples"):
                continue
            analysis = session.get("analysis")
            statistics = analysis.get("statistics", {}) if analysis else {}
            if not statistics.get("acceleration_magnitude") or not statistics.get("gyroscope_magnitude"):
                analysis = _session_analysis({**session, "baseline": baseline}, profile)
            statistics = analysis.get("statistics", {})
            acceleration = statistics.get("acceleration_magnitude")
            gyroscope = statistics.get("gyroscope_magnitude")
            if not acceleration or not gyroscope:
                continue
            trends.append({
                "session_id": session["session_id"],
                "session_type": "monitoring",
                "started_at": session["started_at"],
                "ended_at": session["ended_at"],
                "sample_count": session["sample_count"],
                "features": {
                    "acceleration_magnitude": acceleration["mean"],
                    "gyroscope_magnitude": gyroscope["mean"],
                    "acceleration_magnitude_unit": acceleration["unit"],
                    "gyroscope_magnitude_unit": gyroscope["unit"],
                    "acceleration_axes": {
                        axis: statistics["axes"][axis]["mean"]
                        for axis in ACCEL_AXES
                    },
                    "gyroscope_axes": {
                        axis: statistics["axes"][axis]["mean"]
                        for axis in GYRO_AXES
                    },
                },
                "movement_feature_summary": session.get("movement_feature_summary")
                or summarize_features(session["samples"]),
                "movement_feature_comparisons": (analysis or {}).get(
                    "movement_feature_comparisons", {}
                ),
                "longitudinal_change": (analysis or {}).get("longitudinal_change", {}),
                "model_analysis": session.get("model_analysis", {}),
                "quality": session["quality"],
                "profile_id": profile["profile_id"],
            })
        return sorted(trends, key=lambda item: (item["ended_at"], item["session_id"]))

    @staticmethod
    def _calibration_history(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "calibration_id": calibration["calibration_id"],
                "profile_id": profile["profile_id"],
                "started_at": calibration["started_at"],
                "completed_at": calibration["completed_at"],
                "duration_seconds": calibration["duration_seconds"],
                "accepted_sample_count": calibration["accepted_sample_count"],
                "state": calibration["state"],
                "quality": calibration["quality"],
                "baseline_available": calibration.get("baseline") is not None,
                "sensor_placement": calibration.get("sensor_placement"),
                "incomplete_reason": calibration.get("incomplete_reason"),
                "movement_feature_summary": calibration.get("movement_feature_summary"),
            }
            for calibration in profile["calibrations"]
        ]

    @staticmethod
    def _recent_events(profile: Dict[str, Any], active_session: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        events = [
            {
                **event,
                "session_id": session["session_id"],
                "profile_id": profile["profile_id"],
            }
            for session in profile["sessions"]
            for event in session.get("events", [])
        ]
        if active_session:
            events.extend({
                **event,
                "session_id": active_session["session_id"],
                "profile_id": profile["profile_id"],
            } for event in active_session["events"])
        return sorted(events, key=lambda event: event["timestamp"])[-20:]

    def history(self, profile_id: str) -> Dict[str, Any]:
        profile = self.profile(profile_id)
        return {
            "profile_id": profile_id,
            "calibrations": [
                {key: value for key, value in calibration.items() if key != "samples"}
                for calibration in profile["calibrations"]
            ],
            "sessions": [
                {key: value for key, value in session.items() if key != "samples"}
                for session in profile["sessions"]
            ],
        }

    def export_csv(self, profile_id: str, session_id: Optional[str] = None) -> str:
        import csv
        import io

        profile = self.profile(profile_id)
        rows: List[Dict[str, Any]] = []
        for calibration in profile["calibrations"]:
            if session_id and calibration["calibration_id"] != session_id:
                continue
            baseline = calibration.get("baseline") or {}
            axes = baseline.get("axes", {})
            features = baseline.get("features", {})
            accel = features.get("acceleration_magnitude", {})
            gyro = features.get("gyroscope_magnitude", {})
            first_sample = (calibration.get("samples") or [{}])[0]
            rows.append({
                "record_type": "calibration_summary",
                "profile_id": profile_id,
                "session_id": calibration["calibration_id"],
                "timestamp": calibration["completed_at"],
                "started_at": calibration["started_at"],
                "duration_seconds": calibration["duration_seconds"],
                "sample_count": calibration["accepted_sample_count"],
                "calibration_state": calibration.get("state"),
                "quality_status": calibration.get("quality", {}).get("status"),
                "accel_mag_mean": accel.get("mean"),
                "accel_mag_std": accel.get("standard_deviation"),
                "accel_mag_rms": accel.get("rms"),
                "accel_mag_range": accel.get("range"),
                "gyro_mag_mean": gyro.get("mean"),
                "gyro_mag_std": gyro.get("standard_deviation"),
                "gyro_mag_rms": gyro.get("rms"),
                "gyro_mag_range": gyro.get("range"),
                **{
                    f"mean_{axis}": axes.get(axis, {}).get("mean")
                    for axis in AXES
                },
                **{
                    f"std_{axis}": axes.get(axis, {}).get("standard_deviation")
                    for axis in AXES
                },
                **{f"unit_{axis}": first_sample.get("units", {}).get(axis) for axis in AXES},
            })
            for sample in calibration.get("samples", []):
                rows.append({
                    "record_type": "calibration_sample",
                    "profile_id": profile_id,
                    "session_id": calibration["calibration_id"],
                    "timestamp": sample.get("timestamp"),
                    "sensor": sample.get("sensor"),
                    "sequence_number": sample.get("sequence_number"),
                    **sample.get("values", {}),
                    **{f"unit_{axis}": sample.get("units", {}).get(axis) for axis in AXES},
                    "calibration_state": calibration.get("state"),
                    "quality_status": calibration.get("quality", {}).get("status"),
                })
        for session in profile["sessions"]:
            if session_id and session["session_id"] != session_id:
                continue
            session_features = session.get("features", {})
            rows.append({
                "record_type": "monitoring_summary",
                "profile_id": profile_id,
                "session_id": session["session_id"],
                "timestamp": session["ended_at"],
                "started_at": session["started_at"],
                "duration_seconds": session["duration_seconds"],
                "sample_count": session["sample_count"],
                "baseline_calibration_id": session["baseline_calibration_id"],
                "quality_status": session.get("quality", {}).get("status"),
                "accel_mag_mean": session_features.get("acceleration_magnitude"),
                "gyro_mag_mean": session_features.get("gyroscope_magnitude"),
                "unit_ax": "m/s^2",
                "unit_ay": "m/s^2",
                "unit_az": "m/s^2",
                "unit_gx": "deg/s",
                "unit_gy": "deg/s",
                "unit_gz": "deg/s",
            })
            for sample in session.get("samples", []):
                rows.append({
                    "record_type": "monitoring_sample",
                    "profile_id": profile_id,
                    "session_id": session["session_id"],
                    "timestamp": sample.get("timestamp"),
                    "sensor": sample.get("sensor"),
                    "sequence_number": sample.get("sequence_number"),
                    **sample.get("values", {}),
                    **{f"unit_{axis}": sample.get("units", {}).get(axis) for axis in AXES},
                    "calibration_state": "",
                    "quality_status": session.get("quality", {}).get("status"),
                })
            for event in session.get("events", []):
                deviations = event.get("feature_deviations_z", {})
                rows.append({
                    "record_type": "monitoring_event",
                    "profile_id": profile_id,
                    "session_id": session["session_id"],
                    "timestamp": event.get("timestamp"),
                    "severity": event.get("severity"),
                    "score": event.get("score"),
                    "accel_z_score": deviations.get("acceleration_magnitude"),
                    "gyro_z_score": deviations.get("gyroscope_magnitude"),
                    "quality_status": event.get("signal_quality", {}).get("status"),
                })
        if session_id and not rows:
            raise KeyError(f"Session or calibration '{session_id}' was not found for profile '{profile_id}'.")
        output = io.StringIO()
        fieldnames = [
            "record_type", "profile_id", "session_id", "timestamp", "started_at", "duration_seconds",
            "sample_count", "sensor", "sequence_number", *AXES, *(f"unit_{axis}" for axis in AXES),
            *(f"mean_{axis}" for axis in AXES), *(f"std_{axis}" for axis in AXES),
            "accel_mag_mean", "accel_mag_std", "accel_mag_rms", "accel_mag_range",
            "gyro_mag_mean", "gyro_mag_std", "gyro_mag_rms", "gyro_mag_range",
            "calibration_state", "baseline_calibration_id", "quality_status", "severity", "score",
            "accel_z_score", "gyro_z_score",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()
