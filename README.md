# NeuroWatch

**A Drift-Aware, Explainable Personalized Movement Monitoring System**

NeuroWatch is a research prototype designed to explore personalized movement monitoring using wearable data and external inertial measurement unit (IMU) sensors.

## Goals

- Establish an individual's baseline movement patterns.
- Track gradual drift and sudden deviations from that baseline.
- Explore interpretable movement features, such as movement smoothness and asymmetry.
- Visualize longitudinal trends over time.

## Project Status

Early development. Data ingestion and dashboard architecture are being prepared.

## Technology

- Python
- Wearable data processing
- ESP32 and MPU6050 IMU sensors (planned)
- Data visualization

## Data Privacy

Personal wearable exports, raw sensor recordings, and processed personal datasets must remain local and must not be committed to this repository.

## Disclaimer

NeuroWatch is an experimental research prototype, not a medical diagnostic system.

## Web application

This workspace now includes a FastAPI-based dashboard and backend in addition to the original data-conversion scripts. The dashboard is served directly from the repository root and does not require Streamlit.

The dashboard separates the plain-language My Movement experience from the technical Research Lab. Activity timelines, trends, device states, and insights are rendered only from values available through the existing API; unsupported sensor status and unavailable measurements remain explicit. The interface does not provide medical interpretation.

### Run locally

1. Open a terminal in the project root.
2. Start the app with `.\run_neurowatch.bat`. It uses the tested `.venv311` interpreter and checks that `asyncio` starts before launching Uvicorn.
3. If `.venv311` is missing, create it with `py -3.11 -m venv .venv311`, then install dependencies into that environment with `.\.venv311\Scripts\python.exe -m pip install fastapi uvicorn httpx pytest websockets`.
4. Open the dashboard:
   - `http://127.0.0.1:8000/`

The workspace's Python 3.14.6 installation currently terminates with Windows access violation `0xC0000005` when running `asyncio.run()`, including in an isolated standard-library-only test. This happens before Uvicorn or the application starts; use the verified Python 3.11 environment until that interpreter installation is repaired.

### API endpoints

- `GET /api/health` – returns the current monitoring status.
- `GET /api/dashboard-state` – returns live dashboard state with a genuine idle default.
- `GET /api/session/history` – returns session records.
- `GET /api/session/export` – downloads the current session history as CSV.
- `POST /api/sensor/ingest` – accepts ESP32 and sensor payloads.
- `WS /ws/dashboard` – real-time backend-to-dashboard updates.

### Sensor data handling

- Real sensor data is accepted with source values such as `real` or `uncalibrated`.
- The simulator is disabled by default.
- Idle state is the default: the dashboard shows “Waiting for data” until valid sensor events arrive.
- Data quality checks reject empty payloads and invalid numeric readings.

## Stage 2: ESP32 + MPU6050 hardware integration

The current FastAPI backend remains the system of record for dashboard state, validation, and live updates. Stage 2 adds a real ESP32 firmware path that posts IMU readings to the existing `/api/sensor/ingest` endpoint and keeps the dashboard honest about when data is present.

### Wiring

Default I2C wiring for the MPU6050:

- SDA: GPIO 21
- SCL: GPIO 22
- VCC: 3.3V or the board’s supported IMU rail
- GND: GND

### Firmware files

The Stage 2 sketch is in the repo under:

- `firmware/esp32_neurowatch/esp32_neurowatch.ino`
- `firmware/esp32_neurowatch/arduino_secrets.h.example`

Create a local `arduino_secrets.h` file in the same directory with your Wi‑Fi credentials and the backend URL. This file is ignored by Git so private credentials are not committed.

```cpp
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* BACKEND_URL = "http://127.0.0.1:8000/api/sensor/ingest";
```

### Real-sensor payload contract

The backend validates complete IMU values, ISO-8601 timestamps, freshness limits, and monotonic sequence numbers. A typical payload is:

```json
{
   "sensor": "mpu6050",
   "source": "real",
   "values": {
      "ax": 0.12,
      "ay": -0.03,
      "az": 0.98,
      "gx": 10.2,
      "gy": 1.5,
      "gz": -2.1
   },
   "timestamp": "2026-10-11T10:33:59Z",
   "sequence_number": 42,
   "sensor_freshness_ms": 25.0,
   "units": {
      "ax": "m/s^2",
      "ay": "m/s^2",
      "az": "m/s^2",
      "gx": "deg/s",
      "gy": "deg/s",
      "gz": "deg/s"
   }
}
```

The simulator remains disabled by default, so no fabricated readings are shown unless the system is explicitly configured to use real hardware and valid measurements.

### Optional hardware support

The sketch contains placeholders for a MAX30102 pulse sensor and two FSR contact sensors. They are disabled unless the hardware is physically connected and deliberately enabled in code. No heart-rate or force values are fabricated in the default state.

### Hardware validation flow

1. Start the backend with the verified venv command.
2. Upload the ESP32 firmware with your actual credentials.
3. Open the Serial Monitor and confirm Wi‑Fi connection and MPU6050 initialization.
4. Observe the dashboard at `http://127.0.0.1:8000/` and confirm the status changes from “Waiting for data” to live sensor values only when the hardware is connected and reporting valid measurements.

This repository contains the Stage 2 firmware and backend integration logic, but the real hardware must be physically present for live validation.

## Team Setup

1. Clone this repository.
2. Create a local Python virtual environment.
3. Install the dependencies documented by the project.
4. Keep personal data and local configuration files out of Git.