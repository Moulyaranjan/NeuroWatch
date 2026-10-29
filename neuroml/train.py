from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from neuroml.features import (
    ACCEL_UNITS,
    ACCEL_AXES,
    AXES,
    FEATURE_DEFINITIONS,
    FEATURE_NAMES,
    FEATURE_VERSION,
    GYRO_UNITS,
    MIN_TRAINING_SAMPLE_RATE_HZ,
    WINDOW_OVERLAP,
    WINDOW_SECONDS,
    extract_feature_windows,
)


REQUIRED_COLUMNS = ("timestamp_seconds", "participant_id", "label", *AXES)
def _read_dataset(
    dataset_path: Path,
    declared_sample_rate_hz: float,
    acceleration_unit: str,
    gyroscope_unit: str,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, Any]]:
    if declared_sample_rate_hz < MIN_TRAINING_SAMPLE_RATE_HZ:
        raise ValueError(
            f"Training data must be sampled at {MIN_TRAINING_SAMPLE_RATE_HZ:g} Hz or faster."
        )
    if acceleration_unit not in ACCEL_UNITS or gyroscope_unit not in GYRO_UNITS:
        raise ValueError("Training requires acceleration in m/s^2 and gyroscope in deg/s; convert explicitly before training.")
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    with dataset_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or any(column not in reader.fieldnames for column in REQUIRED_COLUMNS):
            raise ValueError(
                "Dataset CSV must include: " + ", ".join(REQUIRED_COLUMNS)
            )
        for line_number, row in enumerate(reader, start=2):
            participant = (row.get("participant_id") or "").strip()
            label = (row.get("label") or "").strip()
            if not participant or not label:
                raise ValueError(f"Dataset row {line_number} is missing participant_id or label.")
            try:
                timestamp = float(row["timestamp_seconds"])
                values = {axis: float(row[axis]) for axis in AXES}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Dataset row {line_number} contains a missing or non-numeric value.") from exc
            if not math.isfinite(timestamp) or any(not math.isfinite(value) for value in values.values()):
                raise ValueError(f"Dataset row {line_number} contains a non-finite value.")
            grouped.setdefault(participant, []).append({
                "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                "participant_id": participant,
                "label": label,
                "values": values,
                "units": {
                    axis: acceleration_unit if axis in ACCEL_AXES else gyroscope_unit
                    for axis in AXES
                },
            })

    feature_rows: List[List[float]] = []
    labels: List[str] = []
    participants: List[str] = []
    interval_limit = 1.5 / declared_sample_rate_hz
    for participant, records in grouped.items():
        records.sort(key=lambda item: item["timestamp"])
        segment: List[Dict[str, Any]] = []
        prior_label: str | None = None
        prior_timestamp: float | None = None
        for record in records:
            timestamp = datetime.fromisoformat(record["timestamp"]).timestamp()
            if segment and (
                record["label"] != prior_label
                or prior_timestamp is None
                or timestamp - prior_timestamp > interval_limit
            ):
                rows = _windows_for_segment(segment, declared_sample_rate_hz, participant)
                feature_rows.extend(rows)
                labels.extend([prior_label] * len(rows))
                participants.extend([participant] * len(rows))
                segment = []
            segment.append(record)
            prior_label = record["label"]
            prior_timestamp = timestamp
        rows = _windows_for_segment(segment, declared_sample_rate_hz, participant)
        feature_rows.extend(rows)
        labels.extend([prior_label] * len(rows))
        participants.extend([participant] * len(rows))

    if not feature_rows:
        raise ValueError("The dataset did not contain any complete, valid four-second windows.")
    if len(set(labels)) < 2:
        raise ValueError("At least two labeled movement classes are required.")
    return (
        np.asarray(feature_rows, dtype=float),
        np.asarray(labels, dtype=str),
        np.asarray(participants, dtype=str),
        {
            "participant_count": len(grouped),
            "window_count": len(feature_rows),
            "class_counts": {
                label: int(sum(existing == label for existing in labels))
                for label in sorted(set(labels))
            },
            "declared_sample_rate_hz": declared_sample_rate_hz,
        },
    )


def _windows_for_segment(
    segment: Sequence[Dict[str, Any]],
    declared_sample_rate_hz: float,
    participant: str,
) -> List[List[float]]:
    if len(segment) < int(declared_sample_rate_hz * WINDOW_SECONDS):
        return []
    windows, information = extract_feature_windows(
        segment,
        window_seconds=WINDOW_SECONDS,
        overlap=WINDOW_OVERLAP,
    )
    observed_rate = float(information["sample_rate_hz"])
    if abs(observed_rate - declared_sample_rate_hz) / declared_sample_rate_hz > 0.1:
        raise ValueError(
            f"Participant {participant!r} has observed sampling rate {observed_rate:.2f} Hz, "
            f"not the declared {declared_sample_rate_hz:g} Hz."
        )
    missing_features = [
        name
        for row in windows
        for name in FEATURE_NAMES
        if row.get(name) is None
    ]
    if missing_features:
        raise ValueError(
            f"Participant {participant!r} produced a window with unavailable frequency features."
        )
    return [[float(window[name]) for name in FEATURE_NAMES] for window in windows]


def _participant_split(
    features: np.ndarray,
    labels: np.ndarray,
    participants: np.ndarray,
    random_state: int,
) -> Tuple[np.ndarray, np.ndarray]:
    classes = set(labels)
    if any(len(set(participants[labels == label])) < 2 for label in classes):
        raise ValueError("Each class must occur in at least two participants for participant-independent evaluation.")
    splitter = GroupShuffleSplit(n_splits=100, test_size=0.25, random_state=random_state)
    for train_indices, test_indices in splitter.split(features, labels, groups=participants):
        if set(labels[train_indices]) == classes and set(labels[test_indices]) == classes:
            if set(participants[train_indices]).isdisjoint(set(participants[test_indices])):
                return train_indices, test_indices
    raise ValueError(
        "Could not create a participant-independent split containing every class in train and test."
    )


def _evaluate(
    estimator: Pipeline,
    train_features: np.ndarray,
    train_labels: np.ndarray,
    test_features: np.ndarray,
    test_labels: np.ndarray,
    class_labels: Sequence[str],
) -> Dict[str, Any]:
    estimator.fit(train_features, train_labels)
    predictions = estimator.predict(test_features)
    return {
        "accuracy": float(accuracy_score(test_labels, predictions)),
        "balanced_accuracy": float(balanced_accuracy_score(test_labels, predictions)),
        "macro_f1": float(f1_score(test_labels, predictions, average="macro", zero_division=0)),
        "class_metrics": classification_report(
            test_labels,
            predictions,
            labels=list(class_labels),
            output_dict=True,
            zero_division=0,
        ),
        "confusion_matrix": confusion_matrix(
            test_labels,
            predictions,
            labels=list(class_labels),
        ).tolist(),
        "class_labels": list(class_labels),
    }


def train_model(
    dataset_path: Path,
    output_directory: Path,
    dataset_name: str,
    dataset_source: str,
    dataset_license: str,
    declared_sample_rate_hz: float,
    acceleration_unit: str,
    gyroscope_unit: str,
    model_version: str,
    compare_svm: bool = True,
    random_state: int = 42,
) -> Dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", model_version):
        raise ValueError("model_version must be a short filename-safe identifier.")
    if any(not value.strip() for value in (dataset_name, dataset_source, dataset_license)):
        raise ValueError("Dataset name, source, and license are required provenance fields.")

    features, labels, participants, dataset_counts = _read_dataset(
        dataset_path,
        declared_sample_rate_hz,
        acceleration_unit,
        gyroscope_unit,
    )
    train_indices, test_indices = _participant_split(
        features,
        labels,
        participants,
        random_state,
    )
    classes = sorted(set(labels))
    random_forest = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("classifier", RandomForestClassifier(
            n_estimators=300,
            class_weight="balanced_subsample",
            random_state=random_state,
            n_jobs=1,
        )),
    ])
    results = {
        "random_forest": _evaluate(
            random_forest,
            features[train_indices],
            labels[train_indices],
            features[test_indices],
            labels[test_indices],
            classes,
        )
    }
    if compare_svm:
        svm = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("classifier", SVC(class_weight="balanced")),
        ])
        results["svm"] = _evaluate(
            svm,
            features[train_indices],
            labels[train_indices],
            features[test_indices],
            labels[test_indices],
            classes,
        )

    evaluation = {
        "split_strategy": "GroupShuffleSplit by participant_id",
        "random_state": random_state,
        "train_participants": sorted(set(participants[train_indices])),
        "test_participants": sorted(set(participants[test_indices])),
        "train_window_count": int(len(train_indices)),
        "test_window_count": int(len(test_indices)),
        "models": results,
    }
    metadata = {
        "training_status": "trained",
        "model_name": "NeuroWatch Random Forest movement-pattern classifier",
        "model_version": model_version,
        "task": "movement-pattern classification from labeled sensor windows; not clinical diagnosis",
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "feature_definitions": FEATURE_DEFINITIONS,
        "sampling_rate_hz": declared_sample_rate_hz,
        "window_seconds": WINDOW_SECONDS,
        "window_overlap": WINDOW_OVERLAP,
        "sensor_axes": list(AXES),
        "units": {"acceleration": acceleration_unit, "gyroscope": gyroscope_unit},
        "classes": classes,
        "dataset_provenance": {
            "name": dataset_name,
            "source": dataset_source,
            "license": dataset_license,
            "input_file_name": dataset_path.name,
            "participant_count": dataset_counts["participant_count"],
            "window_count": dataset_counts["window_count"],
            "class_counts": dataset_counts["class_counts"],
        },
        "evaluation": evaluation,
        "known_limitations": [
            "Participant-independent evaluation is not external clinical validation.",
            "Model outputs are dataset label classes only and are not diagnosis, severity, or disease risk.",
            "No probability calibration is applied; inference exposes no confidence score.",
            "Performance may not transfer across sensor placement, device, units, sampling rate, or task protocol.",
        ],
        "software_versions": {"scikit_learn": sklearn.__version__, "numpy": np.__version__},
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    output_directory.mkdir(parents=True, exist_ok=True)
    artifact_name = f"movement-rf-{model_version}.joblib"
    report_name = f"movement-rf-{model_version}.json"
    artifact_path = output_directory / artifact_name
    report_path = output_directory / report_name
    joblib.dump({"estimator": random_forest, "metadata": metadata}, artifact_path)
    report_path.write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
    (output_directory / "current-model.json").write_text(
        json.dumps({"artifact": artifact_name, "report": report_name}, indent=2),
        encoding="utf-8",
    )
    return metadata


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Train and participant-independently evaluate the NeuroWatch movement classifier."
    )
    parser.add_argument("--dataset", type=Path, required=True, help="Labeled long-format CSV; see README.")
    parser.add_argument("--dataset-name", required=True, help="Dataset title for model provenance.")
    parser.add_argument("--dataset-source", required=True, help="Dataset source URL or DOI.")
    parser.add_argument("--dataset-license", required=True, help="Verified license or data-use terms.")
    parser.add_argument("--sampling-rate-hz", type=float, required=True)
    parser.add_argument("--accel-unit", choices=sorted(ACCEL_UNITS), required=True)
    parser.add_argument("--gyro-unit", choices=sorted(GYRO_UNITS), required=True)
    parser.add_argument("--model-version", default="1.0.0")
    parser.add_argument("--skip-svm", action="store_true", help="Skip the optional SVM comparison.")
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args(argv)
    try:
        report = train_model(
            args.dataset,
            Path(__file__).resolve().parents[1] / "artifacts",
            args.dataset_name,
            args.dataset_source,
            args.dataset_license,
            args.sampling_rate_hz,
            args.accel_unit,
            args.gyro_unit,
            args.model_version,
            compare_svm=not args.skip_svm,
            random_state=args.random_state,
        )
    except (OSError, ValueError) as exc:
        print(f"Training failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
