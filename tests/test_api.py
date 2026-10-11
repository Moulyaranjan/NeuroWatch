import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from main import SensorPayload, app, export_session, health, ingest_sensor, store


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


def test_session_export_endpoint():
    payload = build_real_imu_payload(sequence_number=9)
    asyncio.run(ingest_sensor(payload))
    response = asyncio.run(export_session())
    assert response.path.endswith(".csv")
    assert response.media_type == "text/csv"
