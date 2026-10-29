from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np

from neuroml.features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    MIN_TRAINING_SAMPLE_RATE_HZ,
    FeatureValidationError,
    extract_feature_windows,
    model_feature_vector,
    sampling_information,
)


LOGGER = logging.getLogger(__name__)
MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "artifacts"
MODEL_POINTER = "current-model.json"


class MovementModelService:
    def __init__(self, artifact_directory: Path = MODEL_DIRECTORY) -> None:
        self.artifact_directory = artifact_directory
        self.estimator: Any = None
        self.metadata: Dict[str, Any] = {}
        self.load_error: Optional[str] = None
        self._load_once()

    def _load_once(self) -> None:
        pointer_path = self.artifact_directory / MODEL_POINTER
        if not pointer_path.exists():
            return
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            artifact_name = pointer.get("artifact")
            if (
                not isinstance(artifact_name, str)
                or Path(artifact_name).name != artifact_name
                or not artifact_name.startswith("movement-rf-")
                or not artifact_name.endswith(".joblib")
            ):
                raise ValueError("The model pointer contains an invalid artifact filename.")

            import joblib

            loaded = joblib.load(self.artifact_directory / artifact_name)
            if not isinstance(loaded, dict):
                raise ValueError("The model artifact has an unsupported structure.")
            metadata = loaded.get("metadata")
            estimator = loaded.get("estimator")
            if (
                not isinstance(metadata, dict)
                or metadata.get("training_status") != "trained"
                or metadata.get("feature_version") != FEATURE_VERSION
                or metadata.get("feature_names") != list(FEATURE_NAMES)
                or not isinstance(metadata.get("model_name"), str)
                or not isinstance(metadata.get("model_version"), str)
                or estimator is None
                or not callable(getattr(estimator, "predict", None))
            ):
                raise ValueError("The model artifact is missing required training metadata or features.")
            self.estimator = estimator
            self.metadata = metadata
        except Exception as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("NeuroWatch movement model could not be loaded from %s.", pointer_path)

    def status(self) -> Dict[str, Any]:
        if self.estimator is not None:
            return {
                "state": "ready",
                "model_name": self.metadata["model_name"],
                "model_version": self.metadata["model_version"],
                "classes": list(self.metadata.get("classes", [])),
                "confidence_available": False,
                "message": "A trained movement-pattern model is loaded. Its class output is exploratory, not a diagnosis.",
            }
        if self.load_error is not None:
            return {
                "state": "model_loading_error",
                "model_name": None,
                "model_version": None,
                "classes": [],
                "confidence_available": False,
                "message": "The trained movement model could not be loaded; classification is unavailable.",
                "error": self.load_error,
            }
        return {
            "state": "disabled",
            "model_name": None,
            "model_version": None,
            "classes": [],
            "confidence_available": False,
            "message": "No trained, compatible model artifact is installed. Classification is disabled until training is completed.",
        }

    def analyze(self, samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        state = self.status()
        sampling = sampling_information(samples)
        if sampling["status"] != "good":
            return {
                **state,
                "model_state": state["state"],
                "state": "insufficient_data" if len(samples) < 2 else sampling["status"],
                "output": None,
                "sampling_information": sampling,
                "data_quality": "insufficient_data" if len(samples) < 2 else sampling["status"],
                "message": sampling["message"],
            }
        if float(sampling["sample_rate_hz"]) < MIN_TRAINING_SAMPLE_RATE_HZ:
            return {
                **state,
                "model_state": state["state"],
                "state": "incompatible_sampling_rate",
                "output": None,
                "sampling_information": sampling,
                "data_quality": "incompatible_sampling_rate",
                "message": (
                    f"The model requires at least {MIN_TRAINING_SAMPLE_RATE_HZ:g} Hz; "
                    f"this recording measured {float(sampling['sample_rate_hz']):.2f} Hz."
                ),
            }
        if self.estimator is None:
            return {
                **state,
                "output": None,
                "sampling_information": sampling,
                "data_quality": sampling["status"],
            }

        try:
            windows, sampling = extract_feature_windows(samples)
            if not windows:
                raise FeatureValidationError("At least one complete four-second model window is required.")
            matrix = np.asarray([model_feature_vector(row) for row in windows], dtype=float)
            predictions = self.estimator.predict(matrix)
            if len(predictions) != len(windows):
                raise ValueError("The model returned an unexpected number of class outputs.")
            counts: Dict[str, int] = {}
            for prediction in predictions:
                label = str(prediction)
                counts[label] = counts.get(label, 0) + 1
            selected = max(counts, key=counts.get)
        except FeatureValidationError as exc:
            return {
                **state,
                "state": "insufficient_data",
                "output": None,
                "sampling_information": sampling,
                "data_quality": "insufficient_data",
                "message": str(exc),
            }
        except Exception as exc:
            LOGGER.exception("NeuroWatch movement inference failed.")
            return {
                **state,
                "state": "inference_error",
                "output": None,
                "sampling_information": sampling,
                "data_quality": "inference_error",
                "message": f"Model inference failed ({type(exc).__name__}); no class output was produced.",
            }

        return {
            **state,
            "state": "available",
            "output": {"class": selected},
            "window_count": len(windows),
            "sampling_information": sampling,
            "data_quality": "good",
            "message": "Class output only; no calibrated probability or clinical interpretation is available.",
        }
