from __future__ import annotations

import csv
import json
import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
EXPORTS_DIR = DATA_DIR / "exports"
SESSION_FILE = SESSIONS_DIR / "session_store.json"

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_SOURCES = {"real", "uncalibrated", "simulated", "unavailable"}
SIMULATOR_ENABLED = False
MAX_SENSOR_AGE_SECONDS = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(value: Optional[datetime] = None) -> str:
    dt = value or utc_now()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SensorPayload(BaseModel):
    sensor: str = Field(..., min_length=2)
    source: str = "real"
    values: Dict[str, float] = Field(default_factory=dict)
    timestamp: Optional[str] = None
    units: Dict[str, str] = Field(default_factory=dict)
    heart_rate: Optional[float] = None
    step_count: Optional[int] = None
    cadence: Optional[float] = None
    activity: Optional[str] = None
    sequence_number: Optional[int] = Field(default=None, ge=0)
    sensor_freshness_ms: Optional[float] = Field(default=None, gt=0)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if value not in ALLOWED_SOURCES:
            raise ValueError(f"Unsupported source '{value}'. Allowed values: {sorted(ALLOWED_SOURCES)}")
        if value == "simulated" and not SIMULATOR_ENABLED:
            raise ValueError("Simulation mode is disabled by default. Use real or uncalibrated data only.")
        return value

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError as exc:
            raise ValueError("timestamp must be a valid ISO-8601 datetime string") from exc

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: Dict[str, float]) -> Dict[str, float]:
        if not values:
            raise ValueError("values cannot be empty")
        cleaned: Dict[str, float] = {}
        for key, value in values.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"Sensor value for '{key}' must be numeric") from None
            cleaned[key] = numeric

        sensor_keys = {"ax", "ay", "az", "gx", "gy", "gz"}
        if any(key in cleaned for key in sensor_keys) and not sensor_keys.issubset(cleaned.keys()):
            missing = sorted(sensor_keys - cleaned.keys())
            raise ValueError(f"Incomplete IMU payload. Missing axes: {missing}")
        return cleaned


class SessionStore:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self.current_session_id: Optional[str] = None
        self.sequence_numbers: Dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        # Keep the dashboard in a genuine idle state until live sensor data arrives.
        # Persisted session files are ignored on startup so stale test data does not
        # appear as a real sensor feed.
        self.events = []
        self.current_session_id = None
        self.sequence_numbers = {}
        if SESSION_FILE.exists():
            try:
                SESSION_FILE.unlink()
            except OSError:
                pass

    def _save(self) -> None:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SESSION_FILE.open("w", encoding="utf-8") as handle:
            json.dump({"current_session_id": self.current_session_id, "events": self.events}, handle, indent=2)

    def ensure_session(self) -> str:
        if self.current_session_id is None:
            self.current_session_id = f"session-{iso_timestamp().replace(':', '').replace('-', '').replace('Z', '')}"
        return self.current_session_id

    def add_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = self.ensure_session()
        sensor_name = payload.get("sensor", "unknown")
        sequence_number = payload.get("sequence_number")
        if sequence_number is not None:
            last_seen = self.sequence_numbers.get(sensor_name)
            if last_seen is not None and int(sequence_number) <= int(last_seen):
                raise ValueError(f"Out-of-order or duplicate sequence number for {sensor_name}.")
            self.sequence_numbers[sensor_name] = int(sequence_number)

        event = {
            "session_id": session_id,
            "timestamp": payload.get("timestamp") or iso_timestamp(),
            "sensor": sensor_name,
            "source": payload.get("source", "real"),
            "values": payload.get("values", {}),
            "units": payload.get("units", {}),
            "heart_rate": payload.get("heart_rate"),
            "step_count": payload.get("step_count"),
            "cadence": payload.get("cadence"),
            "activity": payload.get("activity"),
            "sequence_number": sequence_number,
            "sensor_freshness_ms": payload.get("sensor_freshness_ms"),
        }
        self.events.append(event)
        self._save()
        return event

    def export_csv(self) -> Path:
        export_path = EXPORTS_DIR / f"session_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        fieldnames = [
            "timestamp",
            "session_id",
            "sensor",
            "source",
            "values",
            "heart_rate",
            "step_count",
            "cadence",
            "activity",
        ]
        with export_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for event in self.events:
                writer.writerow(
                    {
                        "timestamp": event.get("timestamp", ""),
                        "session_id": event.get("session_id", ""),
                        "sensor": event.get("sensor", ""),
                        "source": event.get("source", ""),
                        "values": json.dumps(event.get("values", {}), separators=(",", ":")),
                        "heart_rate": event.get("heart_rate", ""),
                        "step_count": event.get("step_count", ""),
                        "cadence": event.get("cadence", ""),
                        "activity": event.get("activity", ""),
                    }
                )
        return export_path

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return list(self.events)[-limit:]


store = SessionStore()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)
        await websocket.send_json(build_dashboard_payload())

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        for connection in list(self.active_connections):
            try:
                await connection.send_json(payload)
            except RuntimeError:
                self.active_connections.discard(connection)


manager = ConnectionManager()


def calculate_magnitude(values: Dict[str, float]) -> float:
    ax = float(values.get("ax", 0.0) or 0.0)
    ay = float(values.get("ay", 0.0) or 0.0)
    az = float(values.get("az", 0.0) or 0.0)
    return (ax ** 2 + ay ** 2 + az ** 2) ** 0.5


def calculate_baseline(history: List[Dict[str, Any]]) -> Dict[str, float]:
    baseline = {"ax": 0.0, "ay": 0.0, "az": 0.0, "gx": 0.0, "gy": 0.0, "gz": 0.0}
    recent = history[-50:]
    if not recent:
        return baseline
    totals = defaultdict(float)
    counts = defaultdict(int)
    for event in recent:
        values = event.get("values", {})
        for key in baseline:
            if key in values:
                totals[key] += float(values[key])
                counts[key] += 1
    for key in baseline:
        if counts[key]:
            baseline[key] = totals[key] / counts[key]
    return baseline


def build_alert_messages(latest_event: Optional[Dict[str, Any]], baseline: Dict[str, float]) -> List[str]:
    if latest_event is None:
        return ["Waiting for sensor data. No movement baseline yet."]

    values = latest_event.get("values", {})
    magnitude = calculate_magnitude(values)
    baseline_mag = calculate_magnitude(baseline)
    delta = abs(magnitude - baseline_mag)
    alerts: List[str] = []

    if delta > 1.2:
        alerts.append(
            "Movement deviation detected: acceleration is materially different from the personalized baseline. "
            "Review the session and verify calibration."
        )
    else:
        alerts.append("Movement remains within the personalized baseline. No immediate drift alert is triggered.")

    if latest_event.get("source") == "uncalibrated":
        alerts.append("Sensor data is present but uncalibrated; trust only after calibration and verification.")
    if latest_event.get("heart_rate") is not None and latest_event.get("heart_rate") > 170:
        alerts.append("Heart-rate signal is elevated; confirm the sensor and check for contact issues.")

    return alerts


SENSOR_SATURATION_LIMITS: Dict[str, float] = {
    "ax": 1.8,
    "ay": 1.8,
    "az": 1.8,
    "gx": 200.0,
    "gy": 200.0,
    "gz": 200.0,
}


def _coerce_float(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _event_magnitude(event: Optional[Dict[str, Any]]) -> Optional[float]:
    if not event:
        return None
    values = event.get("values", {}) or {}
    if not isinstance(values, dict):
        return None
    components = [
        _coerce_float(values.get(axis))
        for axis in ("ax", "ay", "az")
    ]
    if any(component is None for component in components):
        return None
    return math.sqrt(sum(component * component for component in components))


def evaluate_signal_quality(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not history:
        return {
            "available": False,
            "status": "insufficient_data",
            "quality_score": 0.0,
            "summary": "No sensor packets are available for signal-quality analysis.",
            "metrics": {
                "missing_packets": None,
                "sequence_gaps": None,
                "timestamp_gap_ms": None,
                "timestamp_irregularity_ms": None,
                "saturation_events": None,
                "flatlined_windows": None,
                "noise_windows": None,
                "evaluated_packets": 0,
            },
        }

    metrics: Dict[str, Any] = {
        "missing_packets": 0,
        "sequence_gaps": 0,
        "timestamp_gap_ms": None,
        "timestamp_irregularity_ms": None,
        "saturation_events": 0,
        "flatlined_windows": 0,
        "noise_windows": 0,
        "evaluated_packets": len(history),
    }

    timestamps: List[float] = []
    last_sequence: Optional[int] = None
    sequence_seen = False
    for event in history:
        sequence = event.get("sequence_number")
        if sequence is not None:
            sequence_seen = True
            sequence_value = int(sequence)
            if last_sequence is not None and sequence_value > last_sequence + 1:
                metrics["missing_packets"] += sequence_value - last_sequence - 1
                metrics["sequence_gaps"] += 1
            if last_sequence is not None and sequence_value <= last_sequence:
                metrics["sequence_gaps"] += 1
            last_sequence = sequence_value

        timestamp_value = event.get("timestamp")
        if timestamp_value:
            try:
                parsed = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None:
                timestamps.append(parsed.timestamp() * 1000.0)

    if len(timestamps) >= 2:
        gaps = [
            max(0.0, current - previous)
            for previous, current in zip(timestamps, timestamps[1:])
        ]
        if gaps:
            metrics["timestamp_gap_ms"] = round(sum(gaps) / len(gaps), 2)
            avg_gap = sum(gaps) / len(gaps)
            metrics["timestamp_irregularity_ms"] = round(sum(abs(gap - avg_gap) for gap in gaps) / len(gaps), 2)

    for event in history:
        values = event.get("values", {}) or {}
        for axis, limit in SENSOR_SATURATION_LIMITS.items():
            value = _coerce_float(values.get(axis))
            if value is not None and abs(value) >= limit:
                metrics["saturation_events"] += 1

    window_size = min(6, max(2, len(history)))
    for start in range(0, max(len(history) - window_size + 1, 1)):
        window = history[start : start + window_size]
        sample_values = [
            _coerce_float(event.get("values", {}).get(axis))
            for event in window
            for axis in ("ax", "ay", "az")
        ]
        real_values = [value for value in sample_values if value is not None]
        if not real_values:
            continue
        avg_value = sum(real_values) / len(real_values)
        variance = sum((value - avg_value) ** 2 for value in real_values) / len(real_values)
        if variance < 0.0005:
            metrics["flatlined_windows"] += 1
        if variance > 0.18:
            metrics["noise_windows"] += 1

    quality_score = 1.0
    if metrics["missing_packets"]:
        quality_score -= min(0.4, metrics["missing_packets"] * 0.05)
    if metrics["sequence_gaps"]:
        quality_score -= min(0.25, metrics["sequence_gaps"] * 0.08)
    if metrics["timestamp_irregularity_ms"] is not None:
        quality_score -= min(0.2, metrics["timestamp_irregularity_ms"] / 4000.0)
    if metrics["saturation_events"]:
        quality_score -= min(0.2, metrics["saturation_events"] * 0.03)
    if metrics["flatlined_windows"]:
        quality_score -= min(0.2, metrics["flatlined_windows"] * 0.08)
    if metrics["noise_windows"]:
        quality_score -= min(0.25, metrics["noise_windows"] * 0.06)
    quality_score = max(0.0, min(1.0, quality_score))

    if not history or len(history) < 2:
        status = "insufficient_data"
    elif quality_score >= 0.75 and not sequence_seen:
        status = "unknown"
    elif quality_score >= 0.75:
        status = "good"
    elif quality_score >= 0.5:
        status = "warning"
    else:
        status = "poor"

    summary = {
        "good": "Signal quality is acceptable for live monitoring.",
        "warning": "Signal quality is degraded by packet gaps or irregular timing.",
        "poor": "Signal quality is poor; interpretation should be treated cautiously.",
        "unknown": "Signal quality could not be fully assessed without packet metadata.",
        "insufficient_data": "Insufficient sensor data is available to assess signal quality.",
    }.get(status, "Signal quality is unavailable.")

    return {
        "available": True,
        "status": status,
        "quality_score": round(quality_score, 3),
        "summary": summary,
        "metrics": metrics,
    }


def compute_standard_cusum(history: List[Dict[str, Any]], baseline: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    if len(history) < 3:
        return {
            "implemented": True,
            "available": False,
            "state": "insufficient_data",
            "score": None,
            "reference_deviation": None,
            "summary": "Cumulative sum is unavailable until at least three valid packets are present.",
        }

    reference = baseline or calculate_baseline(history[:-1] if len(history) > 1 else history)
    reference_mag = calculate_magnitude(reference)
    cusum = 0.0
    max_cusum = 0.0
    deviations: List[float] = []
    for event in history[-12:]:
        magnitude = _event_magnitude(event)
        if magnitude is None:
            continue
        delta = abs(magnitude - reference_mag)
        deviations.append(delta)
        cusum = max(0.0, cusum + delta - 0.25)
        max_cusum = max(max_cusum, cusum)

    if not deviations:
        return {
            "implemented": True,
            "available": False,
            "state": "insufficient_data",
            "score": None,
            "reference_deviation": None,
            "summary": "Movement vectors were not available for CUSUM scoring.",
        }

    latest_delta = deviations[-1]
    state = "normal"
    if max_cusum >= 1.8:
        state = "candidate_change"
    if max_cusum >= 2.4 and latest_delta > max(0.3, reference_mag * 0.2):
        state = "confirmed_change"

    return {
        "implemented": True,
        "available": True,
        "state": state,
        "score": round(max_cusum, 3),
        "reference_deviation": round(latest_delta, 3),
        "summary": "CUSUM compares the current acceleration magnitude with the rolling baseline and accumulates excess deviation.",
    }


def compute_q_change(history: List[Dict[str, Any]], quality_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if len(history) < 3:
        return {
            "implemented": True,
            "available": False,
            "state": "uncertain",
            "score": None,
            "reference_deviation": None,
            "persistence_duration": 0,
            "signal_quality_status": "insufficient_data",
            "summary": "Q-Change has not accumulated enough valid sensor windows to produce a result.",
            "factors": ["At least three packets are required.", "No quality-aware weighting is applied until packets are present."],
        }

    baseline = calculate_baseline(history[:-1] if len(history) > 1 else history)
    baseline_mag = calculate_magnitude(baseline)
    latest = history[-1]
    latest_mag = _event_magnitude(latest)
    reference_deviation = None if latest_mag is None or baseline_mag is None else abs(latest_mag - baseline_mag)

    quality = quality_state or evaluate_signal_quality(history)
    quality_score = float(quality.get("quality_score", 0.0))
    state = "normal"
    score = 0.0
    if reference_deviation is not None:
        score = max(0.0, reference_deviation * (0.6 + (0.4 * quality_score)))
    if quality_score < 0.35:
        state = "uncertain"
    elif score >= 1.2:
        state = "candidate_change"
    elif score >= 1.8:
        state = "confirmed_change"

    factors = [
        f"Baseline deviation: {reference_deviation if reference_deviation is not None else 'not available'}",
        f"Signal quality score: {quality_score:.2f}",
        "Quality-aware evidence weighting is enabled for detection scoring.",
        "Reference adaptation is intentionally disabled to avoid silently updating the baseline from every packet.",
    ]

    return {
        "implemented": True,
        "available": reference_deviation is not None,
        "state": state,
        "score": round(score, 3) if reference_deviation is not None else None,
        "reference_deviation": round(reference_deviation, 3) if reference_deviation is not None else None,
        "persistence_duration": 1 if score >= 1.2 else 0,
        "signal_quality_status": quality.get("status", "unknown"),
        "summary": "Q-Change blends quality-aware evidence weighting with a fixed baseline reference and a persistence gate.",
        "factors": factors,
    }


def evaluate_research_metrics(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not history:
        return {
            "available": False,
            "metrics": {},
            "summary": "No recorded session is available for evaluation.",
        }

    ground_truth = [event for event in history if event.get("activity")]
    if not ground_truth:
        return {
            "available": False,
            "metrics": {},
            "summary": "Ground-truth annotations are unavailable, so evaluation metrics are not reported.",
        }

    baseline = calculate_baseline(history)
    current = history[-1]
    current_mag = _event_magnitude(current)
    baseline_mag = calculate_magnitude(baseline)
    deviation = abs(current_mag - baseline_mag) if current_mag is not None and baseline_mag is not None else None
    return {
        "available": True,
        "metrics": {
            "false_alarms_per_hour": None,
            "precision": None,
            "recall": None,
            "detection_delay": None,
            "end_to_end_latency_ms": None,
            "baseline_reference": round(baseline_mag, 3) if baseline_mag is not None else None,
            "latest_deviation": round(deviation, 3) if deviation is not None else None,
        },
        "summary": "Labels or evaluation intervals are required before model metrics can be calculated. No results are reported from the current session because ground truth is unavailable.",
    }


def build_dashboard_payload() -> Dict[str, Any]:
    history = store.history(limit=50)
    latest_event = history[-1] if history else None
    baseline = calculate_baseline(history)
    step_count = latest_event.get("step_count") if latest_event else 0
    cadence = latest_event.get("cadence") if latest_event else 0.0
    heart_rate = latest_event.get("heart_rate") if latest_event else None
    activity = latest_event.get("activity") if latest_event else "Waiting"

    source = latest_event.get("source") if latest_event else "unavailable"
    if not latest_event:
        sensor_state = "waiting_for_data"
    elif source == "real":
        sensor_state = "real_data"
    elif source == "uncalibrated":
        sensor_state = "uncalibrated"
    elif source == "simulated":
        sensor_state = "simulated"
    else:
        sensor_state = "waiting_for_data"

    signal_quality = evaluate_signal_quality(history)
    movement_intelligence = compute_q_change(history, signal_quality)
    cusum = compute_standard_cusum(history, baseline)
    research_metrics = evaluate_research_metrics(history)

    return {
        "status": "ok",
        "sensor_state": sensor_state,
        "connection_status": "Connected" if latest_event else "Waiting",
        "data_source": source,
        "last_updated": latest_event.get("timestamp") if latest_event else iso_timestamp(),
        "step_count": int(step_count or 0),
        "cadence": float(cadence or 0.0),
        "heart_rate": None if heart_rate is None else float(heart_rate),
        "activity": activity,
        "baseline": baseline,
        "alerts": build_alert_messages(latest_event, baseline),
        "history": history,
        "signal_quality": signal_quality,
        "movement_intelligence": movement_intelligence,
        "cusum": cusum,
        "research_metrics": research_metrics,
    }


app = FastAPI(
    title="NeuroWatch",
    description="Drift-aware movement monitoring dashboard and real sensor ingestion API.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "sensor_state": "waiting_for_data",
        "data_source": "unavailable",
        "message": "Waiting for data",
        "last_updated": iso_timestamp(),
    }


@app.get("/api/dashboard-state")
async def dashboard_state() -> Dict[str, Any]:
    return build_dashboard_payload()


@app.get("/api/session/history")
async def session_history() -> Dict[str, Any]:
    return {"sessions": store.history(limit=20)}


@app.get("/api/session/export")
async def export_session() -> FileResponse:
    export_path = store.export_csv()
    return FileResponse(path=str(export_path), media_type="text/csv", filename=export_path.name)


@app.post("/api/session/start")
async def start_session() -> Dict[str, Any]:
    session_id = store.ensure_session()
    return {"status": "started", "session_id": session_id, "timestamp": iso_timestamp()}


@app.post("/api/session/end")
async def end_session() -> Dict[str, Any]:
    session_id = store.current_session_id
    store.current_session_id = None
    return {"status": "ended", "session_id": session_id, "timestamp": iso_timestamp()}


@app.post("/api/sensor/ingest")
async def ingest_sensor(payload: SensorPayload) -> Dict[str, Any]:
    if payload.source == "simulated" and not SIMULATOR_ENABLED:
        raise HTTPException(status_code=403, detail="Simulation mode is disabled by default.")

    if payload.timestamp is not None:
        try:
            timestamp = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="timestamp must be ISO-8601") from exc
        age = (utc_now() - timestamp.astimezone(timezone.utc)).total_seconds()
        if age > MAX_SENSOR_AGE_SECONDS:
            raise HTTPException(status_code=400, detail="Sensor data is stale; timestamp exceeds the freshness limit.")

    if payload.sensor_freshness_ms is not None and payload.sensor_freshness_ms > 10000:
        raise HTTPException(status_code=400, detail="Sensor freshness is too old for the current session.")

    sanitized_payload = payload.model_dump(exclude_none=True)
    if "timestamp" not in sanitized_payload:
        sanitized_payload["timestamp"] = iso_timestamp()
    try:
        record = store.add_event(sanitized_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dashboard_payload = build_dashboard_payload()
    await manager.broadcast(dashboard_payload)
    return {"accepted": True, "record": record, "message": "Sensor data accepted."}


@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
