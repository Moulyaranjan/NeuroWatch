import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from movement import ProfileStore, _longitudinal_change
from neuroml.features import (
    AXES,
    FEATURE_NAMES,
    FEATURE_VERSION,
    FeatureValidationError,
    extract_feature_windows,
    model_feature_vector,
    sampling_information,
    summarize_features,
)
from neuroml.service import MovementModelService
from neuroml.train import train_model


UNITS = {
    "ax": "m/s^2",
    "ay": "m/s^2",
    "az": "m/s^2",
    "gx": "deg/s",
    "gy": "deg/s",
    "gz": "deg/s",
}


def build_samples(
    sample_rate_hz=50,
    duration_seconds=8,
    acceleration_scale=0.4,
    start_time=None,
):
    start_time = start_time or datetime(2026, 10, 29, tzinfo=timezone.utc)
    sample_count = int(round(sample_rate_hz * duration_seconds)) + 1
    result = []
    for index in range(sample_count):
        relative_time = index / sample_rate_hz
        tremor = math.sin(2 * math.pi * 5.0 * relative_time)
        slow = math.sin(2 * math.pi * 0.7 * relative_time)
        result.append({
            "timestamp": (start_time + timedelta(seconds=relative_time)).isoformat().replace("+00:00", "Z"),
            "source": "real",
            "units": dict(UNITS),
            "values": {
                "ax": acceleration_scale * tremor + 0.04 * slow,
                "ay": 0.25 * math.sin(2 * math.pi * 4.5 * relative_time),
                "az": 9.8 + 0.1 * tremor,
                "gx": 8.0 * tremor + 0.5 * slow,
                "gy": 3.0 * math.sin(2 * math.pi * 6.0 * relative_time),
                "gz": 2.0 * math.sin(2 * math.pi * 3.5 * relative_time),
            },
        })
    return result


def test_feature_extraction_and_training_preprocessing_are_consistent():
    samples = build_samples(duration_seconds=4)
    rows, information = extract_feature_windows(samples)
    assert information["sample_rate_hz"] == pytest.approx(50)
    assert len(rows) == 1
    features = rows[0]
    assert features["ax_dominant_frequency_hz"] == pytest.approx(5, abs=0.25)
    assert features["gx_dominant_frequency_hz"] == pytest.approx(5, abs=0.25)
    assert features["ax_variance"] == pytest.approx(0.4**2 / 2, rel=0.03)
    assert len(model_feature_vector(features)) == len(FEATURE_NAMES)
    assert all(np.isfinite(model_feature_vector(features)))
    repeated_rows, _ = extract_feature_windows(samples)
    assert repeated_rows == rows


def test_low_rate_data_keeps_time_features_but_marks_frequency_unavailable():
    result = summarize_features(build_samples(sample_rate_hz=5, duration_seconds=8))
    assert result["status"] == "available"
    assert result["frequency_features_available"] is False
    assert result["values"]["gx_rms"] is not None
    assert result["values"]["gx_dominant_frequency_hz"] is None
    assert "below 25 Hz" in result["limitations"][0]


@pytest.mark.parametrize("failure", ["missing_axis", "missing_units", "nonfinite", "duplicate_time"])
def test_invalid_samples_are_rejected_without_imputation(failure):
    samples = build_samples(duration_seconds=4)
    if failure == "missing_axis":
        del samples[0]["values"]["ax"]
    elif failure == "missing_units":
        samples[0]["units"]["gx"] = None
    elif failure == "nonfinite":
        samples[0]["values"]["az"] = float("nan")
    else:
        samples[1]["timestamp"] = samples[0]["timestamp"]

    assert sampling_information(samples)["status"] == "invalid_data"
    with pytest.raises(FeatureValidationError):
        extract_feature_windows(samples)


def _create_test_service(path: Path) -> MovementModelService:
    estimator = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("classifier", RandomForestClassifier(n_estimators=8, random_state=3)),
    ])
    x = np.vstack([
        np.zeros(len(FEATURE_NAMES)),
        np.ones(len(FEATURE_NAMES)),
        np.full(len(FEATURE_NAMES), 0.2),
        np.full(len(FEATURE_NAMES), 0.8),
    ])
    y = np.asarray(["control", "pattern", "control", "pattern"])
    estimator.fit(x, y)
    metadata = {
        "training_status": "trained",
        "model_name": "Test-only deterministic estimator",
        "model_version": "test",
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "classes": ["control", "pattern"],
    }
    path.mkdir(parents=True, exist_ok=True)
    joblib.dump({"estimator": estimator, "metadata": metadata}, path / "movement-rf-test.joblib")
    (path / "current-model.json").write_text(
        json.dumps({"artifact": "movement-rf-test.joblib"}),
        encoding="utf-8",
    )
    return MovementModelService(path)


def test_inference_returns_only_supported_class_and_never_a_probability(tmp_path):
    service = _create_test_service(tmp_path / "model")
    result = service.analyze(build_samples(duration_seconds=8))
    assert result["state"] == "available"
    assert result["output"]["class"] in {"control", "pattern"}
    assert result["confidence_available"] is False
    assert "confidence" not in result
    assert result["window_count"] >= 1


def test_inference_handles_low_rate_short_input_and_invalid_input(tmp_path):
    service = _create_test_service(tmp_path / "model")
    low_rate = service.analyze(build_samples(sample_rate_hz=5, duration_seconds=8))
    assert low_rate["state"] == "incompatible_sampling_rate"
    assert low_rate["output"] is None

    short = service.analyze(build_samples(sample_rate_hz=50, duration_seconds=1))
    assert short["state"] == "insufficient_data"
    assert short["output"] is None

    invalid = build_samples(duration_seconds=4)
    invalid[0]["values"]["gx"] = float("nan")
    invalid_result = service.analyze(invalid)
    assert invalid_result["state"] == "invalid_data"
    assert invalid_result["output"] is None


def test_model_disabled_and_artifact_load_failure_are_explicit(tmp_path):
    missing = MovementModelService(tmp_path / "absent")
    assert missing.status()["state"] == "disabled"
    assert missing.analyze([])["state"] == "insufficient_data"

    broken_path = tmp_path / "broken"
    broken_path.mkdir()
    (broken_path / "current-model.json").write_text(
        json.dumps({"artifact": "movement-rf-broken.joblib"}),
        encoding="utf-8",
    )
    (broken_path / "movement-rf-broken.joblib").write_text("not a model", encoding="utf-8")
    broken = MovementModelService(broken_path)
    assert broken.status()["state"] == "model_loading_error"
    assert broken.analyze(build_samples(duration_seconds=8))["output"] is None


def test_persistent_change_requires_repeated_valid_session_deviations():
    baseline = {
        "status": "available",
        "feature_statistics": {
            "ax_rms": {"mean": 0.1, "standard_deviation": 0.01},
        },
    }

    def completed_session(value):
        return {
            "quality": {"status": "good"},
            "movement_feature_summary": {
                "status": "available",
                "feature_statistics": {
                    "ax_rms": {"mean": value, "standard_deviation": 0.005},
                },
            },
        }

    repeated_deviation = {
        "sessions": [completed_session(0.13), completed_session(0.13)],
    }
    persistent = _longitudinal_change(
        repeated_deviation,
        completed_session(0.13),
        completed_session(0.13)["movement_feature_summary"],
        baseline,
    )
    assert persistent["state"] == "persistent_deviation"
    assert persistent["features"][0]["deviating_session_count"] == 3

    temporary = _longitudinal_change(
        {"sessions": [completed_session(0.1), completed_session(0.1)]},
        completed_session(0.13),
        completed_session(0.13)["movement_feature_summary"],
        baseline,
    )
    assert temporary["state"] == "temporary_fluctuation"
    assert temporary["features"] == []

    insufficient = _longitudinal_change(
        {"sessions": []},
        completed_session(0.13),
        completed_session(0.13)["movement_feature_summary"],
        baseline,
    )
    assert insufficient["state"] == "insufficient_sessions"


def test_training_splits_by_participant_and_writes_versioned_evaluation(tmp_path):
    dataset_path = tmp_path / "labeled.csv"
    fields = ["timestamp_seconds", "participant_id", "label", *AXES]
    with dataset_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for participant_index in range(8):
            label = "pattern_a" if participant_index < 4 else "pattern_b"
            frequency = 4.0 if label == "pattern_a" else 7.0
            for index in range(201):
                stamp = 1_700_000_000 + index / 25
                phase = 2 * math.pi * frequency * index / 25
                writer.writerow({
                    "timestamp_seconds": f"{stamp:.6f}",
                    "participant_id": f"participant-{participant_index}",
                    "label": label,
                    "ax": math.sin(phase),
                    "ay": 0.5 * math.sin(phase + 0.2),
                    "az": 9.8 + 0.2 * math.sin(phase),
                    "gx": 10 * math.sin(phase),
                    "gy": 4 * math.sin(phase + 0.4),
                    "gz": 2 * math.sin(phase),
                })

    output = tmp_path / "artifacts"
    report = train_model(
        dataset_path,
        output,
        "Synthetic unit-test fixture",
        "not-a-public-source",
        "test-only",
        25,
        "m/s^2",
        "deg/s",
        "test-1",
        random_state=7,
    )
    assert report["training_status"] == "trained"
    assert set(report["evaluation"]["train_participants"]).isdisjoint(
        report["evaluation"]["test_participants"]
    )
    assert set(report["evaluation"]["models"]) == {"random_forest", "svm"}
    metrics = report["evaluation"]["models"]["random_forest"]
    assert metrics["balanced_accuracy"] >= 0
    assert set(metrics["class_metrics"]) >= {"pattern_a", "pattern_b"}
    assert (output / "movement-rf-test-1.joblib").exists()
    assert (output / "movement-rf-test-1.json").exists()
    assert json.loads((output / "current-model.json").read_text())["artifact"] == "movement-rf-test-1.joblib"


def test_calibration_preserves_versioned_feature_baselines_and_session_comparisons(
    tmp_path,
    monkeypatch,
):
    store = ProfileStore(tmp_path / "profiles.json")
    store.create_profile("Person", "ml-person")
    clock = [500.0]
    monkeypatch.setattr("movement.time.monotonic", lambda: clock[0])
    start = datetime(2026, 10, 29, tzinfo=timezone.utc)

    def complete_baseline(placement, start_index):
        store.start_calibration("ml-person", 120, placement)
        for index in range(601):
            stamp = start + timedelta(seconds=start_index + index / 5)
            sample = build_samples(sample_rate_hz=5, duration_seconds=0, start_time=stamp)[0]
            sample["timestamp"] = stamp.isoformat().replace("+00:00", "Z")
            store.record_packet(sample, stamp, clock[0] + index / 5, True)
        clock[0] += 120
        return store.calibration_status()

    first = complete_baseline("wrist", 0)
    first_baseline_id = first["result"]["baseline"]["calibration_id"]
    baseline_one = store.profile("ml-person")["baseline"]
    assert baseline_one["baseline_version"] == "v2"
    assert baseline_one["sensor_placement"] == "wrist"
    assert baseline_one["movement_feature_summary"]["status"] == "available"
    assert baseline_one["sampling_information"]["sample_rate_hz"] == pytest.approx(5)
    assert baseline_one["movement_feature_summary"]["frequency_features_available"] is False

    second = complete_baseline("hand", 200)
    profile = store.profile("ml-person")
    assert second["result"]["baseline"]["calibration_id"] != first_baseline_id
    assert len(profile["calibrations"]) == 2
    assert profile["calibrations"][0]["baseline"]["calibration_id"] == first_baseline_id
    assert profile["baseline"]["sensor_placement"] == "hand"
    assert profile["baseline"]["test_protocol"]

    store.start_monitoring("ml-person")
    session_start = start + timedelta(seconds=400)
    for index, sample in enumerate(build_samples(sample_rate_hz=5, duration_seconds=30, acceleration_scale=0.8)):
        stamp = session_start + timedelta(seconds=index / 5)
        sample["timestamp"] = stamp.isoformat().replace("+00:00", "Z")
        store.record_packet(sample, stamp, clock[0] + index / 5, True)
    clock[0] += 30
    report = store.end_monitoring()
    assert report["movement_feature_summary"]["status"] == "available"
    assert report["movement_feature_comparisons"]["status"] == "available"
    assert report["movement_feature_comparisons"]["features"]["ax_rms"]["absolute_difference"] >= 0
    assert report["longitudinal_change"]["state"] == "insufficient_sessions"
    assert report["model_analysis"]["state"] == "disabled"
    assert store.profile("ml-person")["baseline"]["calibration_id"] == second["result"]["baseline"]["calibration_id"]
    assert report["baseline"]["sensor_placement"] == "hand"


def test_inadequate_calibration_explains_why_it_was_not_saved(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "profiles.json")
    store.create_profile("Person", "empty")
    clock = [10.0]
    monkeypatch.setattr("movement.time.monotonic", lambda: clock[0])
    store.start_calibration("empty", 120)
    clock[0] += 120
    state = store.calibration_status()
    assert state["state"] == "insufficient_data"
    assert state["result"]["incomplete_reason"]
    assert store.profile("empty")["baseline"] is None
