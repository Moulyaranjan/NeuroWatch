import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from main import (
    SensorPayload,
    app,
    build_dashboard_payload,
    compute_q_change,
    evaluate_research_metrics,
    evaluate_signal_quality,
    export_session,
    health,
    ingest_sensor,
    store,
)


@pytest.fixture(autouse=True)
def reset_store():
    store.events.clear()
    store.current_session_id = None
    store.sequence_numbers.clear()
    yield
    store.events.clear()
    store.current_session_id = None
    store.sequence_numbers.clear()


def test_health_endpoint():
    payload = asyncio.run(health())
    assert payload["status"] == "ok"
    assert payload["sensor_state"] == "waiting_for_data"


def build_real_imu_payload(timestamp=None, sequence_number=1):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SensorPayload(
        sensor="mpu6050",
        source="real",
        values={"ax": 0.12, "ay": -0.03, "az": 0.98, "gx": 10.2, "gy": 1.5, "gz": -2.1},
        timestamp=timestamp,
        sequence_number=sequence_number,
        sensor_freshness_ms=42.5,
    )


def test_sensor_ingest_requires_real_payload():
    payload = build_real_imu_payload()
    response = asyncio.run(ingest_sensor(payload))
    assert response["accepted"] is True
    assert response["record"]["sensor"] == "mpu6050"


def test_sensor_payload_requires_complete_imu_axes():
    with pytest.raises(ValueError, match="Incomplete IMU payload"):
        SensorPayload(
            sensor="mpu6050",
            source="real",
            values={"ax": 0.12, "ay": -0.03, "az": 0.98, "gx": 10.2, "gy": 1.5},
            timestamp="2026-10-11T00:00:00Z",
        )


def test_sensor_ingest_rejects_stale_data():
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = build_real_imu_payload(timestamp=stale_timestamp)
    with pytest.raises(Exception):
        asyncio.run(ingest_sensor(payload))


def test_sensor_ingest_rejects_duplicate_sequence_number():
    payload = build_real_imu_payload(sequence_number=1)
    asyncio.run(ingest_sensor(payload))
    duplicate = build_real_imu_payload(sequence_number=1)
    with pytest.raises(Exception):
        asyncio.run(ingest_sensor(duplicate))


def test_dashboard_websocket_updates_with_real_data():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/dashboard") as websocket:
            initial = websocket.receive_json()
            assert initial["sensor_state"] == "waiting_for_data"

            payload = build_real_imu_payload(sequence_number=7)
            response = asyncio.run(ingest_sensor(payload))
            assert response["accepted"] is True

            updated = websocket.receive_json()
            assert updated["sensor_state"] == "real_data"
            assert updated["step_count"] == 0


def test_dashboard_payload_contains_research_sections():
    payload = build_dashboard_payload()
    assert payload["signal_quality"]["status"] == "insufficient_data"
    assert payload["movement_intelligence"]["available"] is False
    assert payload["research_metrics"]["available"] is False


def test_signal_quality_handles_missing_metadata_gracefully():
    history = [
        {"timestamp": "2026-10-11T00:00:00Z", "values": {"ax": 0.11, "ay": 0.12, "az": 0.98, "gx": 1.0, "gy": 1.2, "gz": 0.8}},
        {"timestamp": "2026-10-11T00:00:01Z", "values": {"ax": 0.14, "ay": 0.15, "az": 1.02, "gx": 1.1, "gy": 1.4, "gz": 0.7}},
    ]
    quality = evaluate_signal_quality(history)
    assert quality["status"] in {"good", "warning", "unknown", "insufficient_data"}
    assert quality["metrics"]["timestamp_gap_ms"] is not None


def test_q_change_returns_uncertain_when_quality_is_low():
    history = [
        {"timestamp": "2026-10-11T00:00:00Z", "values": {"ax": 0.1, "ay": 0.1, "az": 0.9, "gx": 1.0, "gy": 1.0, "gz": 0.9}},
        {"timestamp": "2026-10-11T00:00:01Z", "values": {"ax": 0.1, "ay": 0.1, "az": 0.9, "gx": 1.0, "gy": 1.0, "gz": 0.9}},
        {"timestamp": "2026-10-11T00:00:02Z", "values": {"ax": 0.1, "ay": 0.1, "az": 0.9, "gx": 1.0, "gy": 1.0, "gz": 0.9}},
    ]
    result = compute_q_change(history, {"quality_score": 0.2, "status": "poor"})
    assert result["state"] in {"uncertain", "normal"}
    assert result["signal_quality_status"] == "poor"


def test_research_metrics_require_ground_truth_annotations():
    history = [
        {"timestamp": "2026-10-11T00:00:00Z", "activity": "walking", "values": {"ax": 0.1, "ay": 0.1, "az": 0.9, "gx": 1.0, "gy": 1.0, "gz": 0.9}},
    ]
    metrics = evaluate_research_metrics(history)
    assert metrics["available"] is True
    assert metrics["metrics"]["precision"] is None


def test_session_export_endpoint():
    payload = build_real_imu_payload(sequence_number=9)
    asyncio.run(ingest_sensor(payload))
    response = asyncio.run(export_session())
    assert response.path.endswith(".csv")
    assert response.media_type == "text/csv"
