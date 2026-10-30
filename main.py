from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Deque, Dict, List, Optional
from xml.sax.saxutils import escape

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.requests import Request
from movement import AXES, ProfileStore, combine_movement_outputs
from neuroml.service import MovementModelService

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
EXPORTS_DIR = DATA_DIR / "exports"
SESSION_FILE = SESSIONS_DIR / "session_store.json"
PROFILE_FILE = DATA_DIR / "profiles" / "profiles.json"

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


def iso_timestamp_precise(value: Optional[datetime] = None) -> str:
    dt = value or utc_now()
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    temperature_c: Optional[float] = None

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
            if not math.isfinite(numeric):
                raise ValueError(f"Sensor value for '{key}' must be finite")
            cleaned[key] = numeric

        sensor_keys = {"ax", "ay", "az", "gx", "gy", "gz"}
        if any(key in cleaned for key in sensor_keys) and not sensor_keys.issubset(cleaned.keys()):
            missing = sorted(sensor_keys - cleaned.keys())
            raise ValueError(f"Incomplete IMU payload. Missing axes: {missing}")
        return cleaned


    @field_validator("temperature_c", "sensor_freshness_ms")
    @classmethod
    def validate_finite_optional_values(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not math.isfinite(value):
            raise ValueError("Optional sensor values must be finite")
        return value


class ProfileCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=80)
    profile_id: Optional[str] = Field(default=None, max_length=40)


class CalibrationStart(BaseModel):
    duration_seconds: int = Field(default=120, ge=120, le=180)
    sensor_placement: Optional[str] = None

    @field_validator("duration_seconds")
    @classmethod
    def validate_duration(cls, value: int) -> int:
        if value not in (120, 150, 180):
            raise ValueError("duration_seconds must be 120, 150, or 180")
        return value


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

    def _save(self) -> None:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SESSION_FILE.open("w", encoding="utf-8") as handle:
            json.dump({"current_session_id": self.current_session_id, "events": self.events}, handle, indent=2)

    def ensure_session(self) -> str:
        if self.current_session_id is None:
            self.current_session_id = f"session-{iso_timestamp().replace(':', '').replace('-', '').replace('Z', '')}"
        return self.current_session_id

    def add_event(
        self,
        payload: Dict[str, Any],
        profile_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        sensor_name = payload.get("sensor", "unknown")
        sequence_number = payload.get("sequence_number")
        if sequence_number is not None:
            last_seen = self.sequence_numbers.get(sensor_name)
            if last_seen is not None and int(sequence_number) <= int(last_seen):
                raise ValueError(f"Out-of-order or duplicate sequence number for {sensor_name}.")
            self.sequence_numbers[sensor_name] = int(sequence_number)

        event = {
            "session_id": profile_context["context_id"] if profile_context else self.ensure_session(),
            "timestamp": payload.get("timestamp") or iso_timestamp(),
            "received_at": iso_timestamp_precise(),
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
            "temperature_c": payload.get("temperature_c"),
        }
        if profile_context is not None:
            event.update({
                "profile_id": profile_context["profile_id"],
                "profile_context_id": profile_context["context_id"],
                "profile_context_type": profile_context["context_type"],
            })
        if profile_context is None:
            self.events.append(event)
            self._save()
        return event

    def export_csv(self) -> Path:
        export_path = EXPORTS_DIR / f"session_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        fieldnames = [
            "timestamp",
            "received_at",
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
                if event.get("profile_id") is not None:
                    continue
                writer.writerow(
                    {
                        "timestamp": event.get("timestamp", ""),
                        "received_at": event.get("received_at", ""),
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
profile_store = ProfileStore(PROFILE_FILE)
ml_service = MovementModelService()

# Live rolling window used by the exploratory movement classifier.
_ml_samples = deque(maxlen=400)
_ml_context: Optional[Dict[str, str]] = None
_ml_prediction = {
    "state": "waiting_for_data",
    "output": None,
    "message": "Waiting for valid live IMU samples.",
}


def _reset_ml_context() -> None:
    global _ml_context, _ml_prediction
    _ml_context = None
    _ml_samples.clear()
    _ml_prediction = {
        "state": "waiting_for_data",
        "output": None,
        "message": "Waiting for valid samples in the active monitoring session.",
    }

_sensor_diagnostics_lock = Lock()
_sensor_diagnostics: Dict[str, Any] = {
    "invalid_packet_count": 0,
    "last_invalid_at": None,
    "last_invalid_reason": None,
}


def _record_invalid_sensor_packet(reason: str) -> None:
    with _sensor_diagnostics_lock:
        _sensor_diagnostics["invalid_packet_count"] += 1
        _sensor_diagnostics["last_invalid_at"] = iso_timestamp()
        _sensor_diagnostics["last_invalid_reason"] = reason


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


class ReportChart(Flowable):
    def __init__(
        self,
        title: str,
        unit: str,
        samples: List[Dict[str, Any]],
        series: List[Dict[str, Any]],
    ) -> None:
        super().__init__()
        self.title = title
        self.unit = unit
        self.samples = samples
        self.series = series
        self.width = 178 * mm
        self.height = 65 * mm

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self.width = min(self.width, available_width)
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        left, right, bottom, top = 43, self.width - 8, 24, self.height - 19
        canvas.setFillColor(colors.HexColor("#18314b"))
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(0, self.height - 10, self.title)
        observed = [
            float(sample["values"][entry["axis"]])
            if entry.get("axis")
            else math.sqrt(sum(float(sample["values"][axis]) ** 2 for axis in entry["axes"]))
            for entry in self.series
            for sample in self.samples
        ]
        reference_values = [
            value
            for entry in self.series
            for value in (
                entry["reference"]["mean"] - entry["reference"]["standard_deviation"],
                entry["reference"]["mean"] + entry["reference"]["standard_deviation"],
            )
        ]
        if not observed:
            canvas.setFillColor(colors.grey)
            canvas.setFont("Helvetica", 9)
            canvas.drawCentredString(self.width / 2, self.height / 2, "No eligible session readings were recorded.")
            return
        low = min(observed + reference_values)
        high = max(observed + reference_values)
        if high - low < 1e-9:
            margin = max(abs(high) * 0.05, 0.5)
        else:
            margin = (high - low) * 0.12
        low -= margin
        high += margin
        plot_width, plot_height = right - left, top - bottom

        def y(value: float) -> float:
            return bottom + (value - low) * plot_height / (high - low)

        canvas.setStrokeColor(colors.HexColor("#dbe5ef"))
        canvas.setLineWidth(0.5)
        canvas.line(left, bottom, left, top)
        canvas.line(left, bottom, right, bottom)
        canvas.setFont("Helvetica", 7)
        for tick in range(5):
            value = high - (high - low) * tick / 4
            y_pos = bottom + plot_height * tick / 4
            canvas.setStrokeColor(colors.HexColor("#d8e0e8"))
            canvas.line(left, y_pos, right, y_pos)
            canvas.setFillColor(colors.HexColor("#425466"))
            canvas.drawRightString(left - 4, y_pos - 2, f"{value:.2f}")
        canvas.saveState()
        canvas.translate(8, bottom + plot_height / 2)
        canvas.rotate(90)
        canvas.setFillColor(colors.HexColor("#425466"))
        canvas.drawCentredString(0, 0, self.unit)
        canvas.restoreState()

        palette = [
            colors.HexColor("#0c8b7b"),
            colors.HexColor("#3478c8"),
            colors.HexColor("#8052b6"),
        ]
        for index, entry in enumerate(self.series):
            color = palette[index % len(palette)]
            reference = entry["reference"]
            canvas.setFillColor(colors.Color(color.red, color.green, color.blue, alpha=0.12))
            band_bottom = y(reference["mean"] - reference["standard_deviation"])
            band_top = y(reference["mean"] + reference["standard_deviation"])
            canvas.rect(left, band_bottom, plot_width, max(1, band_top - band_bottom), fill=1, stroke=0)
            canvas.setStrokeColor(color)
            canvas.setDash(3, 2)
            canvas.setLineWidth(0.8)
            canvas.line(left, y(reference["mean"]), right, y(reference["mean"]))
            canvas.setDash()
            points: List[tuple[float, float]] = []
            sample_times = [
                _report_timestamp(sample.get("sensor_timestamp") or sample.get("timestamp"))
                for sample in self.samples
            ]
            first_time = min(sample_times) if sample_times else 0.0
            last_time = max(sample_times) if sample_times else first_time
            for sample_index, sample in enumerate(self.samples):
                if entry.get("axis"):
                    value = float(sample["values"][entry["axis"]])
                else:
                    value = math.sqrt(sum(float(sample["values"][axis]) ** 2 for axis in entry["axes"]))
                if last_time > first_time:
                    x_pos = left + plot_width * (sample_times[sample_index] - first_time) / (last_time - first_time)
                else:
                    x_pos = left + plot_width * sample_index / max(1, len(self.samples) - 1)
                points.append((x_pos, y(value)))
            if len(points) > 1:
                path = canvas.beginPath()
                path.moveTo(*points[0])
                for point in points[1:]:
                    path.lineTo(*point)
                canvas.setStrokeColor(color)
                canvas.setLineWidth(1)
                canvas.drawPath(path, stroke=1, fill=0)
            canvas.setFillColor(color)
            canvas.setFont("Helvetica", 7)
            legend_x = 120 + index * 100
            canvas.drawString(legend_x, self.height - 10, f"{entry['label']} session / baseline")
        if self.samples:
            canvas.setFillColor(colors.HexColor("#425466"))
            canvas.setFont("Helvetica", 6)
            canvas.drawString(left, 9, str(self.samples[0].get("sensor_timestamp") or self.samples[0].get("timestamp", "")))
            canvas.drawRightString(right, 9, str(self.samples[-1].get("sensor_timestamp") or self.samples[-1].get("timestamp", "")))


def _report_timestamp(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def build_session_pdf(report: Dict[str, Any]) -> bytes:
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"NeuroWatch Session Report {report['session']['session_id']}",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], alignment=TA_CENTER, textColor=colors.HexColor("#113b52")))
    styles.add(ParagraphStyle(name="SmallNote", parent=styles["BodyText"], fontSize=8, leading=10))
    story: List[Flowable] = [
        Paragraph("NeuroWatch Session Report", styles["ReportTitle"]),
        Paragraph(f"Generated {iso_timestamp()}", styles["BodyText"]),
        Spacer(1, 5 * mm),
    ]

    def section(title: str) -> None:
        story.extend([Spacer(1, 3 * mm), Paragraph(title, styles["Heading2"]), Spacer(1, 2 * mm)])

    def table(rows: List[List[Any]], widths: List[float]) -> None:
        rendered = [[Paragraph(str(cell), styles["SmallNote"]) for cell in row] for row in rows]
        item = Table(rendered, colWidths=widths, repeatRows=1, hAlign="LEFT")
        item.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce8f1")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#10283d")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#aab8c5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(item)

    profile, session, baseline = report["profile"], report["session"], report["baseline"]
    section("Profile and session")
    table([
        ["Field", "Value"],
        ["Profile", f"{profile['display_name']} ({profile['profile_id']})"],
        ["Session ID", session["session_id"]],
        ["Session start / end", f"{session['started_at']} / {session['ended_at']}"],
        ["Duration", f"{session['duration_seconds']} seconds"],
        ["Baseline calibration", f"{baseline['calibration_id']} · calibrated {baseline['calibrated_at']}"],
        ["Baseline completed", baseline["completed_at"]],
    ], [42 * mm, 138 * mm])

    section("Session quality and packet statistics")
    counts = report["packet_counts"]
    table([
        ["Received", "Accepted", "Rejected", "Effective rate", "Quality"],
        [counts["received"], counts["accepted"], counts["rejected"],
         f"{report['effective_sample_rate_hz']:.3f} Hz" if report["effective_sample_rate_hz"] is not None else "Unavailable",
         report["quality"].get("status", "unavailable")],
    ], [30 * mm, 30 * mm, 30 * mm, 42 * mm, 48 * mm])

    section("Baseline versus session axis statistics")
    rows = [["Axis", "Unit", "Baseline mean ± SD", "Baseline min – max", "Session mean ± SD", "Session min – max", "Abs. diff", "Diff %", "z diff"]]
    for axis in AXES:
        reference = baseline["axes"][axis]
        measured = report["statistics"]["axes"].get(axis)
        comparison = report["comparisons"]["axes"][axis]
        unit = comparison["unit"] if report["units_verified"] else "unit unverified"
        rows.append([
            axis.upper(), unit,
            f"{reference['mean']:.4g} ± {reference['standard_deviation']:.3g}",
            f"{reference['minimum']:.4g} – {reference['maximum']:.4g}",
            f"{measured['mean']:.4g} ± {measured['standard_deviation']:.3g}" if measured else "Unavailable",
            f"{measured['minimum']:.4g} – {measured['maximum']:.4g}" if measured else "Unavailable",
            f"{comparison['absolute_difference']:.4g}" if comparison.get("absolute_difference") is not None else "Unavailable",
            f"{comparison['percentage_difference']:.2f}%" if comparison.get("percentage_difference") is not None else "Unavailable",
            f"{comparison['standardized_deviation']:.3g}" if comparison.get("standardized_deviation") is not None else "Unavailable",
        ])
    table(rows, [10 * mm, 14 * mm, 25 * mm, 20 * mm, 25 * mm, 20 * mm, 20 * mm, 18 * mm, 18 * mm])

    section("Acceleration and gyroscope time series")
    samples = report["samples"]
    for group, axes, unit in (
        ("Acceleration", ("ax", "ay", "az"), "m/s^2" if report["units_verified"] else "unit unverified"),
        ("Gyroscope", ("gx", "gy", "gz"), "deg/s" if report["units_verified"] else "unit unverified"),
    ):
        chart_series = [
            {"axis": axis, "label": axis.upper(), "reference": baseline["axes"][axis]}
            for axis in axes
        ]
        story.append(ReportChart(group, unit, samples, chart_series))
        story.append(Spacer(1, 3 * mm))

    section("Movement magnitude statistics and deviations")
    rows = [["Feature · unit", "Baseline mean ± SD", "Baseline min–max · range", "Session mean ± SD", "Session min–max · range", "Abs. diff", "Diff %", "z diff"]]
    for name, label in (("acceleration_magnitude", "Acceleration magnitude"), ("gyroscope_magnitude", "Gyroscope magnitude")):
        comparison = report["comparisons"]["magnitudes"][name]
        reference, measured = comparison["baseline"], comparison["session"]
        rows.append([
            f"{label} · {reference['unit'] if report['units_verified'] else 'unit unverified'}",
            f"{reference['mean']:.4g} ± {reference['standard_deviation']:.3g}",
            f"{reference['minimum']:.4g}–{reference['maximum']:.4g} · {reference['range']:.4g}",
            f"{measured['mean']:.4g} ± {measured['standard_deviation']:.3g}" if measured else "Unavailable",
            f"{measured['minimum']:.4g}–{measured['maximum']:.4g} · {measured['range']:.4g}" if measured else "Unavailable",
            f"{comparison['absolute_difference']:.4g}" if comparison.get("absolute_difference") is not None else "Unavailable",
            f"{comparison['percentage_difference']:.2f}%" if comparison.get("percentage_difference") is not None else "Unavailable",
            f"{comparison['standardized_deviation']:.3g}" if comparison.get("standardized_deviation") is not None else "Unavailable",
        ])
    table(rows, [26 * mm, 25 * mm, 29 * mm, 25 * mm, 29 * mm, 18 * mm, 16 * mm, 12 * mm])
    for name, axes, unit in (
        ("Acceleration magnitude", ("ax", "ay", "az"), "m/s^2" if report["units_verified"] else "unit unverified"),
        ("Gyroscope magnitude", ("gx", "gy", "gz"), "deg/s" if report["units_verified"] else "unit unverified"),
    ):
        reference = baseline["features"][("acceleration_magnitude" if axes[0] == "ax" else "gyroscope_magnitude")]
        story.append(ReportChart(
            name,
            unit,
            samples,
            [{"axes": axes, "label": "Magnitude", "reference": reference}],
        ))

    section("Largest standardized differences")
    if report["insights"]:
        table(
            [["Axis", "Absolute difference", "Standardized difference", "Unit"]]
            + [[entry["feature"].upper(), f"{entry['absolute_difference']:.4g}",
                f"{entry['standardized_deviation']:.3g}", entry["unit"]]
               for entry in report["insights"]],
            [45 * mm, 45 * mm, 50 * mm, 40 * mm],
        )
    else:
        story.append(Paragraph("No axis standardized differences are available.", styles["BodyText"]))
    if report["fallback_insights"]:
        table(
            [["Axis", "Absolute difference", "Percentage difference", "Interpretation"]]
            + [[entry["feature"].upper(), f"{entry['absolute_difference']:.4g} {entry['unit']}",
                f"{entry['percentage_difference']:.2f}%" if entry["percentage_difference"] is not None else "Unavailable",
                "Standardized difference unavailable because baseline SD is near zero."]
               for entry in report["fallback_insights"]],
            [20 * mm, 36 * mm, 37 * mm, 87 * mm],
        )

    section("Variability changes")
    variability_rows = [["Feature", "Baseline SD", "Session SD", "SD change", "Change %"]]
    for axis, comparison in report["comparisons"]["axes"].items():
        if comparison.get("variability_difference") is None:
            continue
        unit = comparison["unit"] if report["units_verified"] else "unit unverified"
        variability_rows.append([
            axis.upper(),
            f"{comparison['baseline_standard_deviation']:.4g} {unit}",
            f"{comparison['session_standard_deviation']:.4g} {unit}",
            f"{comparison['variability_difference']:.4g} {unit}",
            f"{comparison['variability_percentage_difference']:.2f}%"
            if comparison["variability_percentage_difference"] is not None else "Unavailable",
        ])
    for name, comparison in report["comparisons"]["magnitudes"].items():
        measured = comparison.get("session")
        if measured is None:
            continue
        unit = comparison["baseline"]["unit"] if report["units_verified"] else "unit unverified"
        variability_rows.append([
            name.replace("_", " ").title(),
            f"{comparison['baseline']['standard_deviation']:.4g} {unit}",
            f"{measured['standard_deviation']:.4g} {unit}",
            f"{comparison['variability_difference']:.4g} {unit}",
            f"{comparison['variability_percentage_difference']:.2f}%"
            if comparison["variability_percentage_difference"] is not None else "Unavailable",
        ])
    if len(variability_rows) > 1:
        table(
            variability_rows,
            [38 * mm, 34 * mm, 34 * mm, 36 * mm, 36 * mm],
        )
    else:
        story.append(Paragraph("Variability percentage changes are unavailable because baseline variability is zero or near zero.", styles["BodyText"]))

    model_analysis = report.get("model_analysis", {})
    section("ML movement analysis")
    model_class = (model_analysis.get("output") or {}).get("class")
    table([
        ["Field", "Recorded result"],
        ["Model", escape(str(model_analysis.get("model_name") or "Unavailable"))],
        ["Model version", escape(str(model_analysis.get("model_version") or "Unavailable"))],
        ["Model status", escape(str(model_analysis.get("state", "unavailable")))],
        ["Data quality", escape(str(model_analysis.get("data_quality", "unavailable")))],
        ["Movement-pattern class", escape(str(model_class or "No model output"))],
        ["Personal feature comparison", escape(str(report.get("movement_feature_comparisons", {}).get("status", "unavailable")))],
        ["Experimental repeated-session flag", escape(str(report.get("longitudinal_change", {}).get("state", "insufficient_data")))],
    ], [62 * mm, 118 * mm])
    if model_analysis.get("message"):
        story.append(Paragraph(escape(str(model_analysis["message"])), styles["SmallNote"]))
    if model_class is not None:
        story.append(Paragraph(
            "The class is the model's dataset label output only. No calibrated confidence, diagnosis, disease risk, or severity score is provided.",
            styles["SmallNote"],
        ))
    movement_comparisons = report.get("movement_feature_comparisons", {}).get("features", {})
    if movement_comparisons:
        comparison_rows = [["Feature", "Baseline", "Session", "Absolute change", "Relative change"]]
        for feature, comparison in movement_comparisons.items():
            comparison_rows.append([
                escape(feature),
                f"{comparison['baseline_mean']:.5g} {comparison['unit']}",
                f"{comparison['session_mean']:.5g} {comparison['unit']}",
                f"{comparison['absolute_difference']:.5g} {comparison['unit']}",
                f"{comparison['relative_change_percent']:.3g}%"
                if comparison["relative_change_percent"] is not None else "Unavailable",
            ])
        table(comparison_rows, [52 * mm, 30 * mm, 30 * mm, 34 * mm, 34 * mm])
    if report.get("longitudinal_change", {}).get("state") == "persistent_deviation":
        story.append(Paragraph(
            "Repeated feature deviations are experimental and are not evidence of Parkinson's progression. "
            "Discuss persistent concerns with a clinician.",
            styles["SmallNote"],
        ))

    section("Detected movement-change events")
    if report["events"]:
        table(
            [["Timestamp", "Severity", "Description"]]
            + [[event.get("timestamp", ""), event.get("severity", ""), event.get("description", "")]
               for event in report["events"]],
            [42 * mm, 25 * mm, 113 * mm],
        )
    else:
        story.append(Paragraph("No movement-change events were detected during this session.", styles["BodyText"]))

    section("Limitations and interpretation")
    limitations = report["limitations"] or ["No additional data-quality limitations were identified by the available checks."]
    for limitation in limitations:
        story.append(Paragraph(f"• {limitation}", styles["SmallNote"]))
    story.extend([
        Spacer(1, 3 * mm),
        Paragraph(
            "Disclaimer: These are experimental movement-analysis results based on the recorded sensor data. "
            "They are not a medical diagnosis, clinical assessment, or a substitute for professional care.",
            styles["SmallNote"],
        ),
    ])
    document.build(story)
    return output.getvalue()


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

        timestamp_value = event.get("received_at") or event.get("timestamp")
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


def build_dashboard_payload(profile_id: Optional[str] = None) -> Dict[str, Any]:
    profile_state = profile_store.dashboard_state(profile_id)
    selected_profile = profile_state["selected_profile"]
    profile_samples = profile_state["profile_samples"]
    if selected_profile and profile_samples:
        history = [
            {
                "session_id": sample["session_id"],
                "profile_id": sample["profile_id"],
                "timestamp": sample.get("sensor_timestamp") or sample["timestamp"],
                "received_at": sample["timestamp"],
                "sensor": sample.get("sensor", "unknown"),
                "source": sample.get("source", "real"),
                "values": sample.get("values", {}),
                "units": sample.get("units", {}),
                "sequence_number": sample.get("sequence_number"),
                "sensor_freshness_ms": sample.get("sensor_freshness_ms"),
                "temperature_c": sample.get("temperature_c"),
            }
            for sample in profile_samples
        ]
    else:
        history = [
            event for event in store.events
            if event.get("profile_id") is None
        ][-50:]
    latest_event = history[-1] if history else None
    saved_baseline = profile_state["baseline"]
    baseline = {
        axis: saved_baseline["axes"][axis]["mean"] if saved_baseline else None
        for axis in AXES
    }
    step_count = latest_event.get("step_count") if latest_event else 0
    cadence = latest_event.get("cadence") if latest_event else 0.0
    heart_rate = latest_event.get("heart_rate") if latest_event else None
    activity = latest_event.get("activity") if latest_event else "Waiting"

    source = latest_event.get("source") if latest_event else "unavailable"
    latest_sensor_age = None
    if latest_event and latest_event.get("timestamp"):
        try:
            latest_sensor_age = (
                utc_now()
                - datetime.fromisoformat(latest_event["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
            ).total_seconds()
        except ValueError:
            latest_sensor_age = None
    if not latest_event:
        sensor_state = "waiting_for_data"
    elif latest_sensor_age is not None and latest_sensor_age > 10:
        sensor_state = "stale_data"
    elif source == "real":
        sensor_state = "real_data"
    elif source == "uncalibrated":
        sensor_state = "uncalibrated"
    elif source == "simulated":
        sensor_state = "simulated"
    else:
        sensor_state = "waiting_for_data"

    signal_quality = evaluate_signal_quality(history)
    comparison = profile_state["comparison"]
    comparison_available = bool(comparison and comparison.get("features"))
    comparison_state = (
        comparison.get("state")
        if comparison
        else ("uncalibrated" if not saved_baseline else "insufficient_data")
    )
    legacy_states = {
        "review": "candidate_change",
        "marked deviation": "confirmed_change",
        "poor_signal_quality": "uncertain",
        "insufficient_data": "uncertain",
        "uncalibrated": "uncalibrated",
    }
    movement_state = legacy_states.get(comparison_state, comparison_state)
    deviations = comparison.get("feature_deviations_z", {}) if comparison else {}
    reference_deviation = max((abs(value) for value in deviations.values()), default=None)
    sustained = bool(comparison and comparison.get("sustained"))
    movement_intelligence = {
        "implemented": True,
        "available": comparison_available,
        "state": movement_state,
        "score": comparison.get("score") if comparison else None,
        "reference_deviation": reference_deviation,
        "persistence_duration": 3 if sustained else 0,
        "signal_quality_status": comparison.get("signal_quality", {}).get("status", "insufficient_data") if comparison else "insufficient_data",
        "summary": comparison.get(
            "interpretation",
            "Select a profile with a completed calibration to compare movement against its fixed saved baseline.",
        ) if comparison else "Select a profile with a completed calibration to compare movement against its fixed saved baseline.",
    }
    cusum = {
        "implemented": True,
        "available": comparison_available,
        "state": movement_state,
        "score": movement_intelligence["score"],
        "reference_deviation": reference_deviation,
        "summary": "Legacy field retained for compatibility; use profile_state.comparison for fixed-baseline results.",
    }
    research_metrics = evaluate_research_metrics(history)
    monitoring = profile_state.get("monitoring", {})
    ml_prediction = dict(_ml_prediction)
    expected_context = {
        "profile_id": monitoring.get("profile_id"),
        "session_id": monitoring.get("session_id"),
    }
    if (
        monitoring.get("state") == "idle"
        or _ml_context != expected_context
        or not expected_context["session_id"]
    ):
        ml_prediction = {
            "state": "waiting_for_data",
            "output": None,
            "message": "No prediction is available for the selected active monitoring session.",
        }
    combined_interpretation = combine_movement_outputs(comparison, ml_prediction)
    alerts = (
        [comparison.get("interpretation", "Experimental movement comparison available.")]
        if comparison_available
        else ["No selected calibrated profile; movement comparison is unavailable."]
    )
    temperature_records = []
    for event in history:
        temperature = event.get("temperature_c")
        if temperature is None:
            temperature = (event.get("values") or {}).get("temperature_c")
        try:
            numeric_temperature = float(temperature)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric_temperature):
            temperature_records.append({
                "timestamp": event.get("received_at") or event.get("timestamp"),
                "sensor_timestamp": event.get("timestamp"),
                "temperature_c": numeric_temperature,
            })
    temperature_state = "absent"
    latest_reading = temperature_records[-1] if temperature_records else None
    latest_temperature = latest_reading["temperature_c"] if latest_reading else None
    temperatures = [
        reading for reading in temperature_records
        if -40 <= reading["temperature_c"] <= 125
    ]
    if latest_temperature is not None:
        temperature_state = "available" if -40 <= latest_temperature <= 125 else "implausible"
        if temperature_state == "implausible":
            latest_temperature = None
        elif latest_reading and latest_reading.get("timestamp"):
            try:
                sensor_timestamp = latest_reading.get("sensor_timestamp") or latest_reading["timestamp"]
                temperature_age = (utc_now() - datetime.fromisoformat(sensor_timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)).total_seconds()
            except ValueError:
                temperature_age = None
            if temperature_age is not None and temperature_age > MAX_SENSOR_AGE_SECONDS:
                temperature_state = "stale"
                latest_temperature = None

    return {
        "status": "ok",
        "sensor_state": sensor_state,
        "connection_status": "Stale data" if latest_sensor_age is not None and latest_sensor_age > 10 else "Connected" if latest_event else "Waiting",
        "data_source": source,
        "last_updated": latest_event.get("timestamp") if latest_event else iso_timestamp(),
        "last_received_at": latest_event.get("received_at") if latest_event else None,
        "step_count": int(step_count or 0),
        "cadence": float(cadence or 0.0),
        "heart_rate": None if heart_rate is None else float(heart_rate),
        "activity": activity,
        "baseline": baseline,
        "alerts": alerts,
        "history": history,
        "signal_quality": signal_quality,
        "movement_intelligence": movement_intelligence,
        "cusum": cusum,
        "research_metrics": research_metrics,
        "profile_state": profile_state,
        "ml_analysis_status": ml_service.status(),
        "ml_prediction": ml_prediction,
        "combined_interpretation": combined_interpretation,
        "sensor_temperature": {
            "state": temperature_state,
            "temperature_c": latest_temperature,
            "history": temperatures[-50:],
            "implausible_count": sum(
                not (-40 <= reading["temperature_c"] <= 125)
                for reading in temperature_records
            ),
            "label": "Sensor/chip temperature, not body temperature.",
        },
        "sensor_diagnostics": dict(_sensor_diagnostics),
    }


app = FastAPI(
    title="NeuroWatch",
    description="Drift-aware movement monitoring dashboard and real sensor ingestion API.",
    version="0.1.0",
)


@app.exception_handler(RequestValidationError)
async def validation_error_response(request: Request, exc: RequestValidationError) -> JSONResponse:
    if request.url.path == "/api/sensor/ingest":
        _record_invalid_sensor_packet("Sensor packet failed request validation.")
    errors = [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", "Invalid request."),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


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


@app.get("/session-report/{session_id}")
async def session_report_page(session_id: str) -> Response:
    try:
        profile_store.completed_session(session_id)
    except KeyError:
        return RedirectResponse("/", status_code=303)
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
async def dashboard_state(profile_id: Optional[str] = None) -> Dict[str, Any]:
    if profile_id is not None:
        try:
            profile_store.profile(profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return build_dashboard_payload(profile_id)


@app.get("/api/session/history")
async def session_history(profile_id: Optional[str] = None) -> Dict[str, Any]:
    if profile_id is not None:
        try:
            history = profile_store.history(profile_id)
            history["completed_sessions"] = profile_store.completed_session_history(profile_id)
            return history
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    unassigned_events = [
        event for event in store.events
        if event.get("profile_id") is None
    ]
    return {
        "sessions": unassigned_events[-20:],
        "completed_sessions": profile_store.completed_session_history(),
    }


@app.get("/api/session/{session_id}/analysis")
async def session_analysis(session_id: str, profile_id: Optional[str] = None) -> Dict[str, Any]:
    try:
        report = profile_store.completed_session(session_id, profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not report.get("analysis_available", True):
        raise HTTPException(status_code=409, detail="Saved analysis is unavailable for this completed session.")
    return report


@app.get("/api/session/{session_id}/report.pdf")
async def session_report_pdf(session_id: str, profile_id: Optional[str] = None) -> Response:
    try:
        report = profile_store.completed_session(session_id, profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    pdf = build_session_pdf(report)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="neurowatch-{session_id}.pdf"'},
    )


@app.get("/api/session/export")
async def export_session(
    profile_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> FileResponse:
    if profile_id is not None:
        try:
            content = profile_store.export_csv(profile_id, session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        export_path = EXPORTS_DIR / f"profile_{profile_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        export_path.write_text(content, encoding="utf-8")
        return FileResponse(path=str(export_path), media_type="text/csv", filename=export_path.name)
    export_path = store.export_csv()
    return FileResponse(path=str(export_path), media_type="text/csv", filename=export_path.name)


@app.post("/api/session/start")
async def start_session() -> Dict[str, Any]:
    session_id = store.ensure_session()
    return {"status": "started", "session_id": session_id, "timestamp": iso_timestamp()}


@app.post("/api/session/end")
async def end_session() -> Dict[str, Any]:
    if profile_store.monitoring is not None:
        try:
            report = profile_store.end_monitoring(ml_service.analyze)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store.current_session_id = None
        await manager.broadcast(build_dashboard_payload(report["profile"]["profile_id"]))
        return {
            "status": "ended",
            "session_id": report["session"]["session_id"],
            "profile_session": report,
            "report": report,
            "timestamp": iso_timestamp(),
        }
    if store.current_session_id is None:
        raise HTTPException(status_code=409, detail="There is no active session to end.")
    session_id = store.current_session_id
    store.current_session_id = None
    return {
        "status": "ended",
        "session_id": session_id,
        "profile_session": None,
        "timestamp": iso_timestamp(),
    }


@app.get("/api/profiles")
async def list_profiles() -> Dict[str, Any]:
    return {"profiles": profile_store.list_profiles()}


@app.post("/api/profiles", status_code=201)
async def create_profile(payload: ProfileCreate) -> Dict[str, Any]:
    try:
        profile = profile_store.create_profile(payload.display_name, payload.profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "profile_id": profile["profile_id"],
        "display_name": profile["display_name"],
        "created_at": profile["created_at"],
        "baseline_available": False,
    }


@app.get("/api/profiles/{profile_id}")
async def get_profile(profile_id: str) -> Dict[str, Any]:
    try:
        return profile_store.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/profiles/{profile_id}/calibrations")
async def start_calibration(profile_id: str, payload: CalibrationStart) -> Dict[str, Any]:
    try:
        state = profile_store.start_calibration(
            profile_id,
            payload.duration_seconds,
            payload.sensor_placement,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await manager.broadcast(build_dashboard_payload(profile_id))
    return state


@app.get("/api/calibration-state")
async def get_calibration_state() -> Dict[str, Any]:
    return profile_store.calibration_status()


@app.delete("/api/calibration")
async def cancel_calibration() -> Dict[str, Any]:
    try:
        state = profile_store.cancel_calibration()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await manager.broadcast(build_dashboard_payload(state.get("profile_id")))
    return state


@app.post("/api/profiles/{profile_id}/monitoring")
async def start_monitoring(profile_id: str) -> Dict[str, Any]:
    try:
        state = profile_store.start_monitoring(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _reset_ml_context()
    await manager.broadcast(build_dashboard_payload(profile_id))
    return state


@app.post("/api/monitoring/end")
async def end_monitoring() -> Dict[str, Any]:
    try:
        report = profile_store.end_monitoring(ml_service.analyze)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _reset_ml_context()
    await manager.broadcast(build_dashboard_payload(report["profile"]["profile_id"]))
    return report


@app.get("/api/profiles/{profile_id}/history")
async def profile_history(profile_id: str) -> Dict[str, Any]:
    try:
        return profile_store.history(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/profiles/{profile_id}/trends")
async def profile_trends(profile_id: str) -> Dict[str, Any]:
    try:
        profile = profile_store.profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"profile_id": profile_id, "sessions": profile_store._trends(profile)}


@app.post("/api/sensor/ingest")
async def ingest_sensor(payload: SensorPayload) -> Dict[str, Any]:
    global _ml_context, _ml_prediction
    if payload.source == "simulated" and not SIMULATOR_ENABLED:
        _record_invalid_sensor_packet("Simulation mode is disabled.")
        raise HTTPException(status_code=403, detail="Simulation mode is disabled by default.")

    sensor_age_seconds = None
    if payload.timestamp is not None:
        try:
            timestamp = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="timestamp must be ISO-8601") from exc
        age = (utc_now() - timestamp.astimezone(timezone.utc)).total_seconds()
        sensor_age_seconds = age
        if age > MAX_SENSOR_AGE_SECONDS:
            _record_invalid_sensor_packet("Sensor packet timestamp exceeded the freshness limit.")
            raise HTTPException(status_code=400, detail="Sensor data is stale; timestamp exceeds the freshness limit.")
        if age < -5:
            _record_invalid_sensor_packet("Sensor packet timestamp was too far in the future.")
            raise HTTPException(status_code=400, detail="Sensor timestamp is too far in the future.")

    if payload.sensor_freshness_ms is not None and payload.sensor_freshness_ms > 10000:
        _record_invalid_sensor_packet("Sensor-reported freshness exceeded the limit.")
        raise HTTPException(status_code=400, detail="Sensor freshness is too old for the current session.")

    sanitized_payload = payload.model_dump(exclude_none=True)
    values = sanitized_payload.get("values", {})
    if payload.temperature_c is not None:
        sanitized_payload["temperature_c"] = payload.temperature_c
    elif "temperature_c" in values:
        sanitized_payload["temperature_c"] = values["temperature_c"]
    if "timestamp" not in sanitized_payload:
        sanitized_payload["timestamp"] = iso_timestamp()
    try:
        record = store.add_event(sanitized_payload, profile_store.packet_context())
    except ValueError as exc:
        _record_invalid_sensor_packet(str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    expected_accel = {"m/s^2", "m/s²"}
    expected_gyro = {"deg/s", "°/s"}
    axes_valid = all(axis in payload.values and math.isfinite(payload.values[axis]) for axis in AXES)
    units_valid = all(payload.units.get(axis) in expected_accel for axis in ("ax", "ay", "az")) and all(
        payload.units.get(axis) in expected_gyro for axis in ("gx", "gy", "gz")
    )
    freshness_valid = (
        sensor_age_seconds is not None
        and -5 <= sensor_age_seconds <= 3
        and (payload.sensor_freshness_ms is None or payload.sensor_freshness_ms <= 3000)
    )
    source_valid = payload.source == "real" or (payload.source == "simulated" and SIMULATOR_ENABLED)
    eligible = source_valid and axes_valid and units_valid and freshness_valid
    reason = None
    if not source_valid:
        reason = "Calibration requires real packets, or explicitly enabled simulated test packets."
    elif not axes_valid:
        reason = "Calibration requires finite values for all six IMU axes."
    elif not units_valid:
        reason = "Calibration requires explicit units: acceleration in m/s^2 and gyroscope in deg/s."
    elif not freshness_valid:
        reason = "Calibration requires a current sensor timestamp and freshness metadata."
    received_at = datetime.fromisoformat(record["received_at"].replace("Z", "+00:00"))
    context = profile_store.packet_context()
    accepted_sample = profile_store.record_packet(record, received_at, time.monotonic(), eligible, reason)

    # Only the sample accepted by the profile store enters the current session's ML path.
    if context and context.get("context_type") == "monitoring":
        context_key = {
            "profile_id": context["profile_id"],
            "session_id": context["context_id"],
        }
        if _ml_context != context_key:
            _reset_ml_context()
            _ml_context = context_key
        if accepted_sample is not None:
            _ml_samples.append(accepted_sample)
            _ml_prediction = ml_service.analyze(list(_ml_samples))

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
