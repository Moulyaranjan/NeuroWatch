from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np


AXES = ("ax", "ay", "az", "gx", "gy", "gz")
ACCEL_AXES = ("ax", "ay", "az")
GYRO_AXES = ("gx", "gy", "gz")
FEATURE_VERSION = "movement-features-v1"
WINDOW_SECONDS = 4.0
WINDOW_OVERLAP = 0.5
MIN_TRAINING_SAMPLE_RATE_HZ = 25.0
MIN_SUPPORTED_SAMPLE_RATE_HZ = 2.0
ACCEL_UNITS = {"m/s^2", "m/s²"}
GYRO_UNITS = {"deg/s", "°/s"}


class FeatureValidationError(ValueError):
    """Raised when a sensor window cannot be processed without guessing."""


def _parse_sample_time(sample: Dict[str, Any]) -> float:
    value = sample.get("timestamp") or sample.get("sensor_timestamp")
    if not isinstance(value, str):
        raise FeatureValidationError("Each sample needs a timestamp.")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeatureValidationError("Sample timestamps must be ISO-8601 values.") from exc
    if timestamp.tzinfo is None:
        raise FeatureValidationError("Sample timestamps must include a timezone.")
    return timestamp.timestamp()


def _validate_samples(
    samples: Sequence[Dict[str, Any]],
) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    if len(samples) < 2:
        raise FeatureValidationError("At least two timestamped samples are required.")
    if any(not isinstance(sample, dict) for sample in samples):
        raise FeatureValidationError("Each sensor sample must be an object.")

    times = np.asarray([_parse_sample_time(sample) for sample in samples], dtype=float)
    intervals = np.diff(times)
    if not np.all(np.isfinite(times)) or np.any(intervals <= 0):
        raise FeatureValidationError("Sample timestamps must be finite and strictly increasing.")

    signals: Dict[str, np.ndarray] = {}
    for axis in AXES:
        values: List[float] = []
        expected_units = ACCEL_UNITS if axis in ACCEL_AXES else GYRO_UNITS
        for sample in samples:
            sensor_values = sample.get("values")
            units = sample.get("units")
            if not isinstance(sensor_values, dict) or axis not in sensor_values:
                raise FeatureValidationError(f"Sample is missing the '{axis}' reading.")
            if not isinstance(units, dict) or units.get(axis) not in expected_units:
                raise FeatureValidationError(f"Sample has missing or incompatible units for '{axis}'.")
            try:
                value = float(sensor_values[axis])
            except (TypeError, ValueError) as exc:
                raise FeatureValidationError(f"Sample reading '{axis}' must be numeric.") from exc
            if not math.isfinite(value):
                raise FeatureValidationError(f"Sample reading '{axis}' must be finite.")
            values.append(value)
        signals[axis] = np.asarray(values, dtype=float)
    return times, signals


def sampling_information(samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        times, _ = _validate_samples(samples)
    except FeatureValidationError as exc:
        return {
            "status": "invalid_data",
            "sample_count": len(samples),
            "sample_rate_hz": None,
            "duration_seconds": None,
            "interval_cv": None,
            "message": str(exc),
        }

    intervals = np.diff(times)
    median_interval = float(np.median(intervals))
    interval_cv = float(np.std(intervals) / np.mean(intervals))
    rate = 1.0 / median_interval
    timestamp_fields = {
        "timestamp" if sample.get("timestamp") else "sensor_timestamp"
        for sample in samples
    }
    if len(timestamp_fields) != 1:
        return {
            "status": "invalid_data",
            "sample_count": len(samples),
            "sample_rate_hz": None,
            "duration_seconds": None,
            "interval_cv": interval_cv,
            "message": "All samples must use the same timestamp field.",
        }
    status = "good" if interval_cv <= 0.25 else "irregular_sampling"
    return {
        "status": status,
        "sample_count": len(samples),
        "sample_rate_hz": rate,
        "duration_seconds": float(times[-1] - times[0]),
        "interval_cv": interval_cv,
        "timestamp_source": (
            "backend_received_at" if timestamp_fields == {"timestamp"} else "sensor_timestamp"
        ),
        "message": (
            "Sampling intervals are too irregular for reproducible spectral features."
            if status != "good"
            else None
        ),
    }


def _spectrum_features(signal: np.ndarray, sample_rate_hz: float) -> Dict[str, float | None]:
    if sample_rate_hz < MIN_TRAINING_SAMPLE_RATE_HZ:
        return {
            "dominant_frequency_hz": None,
            "spectral_entropy": None,
            "tremor_band_power": None,
        }

    centered = signal - float(np.mean(signal))
    power = np.square(np.abs(np.fft.rfft(centered))) / len(centered)
    frequencies = np.fft.rfftfreq(len(centered), d=1.0 / sample_rate_hz)
    usable = (frequencies >= 0.5) & (frequencies <= min(10.0, sample_rate_hz / 2.0))
    if not np.any(usable):
        return {
            "dominant_frequency_hz": None,
            "spectral_entropy": None,
            "tremor_band_power": None,
        }

    usable_power = power[usable]
    usable_frequencies = frequencies[usable]
    total_power = float(np.sum(usable_power))
    if total_power <= 0:
        entropy = 0.0
        dominant_frequency = 0.0
    else:
        probabilities = usable_power / total_power
        nonzero = probabilities > 0
        entropy = float(
            -np.sum(probabilities[nonzero] * np.log(probabilities[nonzero]))
            / math.log(max(2, len(probabilities)))
        )
        dominant_frequency = float(usable_frequencies[int(np.argmax(usable_power))])

    tremor_band = (frequencies >= 3.0) & (frequencies <= min(10.0, sample_rate_hz / 2.0))
    return {
        "dominant_frequency_hz": dominant_frequency,
        "spectral_entropy": entropy,
        "tremor_band_power": float(np.sum(power[tremor_band])),
    }


def _extract_features_from_signals(
    raw_signals: Dict[str, np.ndarray],
    sample_rate_hz: float,
) -> Dict[str, float | None]:
    result: Dict[str, float | None] = {}
    for axis, signal in raw_signals.items():
        centered = signal - float(np.mean(signal))
        result.update({
            f"{axis}_mean": float(np.mean(signal)),
            f"{axis}_variance": float(np.var(signal)),
            f"{axis}_rms": float(np.sqrt(np.mean(np.square(centered)))),
            f"{axis}_peak_to_peak": float(np.ptp(signal)),
            **{
                f"{axis}_{name}": value
                for name, value in _spectrum_features(centered, sample_rate_hz).items()
            },
        })

    for prefix, axes in (("acceleration", ACCEL_AXES), ("gyroscope", GYRO_AXES)):
        magnitude = np.sqrt(sum(np.square(raw_signals[axis]) for axis in axes))
        centered = magnitude - float(np.mean(magnitude))
        result.update({
            f"{prefix}_magnitude_mean": float(np.mean(magnitude)),
            f"{prefix}_magnitude_variance": float(np.var(magnitude)),
            f"{prefix}_magnitude_rms": float(np.sqrt(np.mean(np.square(centered)))),
            f"{prefix}_magnitude_peak_to_peak": float(np.ptp(magnitude)),
            **{
                f"{prefix}_magnitude_{name}": value
                for name, value in _spectrum_features(centered, sample_rate_hz).items()
            },
        })
    return result


def extract_window_features(
    samples: Sequence[Dict[str, Any]],
    sample_rate_hz: float,
) -> Dict[str, float | None]:
    if not math.isfinite(sample_rate_hz) or sample_rate_hz < MIN_SUPPORTED_SAMPLE_RATE_HZ:
        raise FeatureValidationError("Sampling rate must be at least 2 Hz.")
    _, raw_signals = _validate_samples(samples)
    expected_samples = int(round(sample_rate_hz * WINDOW_SECONDS))
    if len(samples) < expected_samples:
        raise FeatureValidationError(
            f"A {WINDOW_SECONDS:g}-second window needs at least {expected_samples} samples."
        )

    return _extract_features_from_signals(raw_signals, sample_rate_hz)


def extract_feature_windows(
    samples: Sequence[Dict[str, Any]],
    window_seconds: float = WINDOW_SECONDS,
    overlap: float = WINDOW_OVERLAP,
) -> Tuple[List[Dict[str, float | None]], Dict[str, Any]]:
    info = sampling_information(samples)
    if info["status"] != "good":
        raise FeatureValidationError(info["message"] or "Sampling information is unavailable.")
    if not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("window_seconds must be positive.")
    if not 0 <= overlap < 1:
        raise ValueError("overlap must be in the range [0, 1).")

    times, signals = _validate_samples(samples)
    rate = float(info["sample_rate_hz"])
    duration = float(info["duration_seconds"])
    window_size = int(round(rate * window_seconds))
    sample_count = int(math.floor(duration * rate + 1e-6)) + 1
    if sample_count < window_size:
        return [], info

    uniform_times = times[0] + np.arange(sample_count, dtype=float) / rate
    if len(uniform_times) < 2:
        return [], info
    uniform_signals = {
        axis: np.interp(uniform_times, times, values)
        for axis, values in signals.items()
    }
    stride = max(1, int(round(window_size * (1.0 - overlap))))
    rows = []
    for start in range(0, len(uniform_times) - window_size + 1, stride):
        end = start + window_size
        rows.append(
            _extract_features_from_signals(
                {axis: uniform_signals[axis][start:end] for axis in AXES},
                rate,
            )
        )
    return rows, info


def summarize_features(samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        rows, info = extract_feature_windows(samples)
    except FeatureValidationError as exc:
        info = sampling_information(samples)
        return {
            "version": FEATURE_VERSION,
            "status": info["status"] if info["status"] != "good" else "invalid_data",
            "message": str(exc),
            "sampling_information": info,
            "window_seconds": WINDOW_SECONDS,
            "window_count": 0,
            "values": {},
            "feature_statistics": {},
            "limitations": [str(exc)],
        }

    if not rows:
        reason = "The recording is shorter than one complete feature window."
        return {
            "version": FEATURE_VERSION,
            "status": "insufficient_data",
            "message": reason,
            "sampling_information": info,
            "window_seconds": WINDOW_SECONDS,
            "window_count": 0,
            "values": {},
            "feature_statistics": {},
            "limitations": [reason],
        }

    names = rows[0].keys()
    statistics: Dict[str, Dict[str, float | int]] = {}
    values: Dict[str, float | None] = {}
    for name in names:
        window_values = [
            float(row[name])
            for row in rows
            if row[name] is not None and math.isfinite(float(row[name]))
        ]
        if not window_values:
            values[name] = None
            continue
        mean = float(np.mean(window_values))
        statistics[name] = {
            "mean": mean,
            "standard_deviation": float(np.std(window_values)),
            "minimum": float(np.min(window_values)),
            "maximum": float(np.max(window_values)),
            "window_count": len(window_values),
        }
        values[name] = mean

    frequency_available = float(info["sample_rate_hz"]) >= MIN_TRAINING_SAMPLE_RATE_HZ
    limitations = []
    if not frequency_available:
        limitations.append(
            f"Sampling rate is below {MIN_TRAINING_SAMPLE_RATE_HZ:g} Hz; frequency-domain features are unavailable."
        )
    return {
        "version": FEATURE_VERSION,
        "status": "available",
        "message": None,
        "sampling_information": info,
        "window_seconds": WINDOW_SECONDS,
        "window_overlap": WINDOW_OVERLAP,
        "window_count": len(rows),
        "frequency_features_available": frequency_available,
        "values": values,
        "feature_statistics": statistics,
        "limitations": limitations,
    }


def model_feature_vector(feature_row: Dict[str, float | None]) -> List[float]:
    values = [feature_row.get(name) for name in FEATURE_NAMES]
    if any(value is None or not math.isfinite(float(value)) for value in values):
        raise FeatureValidationError("All model features must be finite and available.")
    return [float(value) for value in values]


def _feature_names() -> Tuple[str, ...]:
    names: List[str] = []
    for axis in AXES:
        names.extend((
            f"{axis}_mean",
            f"{axis}_variance",
            f"{axis}_rms",
            f"{axis}_peak_to_peak",
            f"{axis}_dominant_frequency_hz",
            f"{axis}_spectral_entropy",
            f"{axis}_tremor_band_power",
        ))
    for prefix in ("acceleration", "gyroscope"):
        names.extend((
            f"{prefix}_magnitude_mean",
            f"{prefix}_magnitude_variance",
            f"{prefix}_magnitude_rms",
            f"{prefix}_magnitude_peak_to_peak",
            f"{prefix}_magnitude_dominant_frequency_hz",
            f"{prefix}_magnitude_spectral_entropy",
            f"{prefix}_magnitude_tremor_band_power",
        ))
    return tuple(names)


FEATURE_NAMES = _feature_names()


def _feature_definitions() -> Dict[str, Dict[str, str]]:
    definitions: Dict[str, Dict[str, str]] = {}
    for name in FEATURE_NAMES:
        if "frequency_hz" in name:
            description, unit = "Largest spectral peak from 0.5 to 10 Hz; zero means no measurable power in that band.", "Hz"
        elif "spectral_entropy" in name:
            description, unit = "Normalized Shannon entropy of spectral power from 0.5 to 10 Hz.", "unitless"
        elif "tremor_band_power" in name:
            description = "Sum of spectral power from 3 to 10 Hz; unavailable below 25 Hz sampling."
            unit = "(m/s^2)^2" if name.startswith(("a", "acceleration")) else "(deg/s)^2"
        elif "variance" in name:
            description = "Population variance of the raw axis or magnitude in the window."
            unit = "(m/s^2)^2" if name.startswith(("a", "acceleration")) else "(deg/s)^2"
        elif name.endswith("_mean"):
            description = "Arithmetic mean of the raw axis or magnitude in the window."
            unit = "m/s^2" if name.startswith(("a", "acceleration")) else "deg/s"
        elif name.endswith("_rms"):
            description = "Root-mean-square of the demeaned axis or magnitude in the window."
            unit = "m/s^2" if name.startswith(("a", "acceleration")) else "deg/s"
        else:
            description = "Peak-to-peak range of the raw axis or magnitude in the window."
            unit = "m/s^2" if name.startswith(("a", "acceleration")) else "deg/s"
        definitions[name] = {"description": description, "unit": unit}
    return definitions


FEATURE_DEFINITIONS = _feature_definitions()
