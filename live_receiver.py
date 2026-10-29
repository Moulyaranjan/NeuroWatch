
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from flask import Flask, request, jsonify

app = Flask(__name__)

DATA_DIR = Path(__file__).parent / "data" / "live"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATA_FILE = DATA_DIR / "imu_readings.jsonl"
file_lock = Lock()


@app.get("/health")
def health():
    return jsonify({"status": "receiver_running"})


@app.post("/api/sensor/ingest")
def ingest():
    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({"error": "Expected JSON"}), 400

    values = payload.get("values")
    required = ("ax", "ay", "az", "gx", "gy", "gz")

    if not isinstance(values, dict):
        return jsonify({"error": "Missing values"}), 400

    try:
        clean_values = {
            key: float(values[key]) for key in required
        }
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Invalid six-axis values"}), 400

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sensor": "mpu6050",
        "source": "real",
        "values": clean_values,
        "units": {
            "acceleration": "m/s^2",
            "gyroscope": "deg/s"
        }
    }

    import json

    with file_lock:
        with DATA_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    return jsonify({"accepted": True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
