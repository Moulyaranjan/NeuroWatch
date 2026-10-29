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
3. If `.venv311` is missing, create it with `py -3.11 -m venv .venv311`, then install dependencies into that environment with `.\.venv311\Scripts\python.exe -m pip install -r requirements.txt`.
4. Open the dashboard:
   - `http://127.0.0.1:8000/`

The workspace's Python 3.14.6 installation currently terminates with Windows access violation `0xC0000005` when running `asyncio.run()`, including in an isolated standard-library-only test. This happens before Uvicorn or the application starts; use the verified Python 3.11 environment until that interpreter installation is repaired.

### API endpoints

- `GET /api/health` – returns the current monitoring status.
- `GET /api/dashboard-state` – returns live dashboard state with a genuine idle default.
- `GET /api/session/history` – returns unassigned legacy records and completed profile-session history.
- `POST /api/session/end` – finalizes active profile monitoring and returns its saved baseline comparison report.
- `GET /api/session/{session_id}/analysis?profile_id={profile_id}` – retrieves saved report analysis and timestamped samples for that completed session.
- `GET /api/session/{session_id}/report.pdf?profile_id={profile_id}` – downloads a PDF generated from that exact saved session and baseline.
- `GET /api/session/export` – retained as a legacy CSV API; the dashboard no longer offers CSV export.
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

The current sketch is specifically written for an MPU6050 and checks its chip ID during initialization. The dashboard displays the sensor name sent in the payload; it does not independently verify physical sensor identity. Confirm the installed hardware before relying on the sketch or its wiring.

Default I2C wiring if the installed component is an MPU6050:

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

## Profile calibration and fixed-baseline monitoring

The FastAPI dashboard supports local person profiles, per-profile calibration, and monitoring against a saved, fixed baseline. Choose a profile explicitly before starting calibration or monitoring; after a page reload, an already-active in-process session is reselected only to continue managing that same bound session. A baseline is never shared between profiles or updated during monitoring.

### Dashboard workflow

1. Run NeuroWatch and open `http://127.0.0.1:8000/`.
2. Create a profile or select an existing one. Verify the selected display name and profile ID.
3. Choose 120, 150, or 180 seconds (120 seconds is the default), then press **Start Calibration**. Keep the sensor connected and use the same device placement intended for monitoring.
4. The dashboard reports backend-measured elapsed and remaining time, accepted and rejected packets, signal state, and completion quality. A calibration pauses visibly when no fresh data arrives. Cancelled or inadequate calibrations never replace a saved baseline.
5. After a reliable baseline is saved, press **Start Monitoring**. The baseline stays fixed for that monitoring session. Press **End Session** to persist its samples and summary; use **Recalibrate** only when a new baseline is explicitly desired.
6. On **End Session**, NeuroWatch saves the completed sensor readings, quality and event metadata, the exact baseline snapshot used by monitoring, and a per-axis/magnitude comparison. After the save succeeds, the dashboard navigates to the dedicated `/session-report/{session_id}` page and loads that report without a refresh.
7. The report page shows actual session measurements, baseline mean ± SD references, mean and variability differences, and limitations; it does not assign a clinical severity or diagnosis. Use its session list to reopen older reports, or return to the dashboard. The dashboard also restores completed-session comparisons after a browser refresh.
8. Use **Download Session Report (PDF)** to generate a report for the selected session; a later calibration cannot change the baseline reference in an older session report.
9. Repeated end requests do not create duplicate completed records. A second request with no active session returns HTTP 409.

Calibration uses only `source: "real"` packets with all six finite IMU axes and explicit units matching the firmware. Current firmware acceleration is m/s². Adafruit's `sensors_event_t.gyro` is in rad/s; the sketch explicitly converts those values to deg/s before transmitting and declares `deg/s` in the payload. This conversion is visible in `firmware/esp32_neurowatch/esp32_neurowatch.ino`; see the upstream [Adafruit MPU6050 implementation](https://github.com/adafruit/Adafruit_MPU6050/blob/master/Adafruit_MPU6050.cpp) (`fillGyroEvent`) for the native rad/s conversion. Packets with missing or mismatched units may appear in unassigned raw history but are excluded from calibration and monitoring; profile-bound samples are shown only with their owning profile.

The backend accepts calibration durations of exactly 120, 150, or 180 seconds. A reliable baseline requires at least 30 accepted samples, at least 70% time coverage, no timestamp gap greater than five seconds, and at least ten usable timestamp-aware two-second windows. Data missing for more than three seconds is shown as stale/paused; calibration still has a server-enforced maximum wall-clock duration. If the quality gate is not met, the attempt is recorded as insufficient and no new baseline is claimed.

Baselines contain per-axis mean, population standard deviation, minimum, maximum, and sample count. Derived acceleration and gyroscope magnitude statistics include mean, standard deviation, RMS, and range. Acceleration includes gravity; the reported dynamic acceleration RMS subtracts the calibration mean vector and is only an approximate gravity/orientation separation. Dominant oscillation frequency is an estimate based on zero crossings and is omitted unless sampling is sufficiently regular and long enough.

The movement comparison uses two-second timestamp-aware windows and z-scores against the selected profile's calibration-window feature statistics, with a numerical floor when baseline variance is near zero. The experimental engineering score averages the absolute acceleration and gyroscope z-scores. Provisional review and marked-deviation thresholds are 2.5 and 4.0, respectively, and an event requires the same severity in at least three of the last five windows. These thresholds are not clinically validated. Results are measured-pattern changes with many possible causes, not a medical probability, diagnosis, disease progression, or clinical severity.

### Profile and monitoring API

- `GET /api/profiles` – list profile IDs, display names, and baseline availability.
- `POST /api/profiles` – create a profile, e.g. `{"display_name":"Person A","profile_id":"person-a"}`. If omitted, the ID is generated from the display name. IDs are limited to letters, numbers, `_`, and `-`.
- `GET /api/profiles/{profile_id}` – read that profile's local data.
- `POST /api/profiles/{profile_id}/calibrations` – start calibration with `{"duration_seconds":120}`.
- `GET /api/calibration-state` – read backend-owned progress, accepted/rejected packet counts, and result.
- `DELETE /api/calibration` – cancel the active calibration without saving it as a completed baseline.
- `POST /api/profiles/{profile_id}/monitoring` – start monitoring only if that profile has a reliable saved baseline.
- `POST /api/monitoring/end` – compatibility endpoint that ends and persists the active profile session and returns its comparison report.
- `GET /api/profiles/{profile_id}/history` – read calibration/session summaries without raw samples.
- `GET /api/session/history?profile_id={profile_id}` – read profile calibration/session summaries and completed report availability; without an ID, returns unassigned legacy records plus completed-session metadata.
- `GET /api/session/{session_id}/analysis?profile_id={profile_id}` – read saved summary, baseline snapshot, computed statistics, events, limitations, and the session's actual timestamped readings. Session IDs are validated and profile association is checked.
- `GET /api/session/{session_id}/report.pdf?profile_id={profile_id}` – download a valid PDF for that completed session. A missing session or a mismatched profile returns 404.
- `GET /session-report/{session_id}` – load the dedicated completed-session report page; unknown session IDs return 404.
- `GET /api/profiles/{profile_id}/trends` – read per-profile calibration and monitoring feature summaries.
- `GET /api/session/export?profile_id={profile_id}[&session_id={id}]` – export only the named profile's calibration/session summaries, events, and samples with units. Omitting `profile_id` retains the legacy export format but excludes profile-bound records.

The existing `/api/health`, `/api/dashboard-state`, `/api/session/history`, `/api/session/export`, `/api/sensor/ingest`, and `/ws/dashboard` routes remain available. Dashboard state retains legacy fields and adds `profile_state`, `sensor_temperature`, and `sensor_diagnostics`; selected-profile history is scoped to that profile, and unassigned legacy history excludes profile-bound packets. The health and monitoring scores are engineering estimates; no medical inference is made.

Profiles, baselines, calibration samples, completed monitoring readings, baseline snapshots, session summaries, and detected events are stored locally in `data/profiles/profiles.json` using atomic replacement. A completed session's accepted readings are stored once; its report stores derived summary data, not a second copy of the time series. Completed data is retained for historical review and is not silently pruned. The separate legacy session store only retains unassigned packets; profile-bound sensor packets are not duplicated there. Back up or archive the local profile file before moving/reinstalling the application. The existing `.gitignore` excludes local JSON/CSV data under `data/`; keep personal sensor data and credentials out of Git. Active in-progress calibration/monitoring state is process-local and is not resumed after an application restart; completed records and baselines remain saved. Temperature is optional and is absent with the current firmware; if a finite `temperature_c` field is sent, values outside -40 to 125 °C are flagged as implausible and omitted from the trend. Temperature is sensor/chip temperature, not body temperature, and no thermal warning threshold is asserted.

## Experimental ML movement analysis

The ML module is deliberately disabled until a compatible, licensed dataset has been obtained and used to train a model. No model artifact, prediction, clinical metric, or fabricated training data is included in this repository. The public [mPower Researcher Portal](https://www.synapse.org/Synapse:syn4993293) was identified as a possible Parkinson's movement-data source, but this checkout did not establish its project-specific license/data-use terms or verify its label schema, sensor placement, sampling rate, and units. It is therefore **not used** and training is **pending**. A public portal is not itself permission to download or use its data.

The current ESP32 sketch samples every 200 ms (5 Hz). This is sufficient for some time-domain personal-reference features but is not sufficient for the pipeline's 3–10 Hz frequency features or a compatible tremor-pattern classifier. Model training and inference require at least 25 Hz, complete six-axis readings, supported units (m/s² and deg/s), monotonic timestamps, and a stable four-second window. The sensor protocol and firmware are not changed. For the existing device, frequency features and ML classification remain unavailable; the dashboard/report state this explicitly. Do not interpret time-domain baseline changes as a disease signal.

### Training and dataset contract

The reproducible command reads a real, labeled CSV with one sensor sample per row and these columns:

`timestamp_seconds,participant_id,label,ax,ay,az,gx,gy,gz`

Timestamps are Unix seconds; acceleration must be m/s² and gyroscope must be deg/s. Each row must have a genuine participant ID and a source-provided movement label. The trainer rejects invalid values, rates below 25 Hz, incomplete windows, and classes without enough distinct participants for a held-out participant split. It segments contiguous four-second windows with 50% overlap, resamples timing jitter consistently, and uses the same feature-extraction code as inference. Features per axis and vector magnitude include mean, variance, demeaned RMS, peak-to-peak range, dominant spectral peak (0.5–10 Hz), normalized spectral entropy, and 3–10 Hz band power. Feature names, units, and definitions are included in the model metadata. It evaluates a Random Forest and (unless disabled) an SVM on a deterministic participant-independent split, reporting class-wise precision/recall/F1, confusion matrices, balanced accuracy, and macro-F1. The fitted preprocessing pipeline and Random Forest are written as versioned `artifacts/movement-rf-<version>.joblib`, with provenance/evaluation JSON and an active-model pointer. The model exposes class labels only; probabilities/confidence are not reported.

After obtaining data and confirming permission for the intended use, install dependencies and train with the dataset's real provenance and verified license:

```powershell
.\.venv311\Scripts\python.exe -m pip install -r requirements.txt
.\.venv311\Scripts\python.exe -m neuroml.train `
  --dataset C:\path\to\authorized-labeled-samples.csv `
  --dataset-name "Verified dataset title" `
  --dataset-source "https://doi.org/..." `
  --dataset-license "Verified license or data-use terms" `
  --sampling-rate-hz 50 `
  --accel-unit m/s^2 `
  --gyro-unit deg/s `
  --model-version 1.0.0
```

Training requires an authorized dataset; do not fill the provenance arguments with guessed values. The command writes to the app's `artifacts/` directory; `current-model.json` is created only after successful training. Restart NeuroWatch after training so its one-time startup load can read the newly created artifact. A missing or unreadable artifact disables inference with an explicit status; it never creates predictions. Only load trusted joblib files because they use Python object deserialization.

Personal calibration baselines preserve prior calibration records and now include a feature-summary version, available time/frequency statistics, placement (if recorded), test-protocol description, and measured sampling information. Feature summaries use the same four-second, 50%-overlap windows as training. Baseline comparisons show absolute and relative feature changes, not disease risk or severity. The experimental repeated-session flag requires the configured number of valid sessions (default 3 of the last 5) to cross the same fixed-baseline z-threshold (default 2.0); configure with `NEUROWATCH_CHANGE_MIN_SESSIONS`, `NEUROWATCH_CHANGE_LOOKBACK_SESSIONS`, and `NEUROWATCH_CHANGE_Z_THRESHOLD`. Thresholds are exploratory and not clinically validated. Persistent changes are not evidence of Parkinson's progression; review concerning changes with a clinician.

Run the ML and application tests with:

```powershell
.\.venv311\Scripts\python.exe -m pytest
```

### Tests

Run the API, profile, calibration, persistence, comparison, and export tests with:

```powershell
.\.venv311\Scripts\python.exe -m pytest -q
```

The automated calibration tests use deterministic synthetic samples in test code only; the application does not generate or display simulated live readings.

## Team Setup

1. Clone this repository.
2. Create a local Python virtual environment.
3. Install the dependencies documented by the project.
4. Keep personal data and local configuration files out of Git.