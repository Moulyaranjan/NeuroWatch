from __future__ import annotations

import csv
import json
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
