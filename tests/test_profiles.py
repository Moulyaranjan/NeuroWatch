import csv
import io
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from movement import AXES, ProfileStore


UNITS = {
    "ax": "m/s^2",
    "ay": "m/s^2",
    "az": "m/s^2",
    "gx": "deg/s",
    "gy": "deg/s",
    "gz": "deg/s",
}


@pytest.fixture(autouse=True)
def isolate_stores(tmp_path, monkeypatch):
    profile_store = ProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(main, "profile_store", profile_store)
    monkeypatch.setattr(main, "_sensor_diagnostics", {
        "invalid_packet_count": 0,
        "last_invalid_at": None,
        "last_invalid_reason": None,
    })
    main.store.events.clear()
    main.store.current_session_id = None
    main.store.sequence_numbers.clear()
    yield profile_store
    main.store.events.clear()
    main.store.current_session_id = None
    main.store.sequence_numbers.clear()


def sample(index, timestamp=None, acceleration=0.1, sensor="test-imu"):
    timestamp = timestamp or datetime(2026, 10, 29, tzinfo=timezone.utc) + timedelta(seconds=index / 5)
    return {
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "sensor": sensor,
        "source": "real",
        "sequence_number": index,
        "sensor_freshness_ms": 25,
        "units": UNITS,
        "values": {
            "ax": acceleration + (index % 5) * 0.001,
            "ay": 0.05 + (index % 3) * 0.001,
            "az": 9.7 + (index % 7) * 0.001,
            "gx": 1.0 + (index % 5) * 0.01,
            "gy": 0.5 + (index % 3) * 0.01,
            "gz": -0.2 + (index % 4) * 0.01,
        },
    }


def complete_calibration(profile_store, monkeypatch, profile_id="person-one"):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    profile_store.create_profile("Person One", profile_id)
    profile_store.start_calibration(profile_id, 120)
    start = datetime(2026, 10, 29, tzinfo=timezone.utc)
    for index in range(601):
        record = sample(index, start + timedelta(seconds=index / 5))
        profile_store.record_packet(record, start + timedelta(seconds=index / 5), clock[0], True)
    clock[0] += 120
    result = profile_store.calibration_status()
    assert result["state"] == "completed"
    return result


def test_profile_creation_validation_persistence_and_isolation(isolate_stores, tmp_path):
    store = isolate_stores
    first = store.create_profile("Moulya", "person_one")
    second = store.create_profile("Guest")
    assert first["profile_id"] == "person_one"
    assert second["profile_id"] == "guest"

    with pytest.raises(ValueError, match="Profile ID"):
        store.create_profile("Invalid", "../outside")

    loaded = ProfileStore(tmp_path / "profiles.json")
    assert {profile["profile_id"] for profile in loaded.list_profiles()} == {"person_one", "guest"}
    assert loaded.profile("person_one")["baseline"] is None
    assert loaded.profile("guest")["baseline"] is None


def test_calibration_baseline_statistics_use_only_collected_samples(isolate_stores, monkeypatch):
    store = isolate_stores
    state = complete_calibration(store, monkeypatch)
    baseline = store.profile("person-one")["baseline"]
    expected = [sample(index)["values"]["ax"] for index in range(601)]
    expected_mean = sum(expected) / len(expected)
    assert state["accepted_sample_count"] == 601
    assert baseline["sample_count"] == 601
    assert baseline["quality"]["reliable"] is True
    assert baseline["axes"]["ax"]["mean"] == pytest.approx(expected_mean)
    assert baseline["axes"]["ax"]["minimum"] == pytest.approx(min(expected))
    assert baseline["axes"]["ax"]["maximum"] == pytest.approx(max(expected))
    assert baseline["axes"]["ax"]["standard_deviation"] > 0
    assert baseline["features"]["acceleration_magnitude"]["unit"] == "m/s^2"
    assert baseline["features"]["gyroscope_magnitude"]["unit"] == "deg/s"
    assert baseline["features"]["dynamic_acceleration_magnitude"]["rms"] >= 0


def test_calibration_cannot_complete_with_absent_or_inadequate_samples(isolate_stores, monkeypatch):
    store = isolate_stores
    clock = [20.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    store.create_profile("Person", "person")
    store.start_calibration("person", 120)
    clock[0] += 120
    result = store.calibration_status()
    assert result["state"] == "insufficient_data"
    assert store.profile("person")["baseline"] is None
    assert result["result"]["quality"]["reliable"] is False


def test_invalid_or_wrong_unit_packets_are_not_calibration_samples(isolate_stores, monkeypatch):
    clock = [5.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    store = isolate_stores
    store.create_profile("Person", "person")
    store.start_calibration("person", 120)
    invalid = sample(1)
    invalid["units"] = {}
    store.record_packet(invalid, datetime.now(timezone.utc), clock[0], False, "Units do not match.")
    state = store.calibration_status()
    assert state["accepted_sample_count"] == 0
    assert state["rejected_packet_count"] == 1
    assert state["last_invalid_reason"] == "Units do not match."


def test_cancelled_calibration_is_never_saved_as_a_baseline(isolate_stores, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    store = isolate_stores
    store.create_profile("Person", "person")
    store.start_calibration("person", 120)
    record = sample(1)
    store.record_packet(record, datetime.now(timezone.utc), clock[0], True)
    result = store.cancel_calibration()
    assert result["state"] == "cancelled"
    assert store.profile("person")["baseline"] is None
    assert store.profile("person")["calibrations"][0]["state"] == "cancelled"


def test_calibration_rejects_packets_from_a_different_sensor(isolate_stores, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    store = isolate_stores
    store.create_profile("Person", "person")
    store.start_calibration("person", 120)
    received_at = datetime.now(timezone.utc)
    store.record_packet(sample(1), received_at, clock[0], True)
    store.record_packet(sample(2, sensor="unrelated-imu"), received_at, clock[0], True)
    state = store.calibration_status()
    assert state["accepted_sample_count"] == 1
    assert state["rejected_packet_count"] == 1
    assert "different sensor" in state["last_invalid_reason"]


def test_fixed_baseline_monitoring_and_sustained_event_persistence(isolate_stores, monkeypatch):
    store = isolate_stores
    completed = complete_calibration(store, monkeypatch)
    original_baseline = store.profile("person-one")["baseline"]
    session = store.start_monitoring("person-one")
    assert session["state"] == "waiting_for_sensor"
    start = datetime(2026, 10, 29, 0, 3, tzinfo=timezone.utc)
    clock = [time.monotonic()]
    for index in range(7):
        record = sample(1000 + index, start + timedelta(seconds=index / 5), acceleration=3.0)
        store.record_packet(record, start + timedelta(seconds=index / 5), clock[0] + index / 5, True)
    state = store.dashboard_state("person-one")
    assert store.profile("person-one")["baseline"] == original_baseline
    assert state["comparison"]["state"] in {"review", "marked deviation"}
    assert len(state["comparison"]["feature_deviations_z"]) == 2
    assert len(state["monitoring"]["events"]) == 1
    assert state["monitoring"]["events"][0]["description"].startswith("Experimental engineering score")
    ended = store.end_monitoring()
    assert ended["sample_count"] == 7
    assert ended["baseline_calibration_id"] == original_baseline["calibration_id"]
    assert completed["state"] == "completed"


def test_profile_export_isolated_and_includes_calibration_samples(isolate_stores, monkeypatch):
    store = isolate_stores
    complete_calibration(store, monkeypatch, "private-person")
    store.create_profile("Other", "other-person")
    contents = store.export_csv("private-person")
    rows = list(csv.DictReader(io.StringIO(contents)))
    assert rows
    assert {row["profile_id"] for row in rows} == {"private-person"}
    assert {row["record_type"] for row in rows} == {"calibration_sample", "calibration_summary"}
    with pytest.raises(KeyError):
        store.export_csv("private-person", "not-a-session")


def test_profile_api_creation_validation_and_dashboard_compatibility(isolate_stores):
    with TestClient(main.app) as client:
        created = client.post("/api/profiles", json={"display_name": "A Person", "profile_id": "person-a"})
        assert created.status_code == 201
        assert created.json()["profile_id"] == "person-a"
        invalid = client.post("/api/profiles", json={"display_name": "Invalid", "profile_id": "../bad"})
        assert invalid.status_code == 400
        dashboard = client.get("/api/dashboard-state?profile_id=person-a").json()
        assert dashboard["profile_state"]["selected_profile"]["profile_id"] == "person-a"
        assert dashboard["profile_state"]["baseline"] is None
        assert dashboard["baseline"] == {axis: None for axis in AXES}
        assert dashboard["movement_intelligence"]["state"] == "uncalibrated"
        assert dashboard["sensor_temperature"]["state"] == "absent"
        assert dashboard["history"] == []
        assert client.get("/api/dashboard-state?profile_id=missing").status_code == 404


def test_api_rejects_nonfinite_sensor_data_and_calibration_without_units(isolate_stores):
    with TestClient(main.app, raise_server_exceptions=False) as client:
        client.post("/api/profiles", json={"display_name": "Person", "profile_id": "p1"})
        started = client.post("/api/profiles/p1/calibrations", json={"duration_seconds": 120})
        assert started.status_code == 200
        invalid = client.post(
            "/api/sensor/ingest",
            content='{"sensor":"imu","source":"real","values":{"ax":NaN,"ay":0,"az":9.8,"gx":0,"gy":0,"gz":0}}',
            headers={"content-type": "application/json"},
        )
        assert invalid.status_code == 422
        packet = sample(1)
        packet["units"] = {}
        packet["timestamp"] = datetime.now(timezone.utc).isoformat()
        response = client.post("/api/sensor/ingest", json=packet)
        assert response.status_code == 200
        assert client.get("/api/calibration-state").json()["accepted_sample_count"] == 0
        assert client.get("/api/calibration-state").json()["rejected_packet_count"] == 1


def test_optional_temperature_is_only_reported_when_present(isolate_stores):
    with TestClient(main.app) as client:
        absent = client.get("/api/dashboard-state").json()["sensor_temperature"]
        assert absent["state"] == "absent"
        payload = sample(4)
        payload["temperature_c"] = 42.5
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        response = client.post("/api/sensor/ingest", json=payload)
        assert response.status_code == 200
        temperature = client.get("/api/dashboard-state").json()["sensor_temperature"]
        assert temperature["state"] == "available"
        assert temperature["temperature_c"] == 42.5
        assert "not body temperature" in temperature["label"]
        implausible = sample(5)
        implausible["temperature_c"] = 200
        implausible["timestamp"] = datetime.now(timezone.utc).isoformat()
        assert client.post("/api/sensor/ingest", json=implausible).status_code == 200
        temperature = client.get("/api/dashboard-state").json()["sensor_temperature"]
        assert temperature["state"] == "implausible"
        assert temperature["temperature_c"] is None


def test_export_endpoint_filters_profile_and_history(isolate_stores, monkeypatch):
    store = isolate_stores
    complete_calibration(store, monkeypatch, "owner")
    store.create_profile("Other", "other")
    with TestClient(main.app) as client:
        history = client.get("/api/profiles/owner/history")
        assert history.status_code == 200
        assert len(history.json()["calibrations"]) == 1
        response = client.get("/api/session/export?profile_id=owner")
        assert response.status_code == 200
        assert "owner" in response.text
        assert "other" not in response.text
        assert "calibration_summary" in response.text
        assert "calibration_sample" in response.text
        missing = client.get("/api/session/export?profile_id=owner&session_id=other-session")
        assert missing.status_code == 404


def test_profile_bound_packets_are_isolated_from_legacy_history_and_export(
    isolate_stores,
    monkeypatch,
):
    profile_store = isolate_stores
    complete_calibration(profile_store, monkeypatch, "owner")
    profile_store.create_profile("Other", "other")
    profile_store.start_monitoring("owner")
    packet = sample(700, datetime.now(timezone.utc))

    with TestClient(main.app) as client:
        response = client.post("/api/sensor/ingest", json=packet)
        assert response.status_code == 200

        owner_dashboard = client.get("/api/dashboard-state?profile_id=owner").json()
        other_dashboard = client.get("/api/dashboard-state?profile_id=other").json()
        assert len(owner_dashboard["history"]) == 1
        assert owner_dashboard["history"][0]["profile_id"] == "owner"
        assert other_dashboard["history"] == []

        assert client.get("/api/session/history").json()["sessions"] == []
        legacy_export = client.get("/api/session/export")
        assert legacy_export.status_code == 200
        assert "9.7" not in legacy_export.text

        assert client.post("/api/monitoring/end").status_code == 200
        owner_export = client.get("/api/session/export?profile_id=owner")
        assert owner_export.status_code == 200
        assert "monitoring_sample" in owner_export.text
        assert "owner" in owner_export.text
        assert "other" not in owner_export.text


def _record_completed_samples(profile_store, count=12):
    profile_store.start_monitoring("report-person")
    start = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    for index in range(count):
        timestamp = start + timedelta(milliseconds=index * 100)
        profile_store.record_packet(
            sample(index, timestamp, acceleration=0.2),
            timestamp,
            time.monotonic(),
            True,
        )


def test_ending_monitoring_creates_one_persisted_baseline_comparison(isolate_stores, monkeypatch, tmp_path):
    store = isolate_stores
    complete_calibration(store, monkeypatch, "report-person")
    original_baseline = store.profile("report-person")["baseline"]
    _record_completed_samples(store)

    report = store.end_monitoring()
    session_id = report["session"]["session_id"]
    assert report["session_id"] == session_id
    assert report["profile"]["profile_id"] == "report-person"
    assert report["baseline"]["calibration_id"] == original_baseline["calibration_id"]
    assert report["packet_counts"] == {"received": 12, "accepted": 12, "rejected": 0}
    assert report["statistics"]["axes"]["ax"]["sample_count"] == 12
    assert report["effective_sample_rate_hz"] == pytest.approx(10.0)
    assert report["samples"][0]["timestamp"] < report["samples"][-1]["timestamp"]
    assert report["comparisons"]["axes"]["ax"]["unit"] == "m/s^2"
    assert report["units_verified"] is True
    assert report["comparisons"]["axes"]["ax"]["variability_difference"] is not None
    assert report["comparisons"]["axes"]["ax"]["variability_percentage_difference"] is not None
    assert report["deviation_summary"]["score"] is None
    assert report["deviation_summary"]["severity_thresholds_used"] is False
    assert store.profile("report-person")["baseline"] == original_baseline
    trend = store.dashboard_state("report-person")["trends"][0]
    assert trend["features"]["acceleration_magnitude"] == pytest.approx(
        report["statistics"]["acceleration_magnitude"]["mean"]
    )
    assert trend["features"]["gyroscope_magnitude"] == pytest.approx(
        report["statistics"]["gyroscope_magnitude"]["mean"]
    )

    loaded = ProfileStore(tmp_path / "profiles.json")
    restored = loaded.completed_session(session_id, "report-person")
    assert restored["baseline"]["calibration_id"] == original_baseline["calibration_id"]
    assert len(restored["samples"]) == 12
    assert loaded.completed_session_history("report-person")[0]["session_id"] == session_id


def test_session_report_pdf_routes_are_scoped_and_valid(isolate_stores, monkeypatch):
    profile_store = isolate_stores
    complete_calibration(profile_store, monkeypatch, "report-person")
    _record_completed_samples(profile_store, 15)

    with TestClient(main.app) as client:
        ended = client.post("/api/session/end")
        assert ended.status_code == 200
        report = ended.json()["report"]
        session_id = report["session"]["session_id"]
        assert report["profile"]["profile_id"] == "report-person"
        report_page = client.get(
            f"/session-report/{session_id}?profile_id=report-person"
        )
        assert report_page.status_code == 200
        assert 'id="post-session-analysis"' in report_page.text
        assert client.get(
            f"/session-report/{session_id}?profile_id=report-person"
        ).status_code == 200
        invalid_page = client.get("/session-report/monitor-invalid", follow_redirects=False)
        assert invalid_page.status_code == 303
        assert invalid_page.headers["location"] == "/"

        analysis = client.get(
            f"/api/session/{session_id}/analysis?profile_id=report-person"
        )
        assert analysis.status_code == 200
        assert analysis.json()["baseline"]["calibration_id"] == report["baseline"]["calibration_id"]
        assert len(analysis.json()["samples"]) == 15

        pdf = client.get(f"/api/session/{session_id}/report.pdf?profile_id=report-person")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert f"neurowatch-{session_id}.pdf" in pdf.headers["content-disposition"]
        assert pdf.content.startswith(b"%PDF-")

        assert client.get(f"/api/session/{session_id}/analysis?profile_id=wrong-profile").status_code == 404
        assert client.get("/api/session/monitor-invalid/report.pdf").status_code == 404
        assert client.post("/api/session/end").status_code == 409

        profile_store.start_monitoring("report-person")
        _record_second_session_samples(profile_store)
        newer = client.post("/api/session/end").json()["report"]
        assert newer["session"]["session_id"] != session_id
        older_pdf = client.get(f"/api/session/{session_id}/report.pdf?profile_id=report-person")
        assert older_pdf.status_code == 200
        assert older_pdf.content.startswith(b"%PDF-")
        older_page = client.get(
            f"/session-report/{session_id}?profile_id=report-person"
        )
        assert older_page.status_code == 200

        history = client.get("/api/session/history?profile_id=report-person").json()
        assert history["completed_sessions"][0]["session_id"] == newer["session"]["session_id"]
        assert len(history["completed_sessions"]) == 2
        assert len({entry["session_id"] for entry in history["completed_sessions"]}) == 2


def _record_second_session_samples(profile_store):
    start = datetime(2026, 10, 29, 15, 2, tzinfo=timezone.utc)
    for index in range(10):
        timestamp = start + timedelta(milliseconds=index * 100)
        profile_store.record_packet(
            sample(100 + index, timestamp, acceleration=0.3),
            timestamp,
            time.monotonic(),
            True,
        )


def test_report_uses_original_baseline_after_profile_recalibration(isolate_stores, monkeypatch, tmp_path):
    profile_store = isolate_stores
    complete_calibration(profile_store, monkeypatch, "report-person")
    _record_completed_samples(profile_store, 8)
    report = profile_store.end_monitoring()
    session_id = report["session_id"]
    calibration_id = report["baseline"]["calibration_id"]

    profile_store.profiles["report-person"]["sessions"][0]["analysis"].pop("units_verified")
    profile_store.profiles["report-person"]["baseline"]["calibration_id"] = "later-calibration"
    profile_store._save()
    restarted = ProfileStore(tmp_path / "profiles.json")
    old_report = restarted.completed_session(session_id)
    assert old_report["baseline"]["calibration_id"] == calibration_id
    assert old_report["units_verified"] is True


def test_empty_completed_session_reports_missing_measurements(isolate_stores, monkeypatch):
    store = isolate_stores
    complete_calibration(store, monkeypatch, "report-person")
    store.start_monitoring("report-person")
    report = store.end_monitoring()
    assert report["statistics"]["axes"] == {}
    assert report["effective_sample_rate_hz"] is None
    assert report["limitations"]
    assert report["packet_counts"]["accepted"] == 0
    assert main.build_session_pdf(report).startswith(b"%PDF-")


def test_monitoring_without_a_saved_baseline_is_blocked(isolate_stores):
    isolate_stores.create_profile("Uncalibrated", "uncalibrated")
    with pytest.raises(ValueError, match="no saved baseline"):
        isolate_stores.start_monitoring("uncalibrated")


def test_report_samples_are_ordered_by_sensor_timestamp(isolate_stores, monkeypatch):
    store = isolate_stores
    complete_calibration(store, monkeypatch, "report-person")
    store.start_monitoring("report-person")
    start = datetime(2026, 10, 29, 15, 5, tzinfo=timezone.utc)
    for offset in (2, 0, 1):
        timestamp = start + timedelta(seconds=offset)
        store.record_packet(
            sample(offset, timestamp),
            timestamp,
            time.monotonic(),
            True,
        )
    report = store.end_monitoring()
    timestamps = [entry["sensor_timestamp"] for entry in report["samples"]]
    assert timestamps == sorted(timestamps)


def test_dashboard_has_pdf_workflow_and_no_csv_export_control(isolate_stores):
    from pathlib import Path

    with TestClient(main.app) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "Download Session Report (PDF)" in page.text
    assert "Export CSV" not in page.text
    assert 'id="completed-session-select"' in page.text
    assert "#post-session-analysis { display:none; }" in page.text
    assert ".report-mode #post-session-analysis { display:block; }" in page.text
    assert 'id="session-deviation-summary"' in page.text
    assert 'id="session-events"' in page.text
    assert 'id="return-to-dashboard"' in page.text

    profile_script = (
        Path(main.BASE_DIR) / "static" / "js" / "profile-dashboard.js"
    ).read_text(encoding="utf-8")
    assert 'window.localStorage.setItem(\'neurowatch.selectedProfile\'' in profile_script
    assert '">View Report</a>' in profile_script
    assert "requestAnimationFrame(() => requestAnimationFrame(resolve))" in profile_script


def test_dashboard_polling_keeps_saved_baseline_and_profile_scope(
    isolate_stores,
    monkeypatch,
):
    profile_store = isolate_stores
    complete_calibration(profile_store, monkeypatch, "calibrated-person")
    profile_store.create_profile("Uncalibrated Person", "uncalibrated-person")

    with TestClient(main.app) as client:
        for _ in range(3):
            calibrated = client.get(
                "/api/dashboard-state?profile_id=calibrated-person"
            ).json()["profile_state"]
            assert calibrated["selected_profile"]["profile_id"] == "calibrated-person"
            assert calibrated["baseline"]["profile_id"] == "calibrated-person"
            assert calibrated["baseline"]["quality"]["reliable"] is True
            assert calibrated["comparison"] is None

            uncalibrated = client.get(
                "/api/dashboard-state?profile_id=uncalibrated-person"
            ).json()["profile_state"]
            assert uncalibrated["selected_profile"]["profile_id"] == "uncalibrated-person"
            assert uncalibrated["baseline"] is None


def test_movement_analysis_has_one_profile_state_renderer():
    template = (Path(main.BASE_DIR) / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    profile_script = (
        Path(main.BASE_DIR) / "static" / "js" / "profile-dashboard.js"
    ).read_text(encoding="utf-8")
    inline_dashboard_script = template.split('<script>', 1)[1].split(
        "</script>", 1
    )[0]

    assert "$('mstate').textContent" not in inline_dashboard_script
    assert "$('msummary').textContent" not in inline_dashboard_script
    assert "renderMonitoring(profileState, profileState.comparison)" in profile_script
    assert "setTextIfChanged('mstate', profileState.baseline ? 'insufficient_data' : 'uncalibrated');" in profile_script
    assert "if (requestedProfile !== selectedProfile) return;" in profile_script
    assert "if (requestedProfile && returnedProfile !== requestedProfile) return;" in profile_script
