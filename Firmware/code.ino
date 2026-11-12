#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <TinyGPSPlus.h>
#include <WiFi.h>
#include <WebServer.h>

// =====================================================
// PIN DEFINITIONS
// =====================================================

// DS18B20
#define DS18B20_PIN 4

// LED
#define LED_PIN 2

// Solar voltage ADC
#define SOLAR_ADC_PIN 34

// MPU-6515
#define SDA_PIN 21
#define SCL_PIN 22
#define MPU_ADDR 0x68

// GPS
#define GPS_RX_PIN 16
#define GPS_TX_PIN 17

// SD CARD
#define SD_MISO 27
#define SD_SCK  26
#define SD_MOSI 12
#define SD_CS   13

// =====================================================
// WIFI
// =====================================================

const char* AP_SSID = "BUOY_MONITOR";
const char* AP_PASSWORD = "buoy1234";

WebServer server(80);

// =====================================================
// OBJECTS
// =====================================================

OneWire oneWire(DS18B20_PIN);
DallasTemperature tempSensor(&oneWire);

TinyGPSPlus gps;
HardwareSerial GPSserial(2);

// =====================================================
// STATUS
// =====================================================

bool sdOK = false;
bool mpuOK = false;
bool tempOK = false;

String logFileName = "";

// =====================================================
// LIVE DATA
// =====================================================

float liveTemperature = NAN;
float liveSolarVoltage = 0;

float liveAx = 0;
float liveAy = 0;
float liveAz = 0;

float liveGx = 0;
float liveGy = 0;
float liveGz = 0;

float liveImuTemp = 0;

double liveLatitude = 0;
double liveLongitude = 0;

int liveSatellites = 0;

unsigned long lastMeasurement = 0;

// =====================================================
// MPU-6515 FUNCTIONS
// =====================================================

void mpuWriteByte(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(value);
  Wire.endTransmission();
}

uint8_t mpuReadByte(uint8_t reg) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);

  Wire.requestFrom(MPU_ADDR, (uint8_t)1);

  if (Wire.available()) {
    return Wire.read();
  }

  return 0xFF;
}

bool mpuInit() {

  uint8_t whoami = mpuReadByte(0x75);

  Serial.print("     WHO_AM_I: 0x");
  Serial.println(whoami, HEX);

  if (whoami != 0x74) {
    Serial.println("     ERROR: MPU-6515 not detected");
    return false;
  }

  Serial.println("     MPU-6515 detected");

  // Reset
  mpuWriteByte(0x6B, 0x80);
  delay(100);

  // Wake
  mpuWriteByte(0x6B, 0x00);
  delay(50);

  // Clock source = PLL
  mpuWriteByte(0x6B, 0x01);

  // DLPF
  mpuWriteByte(0x1A, 0x04);

  // Sample rate = 100 Hz
  mpuWriteByte(0x19, 9);

  // Gyroscope ±500 °/s
  mpuWriteByte(0x1B, 0x08);

  // Accelerometer ±8 g
  mpuWriteByte(0x1C, 0x10);

  // Accelerometer DLPF
  mpuWriteByte(0x1D, 0x03);

  delay(100);

  return true;
}

bool readMPU(
  float &ax,
  float &ay,
  float &az,
  float &gx,
  float &gy,
  float &gz,
  float &imuTemp
) {

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);

  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  uint8_t count = Wire.requestFrom(
    MPU_ADDR,
    (uint8_t)14
  );

  if (count != 14) {
    return false;
  }

  int16_t rawAx =
    (int16_t)((Wire.read() << 8) | Wire.read());

  int16_t rawAy =
    (int16_t)((Wire.read() << 8) | Wire.read());

  int16_t rawAz =
    (int16_t)((Wire.read() << 8) | Wire.read());

  int16_t rawTemp =
    (int16_t)((Wire.read() << 8) | Wire.read());

  int16_t rawGx =
    (int16_t)((Wire.read() << 8) | Wire.read());

  int16_t rawGy =
    (int16_t)((Wire.read() << 8) | Wire.read());

  int16_t rawGz =
    (int16_t)((Wire.read() << 8) | Wire.read());

  // ±8 g
  ax = rawAx / 4096.0;
  ay = rawAy / 4096.0;
  az = rawAz / 4096.0;

  // ±500 °/s
  gx = rawGx / 65.5;
  gy = rawGy / 65.5;
  gz = rawGz / 65.5;

  // MPU temperature
  imuTemp = (rawTemp / 333.87) + 21.0;

  return true;
}

// =====================================================
// CREATE NEW LOG FILE
// =====================================================

String createNewLogFile() {

  int fileNumber = 1;
  String filename;

  while (true) {

    if (fileNumber > 99999) {
      Serial.println("ERROR: Too many log files");
      return "";
    }

    filename = "/test_" + String(fileNumber) + ".csv";

    if (!SD.exists(filename)) {
      break;
    }

    fileNumber++;
  }

  File file = SD.open(filename, FILE_WRITE);

  if (!file) {
    Serial.println("ERROR: Could not create log file");
    return "";
  }

  file.println(
    "millis,temp,solar,"
    "ax,ay,az,gx,gy,gz,"
    "imu_temp,lat,lon,satellites"
  );

  file.flush();
  file.close();

  Serial.print("     New log file: ");
  Serial.println(filename);

  return filename;
}

// =====================================================
// WIFI WEB PAGE
// =====================================================

const char MAIN_PAGE[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Buoy Live Monitor</title>

<style>
body {
  margin: 0;
  font-family: Arial, sans-serif;
  background: #07111f;
  color: #eaf4ff;
}

.container {
  max-width: 900px;
  margin: auto;
  padding: 20px;
}

h1 {
  text-align: center;
  margin-bottom: 5px;
}

.subtitle {
  text-align: center;
  color: #8fa8bd;
  margin-bottom: 25px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
}

.card {
  background: #101f31;
  border: 1px solid #213b55;
  border-radius: 12px;
  padding: 18px;
}

.label {
  color: #8fa8bd;
  font-size: 13px;
  margin-bottom: 8px;
}

.value {
  font-size: 25px;
  font-weight: bold;
}

.unit {
  font-size: 13px;
  color: #8fa8bd;
}

.status {
  margin-top: 18px;
  background: #101f31;
  border-radius: 12px;
  padding: 16px;
}

.ok {
  color: #55e391;
}

.bad {
  color: #ff6b6b;
}

.footer {
  text-align: center;
  color: #667f95;
  margin-top: 20px;
  font-size: 12px;
}
</style>
</head>

<body>

<div class="container">

<h1>🌊 BUOY LIVE MONITOR</h1>
<div class="subtitle">ESP32 telemetry dashboard</div>

<div class="grid">

<div class="card">
<div class="label">WATER TEMPERATURE</div>
<div class="value"><span id="temp">--</span> <span class="unit">°C</span></div>
</div>

<div class="card">
<div class="label">SOLAR VOLTAGE</div>
<div class="value"><span id="solar">--</span> <span class="unit">V</span></div>
</div>

<div class="card">
<div class="label">SATELLITES</div>
<div class="value"><span id="sat">--</span></div>
</div>

<div class="card">
<div class="label">GPS LATITUDE</div>
<div class="value" style="font-size:18px"><span id="lat">--</span></div>
</div>

<div class="card">
<div class="label">GPS LONGITUDE</div>
<div class="value" style="font-size:18px"><span id="lon">--</span></div>
</div>

<div class="card">
<div class="label">ACCEL X</div>
<div class="value"><span id="ax">--</span> <span class="unit">g</span></div>
</div>

<div class="card">
<div class="label">ACCEL Y</div>
<div class="value"><span id="ay">--</span> <span class="unit">g</span></div>
</div>

<div class="card">
<div class="label">ACCEL Z</div>
<div class="value"><span id="az">--</span> <span class="unit">g</span></div>
</div>

<div class="card">
<div class="label">GYRO X</div>
<div class="value"><span id="gx">--</span> <span class="unit">°/s</span></div>
</div>

<div class="card">
<div class="label">GYRO Y</div>
<div class="value"><span id="gy">--</span> <span class="unit">°/s</span></div>
</div>

<div class="card">
<div class="label">GYRO Z</div>
<div class="value"><span id="gz">--</span> <span class="unit">°/s</span></div>
</div>

<div class="card">
<div class="label">IMU TEMPERATURE</div>
<div class="value"><span id="imutemp">--</span> <span class="unit">°C</span></div>
</div>

</div>

<div class="status">
<b>SD CARD:</b>
<span id="sd">--</span>
<br><br>

<b>LOG FILE:</b>
<span id="file">--</span>
<br><br>

<b>LAST SAMPLE:</b>
<span id="time">--</span> ms
</div>

<div class="footer">
Auto-refreshing every 1 second
</div>

</div>

<script>

function updateData() {

  fetch('/data')
  .then(response => response.json())
  .then(d => {

    document.getElementById('temp').innerText = d.temp;
    document.getElementById('solar').innerText = d.solar;

    document.getElementById('sat').innerText = d.satellites;

    document.getElementById('lat').innerText = d.lat;
    document.getElementById('lon').innerText = d.lon;

    document.getElementById('ax').innerText = d.ax;
    document.getElementById('ay').innerText = d.ay;
    document.getElementById('az').innerText = d.az;

    document.getElementById('gx').innerText = d.gx;
    document.getElementById('gy').innerText = d.gy;
    document.getElementById('gz').innerText = d.gz;

    document.getElementById('imutemp').innerText = d.imutemp;

    document.getElementById('sd').innerHTML =
      d.sd ?
      '<span class="ok">● RECORDING</span>' :
      '<span class="bad">● ERROR</span>';

    document.getElementById('file').innerText = d.file;
    document.getElementById('time').innerText = d.millis;
  })
  .catch(error => {
    console.log(error);
  });
}

updateData();
setInterval(updateData, 1000);

</script>

</body>
</html>
)rawliteral";

// =====================================================
// WIFI DATA API
// =====================================================

void handleRoot() {
  server.send_P(
    200,
    "text/html",
    MAIN_PAGE
  );
}

void handleData() {

  String json = "{";

  json += "\"millis\":" + String(millis());
  json += ",\"temp\":" + String(liveTemperature, 2);
  json += ",\"solar\":" + String(liveSolarVoltage, 3);

  json += ",\"ax\":" + String(liveAx, 3);
  json += ",\"ay\":" + String(liveAy, 3);
  json += ",\"az\":" + String(liveAz, 3);

  json += ",\"gx\":" + String(liveGx, 3);
  json += ",\"gy\":" + String(liveGy, 3);
  json += ",\"gz\":" + String(liveGz, 3);

  json += ",\"imutemp\":" + String(liveImuTemp, 2);

  json += ",\"lat\":" + String(liveLatitude, 6);
  json += ",\"lon\":" + String(liveLongitude, 6);

  json += ",\"satellites\":" + String(liveSatellites);

  json += ",\"sd\":";
  json += sdOK ? "true" : "false";

  json += ",\"file\":\"";
  json += logFileName;
  json += "\"";

  json += "}";

  server.send(
    200,
    "application/json",
    json
  );
}

void setupWiFi() {

  Serial.println("\n[8] WIFI MONITOR");

  WiFi.mode(WIFI_AP);

  bool apStarted = WiFi.softAP(
    AP_SSID,
    AP_PASSWORD
  );

  if (apStarted) {

    Serial.println("     WiFi AP started");
    Serial.print("     SSID: ");
    Serial.println(AP_SSID);

    Serial.print("     Password: ");
    Serial.println(AP_PASSWORD);

    Serial.print("     Dashboard: http://");
    Serial.println(WiFi.softAPIP());

    server.on("/", handleRoot);
    server.on("/data", handleData);

    server.begin();

    Serial.println("     Web server started");

  } else {

    Serial.println("     ERROR: WiFi AP failed");

  }
}

// =====================================================
// SETUP
// =====================================================

void setup() {

  Serial.begin(115200);

  delay(1000);

  Serial.println();
  Serial.println("====================================");
  Serial.println("       BUOY HARDWARE TEST");
  Serial.println("====================================");

  // ---------------------------------------------------
  // LED
  // ---------------------------------------------------

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.println("\n[1] LED");
  Serial.println("     OK");

  // ---------------------------------------------------
  // I2C
  // ---------------------------------------------------

  Serial.println("\n[2] I2C");

  Wire.begin(
    SDA_PIN,
    SCL_PIN
  );

  Wire.setClock(400000);

  delay(100);

  // ---------------------------------------------------
  // DS18B20
  // ---------------------------------------------------

  Serial.println("\n[3] DS18B20");

  tempSensor.begin();

  int deviceCount =
    tempSensor.getDeviceCount();

  if (deviceCount > 0) {

    tempOK = true;

    Serial.print("     Found ");
    Serial.print(deviceCount);
    Serial.println(" sensor(s)");

  } else {

    Serial.println(
      "     ERROR: DS18B20 not found"
    );

  }

  // ---------------------------------------------------
  // MPU-6515
  // ---------------------------------------------------

  Serial.println("\n[4] MPU-6515");

  mpuOK = mpuInit();

  // ---------------------------------------------------
  // GPS
  // ---------------------------------------------------

  Serial.println("\n[5] NEO-6M GPS");

  GPSserial.begin(
    9600,
    SERIAL_8N1,
    GPS_RX_PIN,
    GPS_TX_PIN
  );

  Serial.println(
    "     GPS UART started"
  );

  Serial.println(
    "     Waiting for GPS data..."
  );

  // ---------------------------------------------------
  // SD CARD
  // ---------------------------------------------------

  Serial.println("\n[6] SD CARD");

  SPI.begin(
    SD_SCK,
    SD_MISO,
    SD_MOSI,
    SD_CS
  );

  if (SD.begin(SD_CS, SPI)) {

    sdOK = true;

    Serial.println(
      "     SD card detected"
    );

    uint64_t cardSize =
      SD.cardSize() / (1024 * 1024);

    Serial.print(
      "     Card size: "
    );

    Serial.print(cardSize);

    Serial.println(" MB");

    logFileName = createNewLogFile();

    if (logFileName == "") {
      sdOK = false;
    }

  } else {

    Serial.println(
      "     ERROR: SD card initialization failed"
    );

  }

  // ---------------------------------------------------
  // SOLAR ADC
  // ---------------------------------------------------

  Serial.println(
    "\n[7] SOLAR VOLTAGE ADC"
  );

  analogReadResolution(12);

  analogSetPinAttenuation(
    SOLAR_ADC_PIN,
    ADC_11db
  );

  Serial.println(
    "     GPIO34 configured"
  );

  // ---------------------------------------------------
  // WIFI
  // ---------------------------------------------------

  setupWiFi();

  // ---------------------------------------------------
  // SUMMARY
  // ---------------------------------------------------

  Serial.println();

  Serial.println(
    "===================================="
  );

  Serial.println(
    "       INITIALIZATION COMPLETE"
  );

  Serial.println(
    "===================================="
  );

  Serial.print("DS18B20 : ");
  Serial.println(
    tempOK ? "OK" : "FAIL"
  );

  Serial.print("MPU-6515 : ");
  Serial.println(
    mpuOK ? "OK" : "FAIL"
  );

  Serial.print("SD Card : ");
  Serial.println(
    sdOK ? "OK" : "FAIL"
  );

  Serial.print("Log File : ");
  Serial.println(
    logFileName
  );

  Serial.println(
    "GPS     : UART ACTIVE"
  );

  Serial.println(
    "Solar   : ADC ACTIVE"
  );

  Serial.println(
    "LED     : ACTIVE"
  );

  Serial.println();

  Serial.println(
    "Connect to WiFi network:"
  );

  Serial.println(
    "SSID: BUOY_MONITOR"
  );

  Serial.println(
    "Password: buoy1234"
  );

  Serial.println(
    "Then open: http://192.168.4.1"
  );

  Serial.println();

  Serial.println(
    "Starting live measurements..."
  );

  Serial.println();
}

// =====================================================
// LOOP
// =====================================================

void loop() {

  // ---------------------------------------------------
  // SERVICE WIFI
  // ---------------------------------------------------

  server.handleClient();

  // ---------------------------------------------------
  // READ GPS
  // ---------------------------------------------------

  while (GPSserial.available()) {

    gps.encode(
      GPSserial.read()
    );
  }

  // ---------------------------------------------------
  // READ WATER TEMPERATURE
  // ---------------------------------------------------

  float temperature = NAN;

  tempSensor.requestTemperatures();

  temperature =
    tempSensor.getTempCByIndex(0);

  liveTemperature = temperature;

  // ---------------------------------------------------
  // READ MPU-6515
  // ---------------------------------------------------

  float ax = 0;
  float ay = 0;
  float az = 0;

  float gx = 0;
  float gy = 0;
  float gz = 0;

  float imuTemp = 0;

  bool imuReadOK = false;

  if (mpuOK) {

    imuReadOK =
      readMPU(
        ax,
        ay,
        az,
        gx,
        gy,
        gz,
        imuTemp
      );
  }

  if (imuReadOK) {

    liveAx = ax;
    liveAy = ay;
    liveAz = az;

    liveGx = gx;
    liveGy = gy;
    liveGz = gz;

    liveImuTemp = imuTemp;
  }

  // ---------------------------------------------------
  // READ SOLAR VOLTAGE
  // ---------------------------------------------------

  int adcValue =
    analogRead(SOLAR_ADC_PIN);

  float adcVoltage =
    (adcValue / 4095.0) * 3.3;

  // Voltage divider:
  // Solar -> 100k -> GPIO34 -> 27k -> GND

  float solarVoltage =
    adcVoltage * (127.0 / 27.0);

  liveSolarVoltage = solarVoltage;

  // ---------------------------------------------------
  // GPS DATA
  // ---------------------------------------------------

  double latitude = 0;
  double longitude = 0;

  int satellites = 0;

  if (gps.location.isValid()) {

    latitude =
      gps.location.lat();

    longitude =
      gps.location.lng();
  }

  if (gps.satellites.isValid()) {

    satellites =
      gps.satellites.value();
  }

  liveLatitude = latitude;
  liveLongitude = longitude;
  liveSatellites = satellites;

  // ---------------------------------------------------
  // SERIAL OUTPUT
  // ---------------------------------------------------

  Serial.println(
    "------------------------------------"
  );

  Serial.print(
    "Water Temp  : "
  );

  Serial.print(
    temperature,
    2
  );

  Serial.println(" °C");

  Serial.print(
    "Solar       : "
  );

  Serial.print(
    solarVoltage,
    3
  );

  Serial.println(" V");

  if (imuReadOK) {

    Serial.print(
      "Accel       : "
    );

    Serial.print(
      ax,
      3
    );

    Serial.print(" , ");

    Serial.print(
      ay,
      3
    );

    Serial.print(" , ");

    Serial.print(
      az,
      3
    );

    Serial.println(" g");

    Serial.print(
      "Gyro        : "
    );

    Serial.print(
      gx,
      3
    );

    Serial.print(" , ");

    Serial.print(
      gy,
      3
    );

    Serial.print(" , ");

    Serial.print(
      gz,
      3
    );

    Serial.println(" °/s");

    Serial.print(
      "IMU Temp    : "
    );

    Serial.print(
      imuTemp,
      2
    );

    Serial.println(" °C");

  } else {

    Serial.println(
      "IMU         : READ FAILED"
    );
  }

  Serial.print(
    "GPS         : "
  );

  if (gps.location.isValid()) {

    Serial.print(
      latitude,
      6
    );

    Serial.print(" , ");

    Serial.print(
      longitude,
      6
    );

  } else {

    Serial.print(
      "NO FIX"
    );
  }

  Serial.print(
    "  Satellites: "
  );

  Serial.println(
    satellites
  );

  // ---------------------------------------------------
  // SAVE TO SD
  // ---------------------------------------------------

  bool writeSuccessful =
    false;

  if (sdOK && logFileName != "") {

    File file =
      SD.open(
        logFileName,
        FILE_APPEND
      );

    if (file) {

      file.print(
        millis()
      );

      file.print(",");

      file.print(
        temperature,
        2
      );

      file.print(",");

      file.print(
        solarVoltage,
        3
      );

      file.print(",");

      file.print(
        ax,
        3
      );

      file.print(",");

      file.print(
        ay,
        3
      );

      file.print(",");

      file.print(
        az,
        3
      );

      file.print(",");

      file.print(
        gx,
        3
      );

      file.print(",");

      file.print(
        gy,
        3
      );

      file.print(",");

      file.print(
        gz,
        3
      );

      file.print(",");

      file.print(
        imuTemp,
        2
      );

      file.print(",");

      file.print(
        latitude,
        6
      );

      file.print(",");

      file.print(
        longitude,
        6
      );

      file.print(",");

      file.println(
        satellites
      );

      file.flush();
      file.close();

      writeSuccessful = true;

      Serial.println(
        "SD WRITE    : SUCCESS"
      );

    } else {

      Serial.println(
        "SD WRITE    : FAILED"
      );
    }
  }

  // ---------------------------------------------------
  // FLASH LED ONLY AFTER SD SUCCESS
  // ---------------------------------------------------

  if (writeSuccessful) {

    digitalWrite(
      LED_PIN,
      HIGH
    );

    delay(50);

    digitalWrite(
      LED_PIN,
      LOW
    );
  }

  // ---------------------------------------------------
  // WAIT
  // ---------------------------------------------------

  delay(1000);
}