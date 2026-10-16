/*
 * Automatic Solar Tracking System 
 * Arduino sketch: 2 LDRs + SG90/micro servo (pan)
 *
 * Anti-oscillation control law (mirrors shared/control.py):
 *   median filter -> deadband -> N consecutive agreeing samples
 *   -> clamped proportional step -> settle delay
 *
 * Wiring (Arduino Uno/Nano):
 *   LDR left  -> A0   (voltage divider with 10k to GND)
 *   LDR right -> A1   (voltage divider with 10k to GND)
 *   Servo SIG -> D9   (power 5V, GND common)
 *   Optional  LED   -> D13  (fault indicator)
 *
 * Serial protocol (115200 baud):
 *   device -> host, every REPORT_MS:
 *     T,<ts_ms>,<ldr_left>,<ldr_right>,<angle>,<mode>,<state>,<fault>,<reason>,<bootId>
 *   host -> device:
 *     MODE AUTO | MODE MANUAL
 *     MOVE <angle 0-180>
 *     PARK
 *     STATUS
 *
 * EEPROM layout:
 *   addr 0-1 : boot ID (uint16_t, big-endian)  EEPROM_ADDR_BOOT_ID
 *   addr 2   : mode    (uint8_t, 0=AUTO 1=MANUAL) EEPROM_ADDR_MODE
 *   addr 3   : angle   (uint8_t, integer degrees)  EEPROM_ADDR_ANGLE
 */

#include <Servo.h>
#include <EEPROM.h>       // EEPROM persistence of boot ID, mode, angle
#include <avr/eeprom.h>   // low-level EEPROM for 16-bit boot ID read/write
#include <avr/wdt.h>      // watchdog timer

// ---------------- EEPROM address map ----------------
#define EEPROM_ADDR_BOOT_ID  0   // uint16, 2 bytes (addresses 0-1)
#define EEPROM_ADDR_MODE     2   // uint8,  1 byte
#define EEPROM_ADDR_ANGLE    3   // uint8,  1 byte (integer degrees 0-180)

// ---------------- configuration (mirrors TrackerConfig) ----------------
const int   ANGLE_MIN       = 0;
const int   ANGLE_MAX       = 180;
const int   SOFT_MIN        = 10;
const int   SOFT_MAX        = 170;
const int   PARK_ANGLE      = 90;
const int   DEADBAND        = 30;    // ADC counts treated as equal light
const int   CONFIRM_SAMPLES = 3;     // consecutive agreeing samples to move
const int   FILTER_WINDOW   = 5;     // median filter length
const unsigned long SETTLE_MS = 400; // pause after each servo move
const float MIN_STEP        = 2.0f;
const float MAX_STEP        = 15.0f;
const int   STUCK_LOW       = 2;     // ADC rail values -> sensor fault
const int   STUCK_HIGH      = 1021;
const int   STUCK_SAMPLES   = 3;
const unsigned long REPORT_MS = 250; // telemetry report interval
const unsigned long TICK_MS   = 200; // control loop period
const bool BOOT_SELFTEST      = true; // wiggle the servo once at boot

const uint8_t PIN_LDR_LEFT  = A0;
const uint8_t PIN_LDR_RIGHT = A1;
const uint8_t PIN_SERVO     = 9;
const uint8_t PIN_FAULT_LED = 13;

// ---------------- states ----------------
enum Mode { MODE_AUTO, MODE_MANUAL };
enum TrackState {
  ST_ALIGNED,
  ST_EVALUATING,
  ST_SLEWING_LEFT,
  ST_SLEWING_RIGHT,
  ST_MANUAL,
  ST_SENSOR_FAULT
};

// ---------------- globals ----------------
Servo panelServo;
Mode mode = MODE_AUTO;
TrackState state = ST_ALIGNED;

float angle = PARK_ANGLE;
float manualTarget = PARK_ANGLE;
float lastStepDeg = 0;

int leftBuf[FILTER_WINDOW];
int rightBuf[FILTER_WINDOW];
int bufCount = 0;
int filteredLeft = 0, filteredRight = 0;
int lastDiff = 0;

int pendingDir = 0;   // -1 left, 0 none, +1 right
int confirmCount = 0;
int lStuck = 0, rStuck = 0;
bool faultActive = false;
char faultReason[48] = "";
char moveReason[48] = "boot";

unsigned long settleUntilMs = 0;
unsigned long lastTickMs = 0;
unsigned long lastReportMs = 0;
unsigned long bootMs = 0;
unsigned long seq = 0;
int lastMoveDir = 0;
unsigned long moveCount = 0;
unsigned long reversalCount = 0;

// Boot ID: persisted across resets in EEPROM. Incremented every power-on/reset.
uint16_t bootId = 0;

// ---------------- helpers ----------------
float clampf(float v, float lo, float hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

int readMedian(int* buf, int n) {
  // insertion sort a copy, return median
  int tmp[FILTER_WINDOW];
  for (int i = 0; i < n; i++) tmp[i] = buf[i];
  for (int i = 1; i < n; i++) {
    int key = tmp[i];
    int j = i - 1;
    while (j >= 0 && tmp[j] > key) { tmp[j + 1] = tmp[j]; j--; }
    tmp[j + 1] = key;
  }
  return tmp[n / 2];
}

const char* modeName() { return mode == MODE_AUTO ? "AUTO" : "MANUAL"; }

const char* stateName(TrackState s) {
  switch (s) {
    case ST_ALIGNED:       return "ALIGNED";
    case ST_EVALUATING:    return "EVALUATING";
    case ST_SLEWING_LEFT:  return "SLEWING_LEFT";
    case ST_SLEWING_RIGHT: return "SLEWING_RIGHT";
    case ST_MANUAL:        return "MANUAL";
    case ST_SENSOR_FAULT:  return "SENSOR_FAULT";
  }
  return "?";
}

void writeAngle(float a) {
  angle = clampf(a, SOFT_MIN, SOFT_MAX);
  panelServo.write((int)(angle + 0.5f));
  // Persist the new integer angle to EEPROM (only writes if value changed)
  EEPROM.update(EEPROM_ADDR_ANGLE, (uint8_t)(int)(angle + 0.5f));
}

bool commitMove(float target, unsigned long now, const char* reason) {
  target = clampf(target, SOFT_MIN, SOFT_MAX);
  if (fabs(target - angle) < 0.5f) return false;
  lastStepDeg = fabs(target - angle);
  writeAngle(target);
  settleUntilMs = now + SETTLE_MS;
  strncpy(moveReason, reason, sizeof(moveReason) - 1);
  moveCount++;
  return true;
}

void noteDirection(int dir) {
  if (lastMoveDir != 0 && dir != lastMoveDir) reversalCount++;
  lastMoveDir = dir;
}

void setMode(Mode m) {
  if (m == mode) return;
  mode = m;
  if (m == MODE_MANUAL) manualTarget = angle;  // freeze, never jump the panel
  pendingDir = 0;
  confirmCount = 0;
  strncpy(moveReason, m == MODE_AUTO ? "mode -> AUTO" : "mode -> MANUAL",
          sizeof(moveReason) - 1);
  // Persist mode to EEPROM (only writes if value changed)
  EEPROM.update(EEPROM_ADDR_MODE, (uint8_t)m);
}

void updateFault(int fl, int fr) {
  bool leftBad  = (fl <= STUCK_LOW) || (fl >= STUCK_HIGH);
  bool rightBad = (fr <= STUCK_LOW) || (fr >= STUCK_HIGH);

  if (leftBad != rightBad) {
    lStuck = leftBad ? lStuck + 1 : 0;
    rStuck = rightBad ? rStuck + 1 : 0;
  } else {
    lStuck = 0;
    rStuck = 0;
  }

  const char* reason = NULL;
  if (lStuck >= STUCK_SAMPLES) reason = "left LDR stuck at ADC rail";
  else if (rStuck >= STUCK_SAMPLES) reason = "right LDR stuck at ADC rail";

  if (reason && !faultActive) {
    faultActive = true;
    strncpy(faultReason, reason, sizeof(faultReason) - 1);
    // NOTE: manualTarget is deliberately NOT set to PARK_ANGLE.  The fault
    // already freezes the actuator below; setting the target would cause an
    // unrequested slew to park when the fault later clears in MANUAL mode.
    // (Mirrors the Python twin in shared/control.py.)
    digitalWrite(PIN_FAULT_LED, HIGH);
  } else if (!reason && faultActive) {
    faultActive = false;
    faultReason[0] = '\0';
    digitalWrite(PIN_FAULT_LED, LOW);
  }
}

// ---------------- control tick ----------------
void controlTick(unsigned long now) {
  int rawL = analogRead(PIN_LDR_LEFT);
  int rawR = analogRead(PIN_LDR_RIGHT);

  if (bufCount < FILTER_WINDOW) {
    leftBuf[bufCount] = rawL;
    rightBuf[bufCount] = rawR;
    bufCount++;
  } else {
    for (int i = 1; i < FILTER_WINDOW; i++) {
      leftBuf[i - 1] = leftBuf[i];
      rightBuf[i - 1] = rightBuf[i];
    }
    leftBuf[FILTER_WINDOW - 1] = rawL;
    rightBuf[FILTER_WINDOW - 1] = rawR;
  }

  filteredLeft = readMedian(leftBuf, bufCount);
  filteredRight = readMedian(rightBuf, bufCount);
  lastDiff = filteredRight - filteredLeft;

  updateFault(filteredLeft, filteredRight);

  if (faultActive) {
    if (state != ST_SENSOR_FAULT) {
      state = ST_SENSOR_FAULT;
      strncpy(moveReason, "sensor fault - holding", sizeof(moveReason) - 1);
    }
    return;  // freeze the actuator
  }

  // Wrap-safe deadline check: an absolute `now < settleUntilMs` comparison
  // breaks when millis() rolls over (~49.7 days).  The signed difference of
  // the unsigned subtraction is valid for deadlines up to ~24 days ahead,
  // which covers SETTLE_MS comfortably.
  if ((long)(now - settleUntilMs) < 0) return;  // still settling

  if (mode == MODE_MANUAL) {
    state = ST_MANUAL;
    if (fabs(manualTarget - angle) < 0.5f) {
      strncpy(moveReason, "manual hold", sizeof(moveReason) - 1);
      return;
    }
    int dir = manualTarget > angle ? 1 : -1;
    if (commitMove(manualTarget, now, "manual move")) {
      noteDirection(dir);
    }
    return;
  }

  // ---- AUTO ----
  int diff = lastDiff;
  if (abs(diff) <= DEADBAND) {
    pendingDir = 0;
    confirmCount = 0;
    state = ST_ALIGNED;
    strncpy(moveReason, "inside deadband - holding", sizeof(moveReason) - 1);
    return;
  }

  int dir = diff > 0 ? 1 : -1;
  if (dir != pendingDir) {
    pendingDir = dir;
    confirmCount = 1;
  } else {
    confirmCount++;
  }

  if (confirmCount < CONFIRM_SAMPLES) {
    state = ST_EVALUATING;
    strncpy(moveReason, "awaiting confirmation", sizeof(moveReason) - 1);
    return;
  }

  float fullSpan = 1023.0f;
  float fraction = clampf((abs(diff) - DEADBAND) / (fullSpan - DEADBAND), 0.0f, 1.0f);
  float step = MIN_STEP + (MAX_STEP - MIN_STEP) * fraction;
  float target = clampf(angle + dir * step, SOFT_MIN, SOFT_MAX);

  if (fabs(target - angle) < 0.5f) {
    pendingDir = 0;
    confirmCount = 0;
    state = ST_ALIGNED;
    strncpy(moveReason, "at travel limit", sizeof(moveReason) - 1);
    return;
  }

  if (commitMove(target, now, "auto track")) {
    noteDirection(dir);
    state = dir > 0 ? ST_SLEWING_RIGHT : ST_SLEWING_LEFT;
    pendingDir = 0;
    confirmCount = 0;
  }
}

// ---------------- serial protocol ----------------
void report(unsigned long now) {
  // T,ts_ms,left,right,angle,mode,state,fault,reason,bootId
  // left/right are the median-filtered ADC values used by the controller,
  // not a fresh analogRead() which would introduce noise into the telemetry.
  Serial.print(F("T,"));
  Serial.print(now - bootMs);
  Serial.print(',');
  Serial.print(filteredLeft);   // filtered median value, not raw analogRead()
  Serial.print(',');
  Serial.print(filteredRight);  // filtered median value, not raw analogRead()
  Serial.print(',');
  Serial.print(angle, 1);
  Serial.print(',');
  Serial.print(modeName());
  Serial.print(',');
  Serial.print(stateName(state));
  Serial.print(',');
  Serial.print(faultActive ? faultReason : "");
  Serial.print(',');
  Serial.print(moveReason);
  Serial.print(',');
  Serial.println(bootId);       // 10th field: boot counter from EEPROM
}

void printStatus() {
  Serial.print(F("STATUS mode="));
  Serial.print(modeName());
  Serial.print(F(" angle="));
  Serial.print(angle, 1);
  Serial.print(F(" state="));
  Serial.print(stateName(state));
  Serial.print(F(" fault="));
  Serial.println(faultActive ? faultReason : "none");
  Serial.print(F("STATS moves="));
  Serial.print(moveCount);
  Serial.print(F(" reversals="));
  Serial.print(reversalCount);
  Serial.print(F(" diff="));
  Serial.println(lastDiff);
}

void pollSerial();

void handleLine(char* line) {
  // trim
  char* p = line;
  while (*p == ' ' || *p == '\t') p++;
  if (*p == '\0' || *p == '\n' || *p == '\r') return;

  if (strncmp(p, "MODE ", 5) == 0) {
    const char* m = p + 5;
    while (*m == ' ') m++;
    if (strncmp(m, "AUTO", 4) == 0) {
      setMode(MODE_AUTO);
      Serial.println(F("OK mode AUTO"));
    } else if (strncmp(m, "MANUAL", 6) == 0) {
      setMode(MODE_MANUAL);
      Serial.println(F("OK mode MANUAL"));
    } else {
      Serial.println(F("ERR bad mode"));
    }
    return;
  }

  if (strncmp(p, "MOVE ", 5) == 0) {
    float t = atof(p + 5);
    if (t < SOFT_MIN || t > SOFT_MAX) {
      Serial.print(F("ERR angle outside "));
      Serial.print(SOFT_MIN);
      Serial.print('-');
      Serial.println(SOFT_MAX);
      return;
    }
    if (mode != MODE_MANUAL) {
      Serial.println(F("ERR switch to MANUAL first"));
      return;
    }
    manualTarget = t;
    Serial.print(F("OK manual target "));
    Serial.println(manualTarget, 1);
    return;
  }

  if (strncmp(p, "PARK", 4) == 0) {
    setMode(MODE_MANUAL);
    manualTarget = PARK_ANGLE;
    Serial.print(F("OK park "));
    Serial.println(PARK_ANGLE);
    return;
  }

  if (strncmp(p, "STATUS", 6) == 0) {
    printStatus();
    return;
  }

  if (strncmp(p, "SWEEP", 5) == 0) {
    Serial.println(F("OK sweep"));
    // full soft-limit sweep so you can prove the servo + wiring work
    for (int a = SOFT_MIN; a <= SOFT_MAX; a += 3) {
      writeAngle(a);
      delay(15);
      pollSerial();
    }
    for (int a = SOFT_MAX; a >= SOFT_MIN; a -= 3) {
      writeAngle(a);
      delay(15);
      pollSerial();
    }
    writeAngle(PARK_ANGLE);
    Serial.println(F("OK sweep done"));
    return;
  }

  if (strncmp(p, "DEBUG", 5) == 0) {
    Serial.print(F("DEBUG L="));
    Serial.print(filteredLeft);
    Serial.print(F(" R="));
    Serial.print(filteredRight);
    Serial.print(F(" diff="));
    Serial.print(lastDiff);
    Serial.print(F(" deadband="));
    Serial.print(DEADBAND);
    Serial.print(F(" confirm="));
    Serial.print(confirmCount);
    Serial.print(F("/"));
    Serial.print(CONFIRM_SAMPLES);
    Serial.print(F(" settle="));
    Serial.print((long)(settleUntilMs - millis()));  // wrap-safe signed delta
    Serial.print(F(" mode="));
    Serial.print(modeName());
    Serial.print(F(" fault="));
    Serial.println(faultActive ? faultReason : "none");
    return;
  }

  Serial.println(F("ERR unknown command"));
}

char rxBuf[48];
uint8_t rxLen = 0;

void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (rxLen > 0) {
        rxBuf[rxLen] = '\0';
        handleLine(rxBuf);
        rxLen = 0;
      }
    } else if (rxLen < sizeof(rxBuf) - 1) {
      rxBuf[rxLen++] = c;
    }
  }
}

// ---------------- arduino entry points ----------------
void servoSelfTest() {
  // short, obvious wiggle: proves attach() + signal wire + power are OK
  Serial.println(F("SELFTEST servo wiggle..."));
  writeAngle(PARK_ANGLE);
  delay(300);
  writeAngle(PARK_ANGLE + 30);
  delay(350);
  writeAngle(PARK_ANGLE - 30);
  delay(350);
  writeAngle(PARK_ANGLE);
  delay(300);
  Serial.println(F("SELFTEST done - if it did not move, check wiring"));
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_FAULT_LED, OUTPUT);
  digitalWrite(PIN_FAULT_LED, LOW);

  // ---- Boot ID: read from EEPROM, increment, write back ----
  // Uses avr/eeprom.h eeprom_read_word / eeprom_write_word for atomic 16-bit access.
  // Blank (erased) EEPROM reads 0xFFFF; treat that as "first boot -> ID 1".
  uint16_t storedId = eeprom_read_word((uint16_t*)EEPROM_ADDR_BOOT_ID);
  bootId = (storedId == 0xFFFF) ? 1 : storedId + 1;
  eeprom_write_word((uint16_t*)EEPROM_ADDR_BOOT_ID, bootId);

  // ---- Restore persisted mode ----
  uint8_t storedMode = EEPROM.read(EEPROM_ADDR_MODE);
  if (storedMode == 0) {
    mode = MODE_AUTO;
  } else if (storedMode == 1) {
    mode = MODE_MANUAL;
  }
  // (any other value means EEPROM was blank; leave default MODE_AUTO)

  // ---- Restore persisted angle ----
  uint8_t storedAngle = EEPROM.read(EEPROM_ADDR_ANGLE);
  if (storedAngle >= SOFT_MIN && storedAngle <= SOFT_MAX) {
    angle = storedAngle;
    manualTarget = angle;
  } else {
    angle = PARK_ANGLE;
    manualTarget = PARK_ANGLE;
  }

  panelServo.attach(PIN_SERVO);
  panelServo.write((int)(angle + 0.5f));  // move to restored angle directly

  for (int i = 0; i < FILTER_WINDOW; i++) {
    leftBuf[i] = 512;
    rightBuf[i] = 512;
  }
  bufCount = FILTER_WINDOW;

  bootMs = millis();
  lastTickMs = bootMs;
  lastReportMs = bootMs;

  Serial.println(F("READY solar-tracker"));
  Serial.print(F("BOOT_ID "));
  Serial.println(bootId);
  if (BOOT_SELFTEST) servoSelfTest();
  Serial.println(F("cmds: MODE AUTO|MANUAL, MOVE <deg>, PARK, STATUS, DEBUG, SWEEP"));

  // ---- Watchdog: reset the MCU if loop() stalls for more than 8 seconds ----
  // wdt_reset() must be called at the top of loop() to keep the dog fed.
  wdt_enable(WDTO_8S);
}

void loop() {
  // Reset the watchdog at the top of every iteration.
  // If the MCU hangs (e.g. in a long blocking call or an infinite loop caused
  // by a software bug), the watchdog will fire after 8 s and force a reboot.
  wdt_reset();

  unsigned long now = millis();
  pollSerial();

  if (now - lastTickMs >= TICK_MS) {
    lastTickMs = now;
    controlTick(now);
    seq++;
  }

  if (now - lastReportMs >= REPORT_MS) {
    lastReportMs = now;
    report(now);
  }
}
