#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

#if __has_include("arduino_secrets.h")
#include "arduino_secrets.h"
#else
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* BACKEND_URL = "http://127.0.0.1:8000/api/sensor/ingest";
#endif

const int I2C_SDA_PIN = 21;
const int I2C_SCL_PIN = 22;
const uint32_t SENSOR_INTERVAL_MS = 200;
const bool ENABLE_MAX30102 = false;
const bool ENABLE_FSR = false;
const float RADIANS_TO_DEGREES = 180.0f / PI;

Adafruit_MPU6050 mpu;
uint32_t sequenceNumber = 0;
uint32_t lastSampleTimeMs = 0;

String isoTimestampNow() {
  time_t now = time(nullptr);
  struct tm timeinfo;
  gmtime_r(&now, &timeinfo);
  char buffer[32];
  strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &timeinfo);
  return String(buffer);
}

void connectWiFi() {
  Serial.println("Connecting to WiFi...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts += 1;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.print("WiFi connected. IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println();
    Serial.println("WiFi connection failed. Continuing without a network connection.");
  }
}

String buildSensorPayload(float ax, float ay, float az, float gx, float gy, float gz) {
  const float gxDegreesPerSecond = gx * RADIANS_TO_DEGREES;
  const float gyDegreesPerSecond = gy * RADIANS_TO_DEGREES;
  const float gzDegreesPerSecond = gz * RADIANS_TO_DEGREES;
  String payload = "{";
  payload += "\"sensor\":\"mpu6050\",";
  payload += "\"source\":\"real\",";
  payload += "\"timestamp\":\"" + isoTimestampNow() + "\",";
  payload += "\"sequence_number\":" + String(sequenceNumber) + ",";
  payload += "\"sensor_freshness_ms\":" + String(millis() - lastSampleTimeMs) + ",";
  payload += "\"values\":{\"ax\":" + String(ax, 6) + ",\"ay\":" + String(ay, 6) + ",\"az\":" + String(az, 6) + ",\"gx\":" + String(gxDegreesPerSecond, 6) + ",\"gy\":" + String(gyDegreesPerSecond, 6) + ",\"gz\":" + String(gzDegreesPerSecond, 6) + "},";
  payload += "\"units\":{\"ax\":\"m/s^2\",\"ay\":\"m/s^2\",\"az\":\"m/s^2\",\"gx\":\"deg/s\",\"gy\":\"deg/s\",\"gz\":\"deg/s\"}";
  payload += "}";
  sequenceNumber += 1;
  return payload;
}

bool postSensorPayload(const String& payload) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Skipping upload because WiFi is unavailable.");
    return false;
  }

  WiFiClient client;
  HTTPClient http;

  Serial.print("Posting to ");
  Serial.println(BACKEND_URL);

  if (!http.begin(client, BACKEND_URL)) {
    Serial.println("HTTP connection failed.");
    return false;
  }

  http.addHeader("Content-Type", "application/json");
  int httpCode = http.POST(payload);
  String response = http.getString();
  http.end();

  Serial.print("HTTP status: ");
  Serial.println(httpCode);
  Serial.print("Response: ");
  Serial.println(response);
  return httpCode >= 200 && httpCode < 300;
}

void readAndSendMpuData() {
  sensors_event_t accel;
  sensors_event_t gyro;
  sensors_event_t temp;
  if (!mpu.getEvent(&accel, &gyro, &temp)) {
    Serial.println("MPU6050 read failed.");
    return;
  }

  lastSampleTimeMs = millis();
  String payload = buildSensorPayload(
    accel.acceleration.x,
    accel.acceleration.y,
    accel.acceleration.z,
    gyro.gyro.x,
    gyro.gyro.y,
    gyro.gyro.z
  );

  Serial.println(payload);
  postSensorPayload(payload);
}

void setup() {
  Serial.begin(115200);
  while (!Serial) {
    delay(10);
  }

  Serial.println("NeuroWatch ESP32 Stage 2 booting...");

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  connectWiFi();

  if (!mpu.begin()) {
    Serial.println("Failed to find the MPU6050 chip.");
    while (1) {
      delay(10);
    }
  }

  mpu.setAccelerometerRange(MPU6050_RANGE_2_G);
  mpu.setGyroRange(MPU6050_RANGE_250_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

  Serial.println("MPU6050 initialized.");
  if (ENABLE_MAX30102) {
    Serial.println("MAX30102 placeholder enabled but not yet configured.");
  }
  if (ENABLE_FSR) {
    Serial.println("FSR placeholder enabled but not yet configured.");
  }
}

void loop() {
  if (millis() - lastSampleTimeMs >= SENSOR_INTERVAL_MS) {
    readAndSendMpuData();
  }

  delay(10);
}
