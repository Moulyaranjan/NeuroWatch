from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main
from movement import AXES, ProfileStore, combine_movement_outputs


UNITS = {
    "ax": "m/s^2", "ay": "m/s^2", "az": "m/s^2",
    "gx": "deg/s", "gy": "deg/s", "gz": "deg/s",
}


def _sample(index, stamp, sensor="sim-imu", source="simulated", values=None, units=None):
    values = values or {
        "ax": 0.08 + 0.002 * (index % 7),
        "ay": 0.03 + 0.001 * (index % 5),
        "az": 9.7 + 0.002 * (index % 11),
        "gx": 1.0 + 0.01 * (index % 9),
        "gy": 0.4 + 0.01 * (index % 4),
        "gz": -0.2 + 0.01 * (index % 6),
    }
    return {
        "timestamp": stamp.isoformat().replace("+00:00", "Z"),
        "sensor": sensor,
        "source": source,
        "sequence_number": index,
        "sensor_freshness_ms": 25,
        "units": units or UNITS,
        "values": values,
    }


@pytest.fixture
def isolated_integration(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(main, "profile_store", store)
    monkeypatch.setattr(main, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(main, "_sensor_diagnostics", {
        "invalid_packet_count": 0, "last_invalid_at": None, "last_invalid_reason": None,
    })
    main._reset_ml_context()
    main.store.events.clear()
    main.store.current_session_id = None
    main.store.sequence_numbers.clear()
    yield store
    main._reset_ml_context()
    main.store.events.clear()
    main.store.current_session_id = None
    main.store.sequence_numbers.clear()


def _calibrate(store, profile_id, monkeypatch, sensor="sim-imu"):
    store.create_profile(profile_id.title(), profile_id)
    clock = [500.0]
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    with monkeypatch.context() as patcher:
        patcher.setattr("movement.time.monotonic", lambda: clock[0])
        store.start_calibration(profile_id, 120)
        for index in range(601):
            stamp = start + timedelta(seconds=index / 5)
            item = _sample(index, stamp, sensor=sensor)
            store.record_packet(item, stamp, clock[0], True)
        clock[0] += 120
        assert store.calibration_status()["state"] == "completed"
    return store.profile(profile_id)["baseline"]


def _api_sample_payload(index, stamp, **overrides):
    item = _sample(index, stamp, **overrides)
    return item


def test_simulated_25hz_stream_is_shared_and_session_scoped(isolated_integration, monkeypatch):
    store = isolated_integration
    baseline = _calibrate(store, "person-a", monkeypatch)
    with TestClient(main.app) as client:
        assert client.post("/api/profiles/person-a/monitoring").status_code == 200
        initial_context = store.monitoring_status()["session_id"]

        base = datetime.now(timezone.utc) - timedelta(seconds=2.5)
        receive_index = [0]

        def simulated_receive_time():
            stamp = base + timedelta(seconds=receive_index[0] / 25)
            receive_index[0] += 1
            return stamp.isoformat().replace("+00:00", "Z")

        monkeypatch.setattr(main, "iso_timestamp_precise", simulated_receive_time)
        for index in range(101):
            stamp = base + timedelta(seconds=index / 25)
            response = client.post("/api/sensor/ingest", json=_api_sample_payload(index, stamp))
            assert response.status_code == 200, response.text

        state = client.get("/api/dashboard-state?profile_id=person-a").json()
        assert state["profile_state"]["monitoring"]["session_id"] == initial_context
        assert state["profile_state"]["monitoring"]["sample_count"] == 101
        assert len(main._ml_samples) == 101
        assert main._ml_context == {"profile_id": "person-a", "session_id": initial_context}
        profile_samples = state["profile_state"]["profile_samples"]
        assert [sample["values"] for sample in list(main._ml_samples)[-len(profile_samples):]] == [
            sample["values"] for sample in profile_samples
        ]
        assert state["ml_prediction"]["sampling_information"]["sample_rate_hz"] == pytest.approx(25)
        assert state["combined_interpretation"]["state"] in {
            "agreement", "disagreement", "baseline-only", "ML-only", "insufficient-data",
        }
        assert store.monitoring["baseline"] == baseline

        # Invalid units are not passed to either consumer.
        invalid_units = {**UNITS, "gx": "rad/s"}
        rejected = client.post(
            "/api/sensor/ingest",
            json=_api_sample_payload(101, base + timedelta(seconds=101 / 25), units=invalid_units),
        )
        assert rejected.status_code == 200
        assert len(main._ml_samples) == 101
        assert store.monitoring_status()["sample_count"] == 101

        # Sequence duplication is rejected at ingress and does not affect either stream.
        duplicate = client.post(
            "/api/sensor/ingest",
            json=_api_sample_payload(100, base + timedelta(seconds=102 / 25)),
        )
        assert duplicate.status_code == 400
        assert len(main._ml_samples) == 101

        report_response = client.post("/api/monitoring/end")
        assert report_response.status_code == 200, report_response.text
        report = report_response.json()
        assert report["baseline"]["calibration_id"] == baseline["calibration_id"]
        stored_record = store.profile("person-a")["sessions"][-1]
        assert stored_record["baseline"] == baseline
        saved_session = store.completed_session(report["session_id"], "person-a")
        assert saved_session["baseline"]["calibration_id"] == baseline["calibration_id"]
        assert report["session"]["combined_interpretation"] == report["combined_interpretation"]
        assert report["combined_interpretation"]["state"] in {
            "agreement", "disagreement", "baseline-only", "ML-only", "insufficient-data",
        }
        assert report["model_analysis"]["sampling_information"]["sample_rate_hz"] == pytest.approx(25)
        assert report["session"]["sample_count"] == len(report["samples"]) == 101
        assert main._ml_context is None
        assert len(main._ml_samples) == 0


def test_profile_and_session_switches_reset_ml_buffer(isolated_integration, monkeypatch):
    store = isolated_integration
    _calibrate(store, "person-a", monkeypatch)
    _calibrate(store, "person-b", monkeypatch)
    with TestClient(main.app) as client:
        for profile_index, profile_id in enumerate(("person-a", "person-b")):
            assert client.post(f"/api/profiles/{profile_id}/monitoring").status_code == 200
            session_id = store.monitoring_status()["session_id"]
            assert main._ml_context is None
            assert len(main._ml_samples) == 0
            stamp = datetime.now(timezone.utc)
            response = client.post(
                "/api/sensor/ingest",
                json=_api_sample_payload(profile_index + 10, stamp, sensor="sim-imu"),
            )
            assert response.status_code == 200
            assert main._ml_context == {"profile_id": profile_id, "session_id": session_id}
            assert len(main._ml_samples) == 1
            ended = client.post("/api/monitoring/end")
            assert ended.status_code == 200
            assert main._ml_context is None
            assert len(main._ml_samples) == 0


def test_irregular_timing_and_explicit_combined_states():
    assert combine_movement_outputs({"state": "normal"}, {"state": "available", "output": {"class": "Healthy"}})["state"] == "agreement"
    assert combine_movement_outputs({"state": "marked deviation"}, {"state": "available", "output": {"class": "Healthy"}})["state"] == "disagreement"
    assert combine_movement_outputs({"state": "normal"}, {"state": "incompatible_sampling_rate", "output": None})["state"] == "baseline-only"
    assert combine_movement_outputs(None, {"state": "available", "output": {"class": "Parkinsons"}})["state"] == "ML-only"
    assert combine_movement_outputs(None, {"state": "insufficient_data", "output": None})["state"] == "insufficient-data"

    base = datetime(2026, 10, 30, tzinfo=timezone.utc)
    intervals = [0.04 if index % 2 else 0.1 for index in range(100)]
    elapsed = 0.0
    samples = []
    for index, interval in enumerate([0.0, *intervals]):
        elapsed += interval
        samples.append({
            "timestamp": (base + timedelta(seconds=elapsed)).isoformat(),
            "values": {axis: 1.0 for axis in AXES},
            "units": UNITS,
        })
    result = main.ml_service.analyze(samples)
    assert result["state"] == "irregular_sampling"
    assert result["output"] is None


def test_stale_and_wrong_source_samples_do_not_enter_monitoring(isolated_integration, monkeypatch):
    store = isolated_integration
    _calibrate(store, "person-a", monkeypatch)
    with TestClient(main.app) as client:
        assert client.post("/api/profiles/person-a/monitoring").status_code == 200
        now = datetime.now(timezone.utc)
        wrong_units = _api_sample_payload(1, now, units={**UNITS, "ax": "g"})
        assert client.post("/api/sensor/ingest", json=wrong_units).status_code == 200
        assert store.monitoring_status()["sample_count"] == 0
        assert len(main._ml_samples) == 0
        stale = _api_sample_payload(2, now - timedelta(seconds=60))
        assert client.post("/api/sensor/ingest", json=stale).status_code == 400
        assert store.monitoring_status()["sample_count"] == 0
        assert len(main._ml_samples) == 0
