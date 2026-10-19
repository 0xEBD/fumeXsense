/*
 * WX2 Hello World — XIAO ESP32-C3 <-> Weller WX2 + Shelly Plug S Gen3
 * fumeXsense PoC 002: OLED HMI, push button, splash screen
 *
 * Reads the WX2 status via RS232 and controls the fume extractor via
 * a Shelly Plug S Gen3 (HTTP RPC API):
 *   - at least one channel ON -> extractor ON
 *   - no channel ON           -> extractor OFF after run-on delay
 *   - the desired state is periodically validated against the actual
 *     Shelly state (external changes, e.g. button on the plug)
 *   - warning on low power draw despite relay ON
 *   - OLED (SSD1306 128x64, I2C) with a 3-band status display
 *   - push button: short = cancel run-on (OFF immediately),
 *                  long (>=800ms) = manual mode ON/OFF (continuous run)
 *
 * !! HARDWARE WARNING !!
 * The WX2 serial port uses true RS232 levels (+/-12V) and must never be
 * connected directly to the ESP32-C3. A MAX3232 RS232 transceiver
 * (3.3 V) is mandatory — see hw/README.md.
 *
 * Wiring (XIAO ESP32-C3, UART1, B1 sample board):
 *   D6 / GPIO21 (TX) -> MAX3232 -> WX2 RX
 *   D7 / GPIO20 (RX) <- MAX3232 <- WX2 TX
 *   Common GND.
 *
 * OLED (I2C, XIAO ESP32-C3 default bus):
 *   D4 / GPIO6 -> SDA
 *   D5 / GPIO7 -> SCL
 *   3V3, GND.
 *
 * Push button:
 *   D2 / GPIO4 -> button -> GND (INPUT_PULLUP, no external resistor)
 *
 * by 0xEBD — MIT License
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>   // OLED
#include "wx2_splash.h"         // Animated splash screen
#include "secrets.h"            // copy secrets.h.example -> secrets.h

// ---------- Configuration ----------
const char* WIFI_SSID = SECRET_WIFI_SSID;
const char* WIFI_PASS = SECRET_WIFI_PASS;
const char* SHELLY_IP = SECRET_SHELLY_IP;       // Shelly Plug S Gen3

const unsigned long EXTRACTOR_OFF_DELAY_MS = 30000;  // run-on delay
const unsigned long SHELLY_VALIDATE_MS     = 10000;  // re-check state
const float LOW_POWER_WARN_W               = 5.0f;   // warning threshold

// ---------- Push button ----------
const int BTN_PIN                    = 4;      // XIAO D2, not a strapping pin
const unsigned long BTN_DEBOUNCE_MS  = 30;
const unsigned long BTN_LONGPRESS_MS = 800;

// ---------- WX2 UART ----------
HardwareSerial wx(1);
const int WX_RX = 20;          // XIAO D7
const int WX_TX = 21;          // XIAO D6
const unsigned long WX_TIMEOUT_MS  = 1500;
const unsigned long WX_IDLE_GAP_MS = 120;
const int FRAME_LEN = 7;

// ---------- Shelly state ----------
bool shellyDesiredOn   = false;
bool shellyReportedOn  = false;
float shellyPowerW     = 0.0f;
unsigned long lastAnyChannelOn = 0;
unsigned long lastValidate     = 0;
bool anyChannelOn = false;
bool fumeManual   = false;     // continuous run via long button press

// ---------- OLED + display snapshot ----------
#define OLED_W    128
#define OLED_H     64
#define OLED_ADDR 0x3C              // 128x64 modules: usually 0x3C, else 0x3D
#define OLED_RST    -1              // no reset pin on the XIAO
Adafruit_SSD1306 oled(OLED_W, OLED_H, &Wire, OLED_RST);
bool  wx2Connected    = false;              // valid Q frame received
bool  shellyConnected = false;              // last GetStatus returned a body
char  chStatus[2]     = {'?', '?'};         // channel status codes (from Q)
float chActual[2]     = {NAN, NAN};         // actual temperatures
bool  blinkPhase      = false;              // toggled once per poll cycle

// Forward declaration (button is also polled inside wxRead)
void buttonPoll();

// ================= WX2 protocol =================

String wxRead(int nBytes) {
  String resp;
  unsigned long start = millis();
  unsigned long lastByte = millis();
  while (millis() - start < WX_TIMEOUT_MS) {
    while (wx.available()) {
      char c = wx.read();
      if (c == '\r' || c == '\n') continue;
      resp += c;
      lastByte = millis();
      if ((int)resp.length() >= nBytes) return resp;
    }
    if (resp.length() > 0 && millis() - lastByte > WX_IDLE_GAP_MS) break;
    buttonPoll();               // capture button events while waiting;
    delay(2);                   // they are executed only after the
  }                             // serial traffic (see buttonService)
  return resp;
}

bool frameChecksumOk(const String& f) {
  if (f.length() != FRAME_LEN) return false;
  int sum = 0;
  for (int i = 0; i < FRAME_LEN - 1; i++) sum += (uint8_t)f[i];
  return (char)(sum % 256) == f[FRAME_LEN - 1];
}

int wxCommand(const char* cmd, String out[], int frames) {
  while (wx.available()) wx.read();
  wx.print(cmd);
  String resp = wxRead(frames * FRAME_LEN);
  if (resp.length() == 0) {
    Serial.printf("  [%s] no response (check wiring/baud)\n", cmd);
    return 0;
  }
  int n = 0;
  for (int pos = 0; pos + FRAME_LEN <= (int)resp.length() && n < frames;
       pos += FRAME_LEN) {
    String f = resp.substring(pos, pos + FRAME_LEN);
    if (frameChecksumOk(f)) out[n++] = f;
    else Serial.printf("  [%s] checksum failed on frame: %s\n", cmd, f.c_str());
  }
  return n;
}

const char* statusName(char code) {
  switch (code) {
    case '0': return "OFF";
    case '1': return "ON";
    case '2': return "STANDBY";
    case '3': return "AUTO-OFF";
    default:  return "UNKNOWN";
  }
}

const char* toolName(int code) {
  switch (code) {
    case 0: return "no tool";
    case 1: return "WXP120";
    case 2: return "WXP200";
    case 3: return "WXMP";
    case 4: return "WXMT";
    case 5: return "WXP65";
    case 6: return "WXP80";
    case 7: return "WXB200";
    default: return "unknown";
  }
}

int frameValue(const String& f)   { return f.substring(2, 6).toInt(); }
int frameChannel(const String& f) { return (f[1] == '2') ? 1 : 0; }

// ================= Shelly Gen3 RPC =================

// GET http://<ip>/rpc/<method+query>, returns body or "" on error
String shellyRpc(const String& methodAndQuery) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("  [shelly] WiFi not connected");
    return "";
  }
  HTTPClient http;
  http.setTimeout(2000);
  String url = String("http://") + SHELLY_IP + "/rpc/" + methodAndQuery;
  if (!http.begin(url)) return "";
  int code = http.GET();
  String body = (code == HTTP_CODE_OK) ? http.getString() : "";
  if (code != HTTP_CODE_OK)
    Serial.printf("  [shelly] HTTP %d on %s\n", code, methodAndQuery.c_str());
  http.end();
  return body;
}

// Primitive JSON field lookup, sufficient for the RPC responses
bool jsonBool(const String& body, const char* key, bool& out) {
  int i = body.indexOf(String("\"") + key + "\":");
  if (i < 0) return false;
  i = body.indexOf(':', i) + 1;
  out = body.substring(i, i + 4) == "true";
  return true;
}

bool jsonFloat(const String& body, const char* key, float& out) {
  int i = body.indexOf(String("\"") + key + "\":");
  if (i < 0) return false;
  i = body.indexOf(':', i) + 1;
  out = body.substring(i).toFloat();
  return true;
}

void shellySet(bool on) {
  String body = shellyRpc(String("Switch.Set?id=0&on=") + (on ? "true" : "false"));
  if (body.length()) {
    shellyDesiredOn  = on;
    shellyReportedOn = on;      // switch confirmed -> display follows at once
    lastValidate     = 0;       // re-read power/state in the next cycle
    Serial.printf("Extractor -> %s\n", on ? "ON" : "OFF");
  }
}

// Fetch actual state + power from the Shelly and sync the desired state
void shellyValidate() {
  String body = shellyRpc("Switch.GetStatus?id=0");
  shellyConnected = body.length() > 0;          // connection status for the OLED
  if (!body.length()) return;

  bool outp;
  if (jsonBool(body, "output", outp)) {
    shellyReportedOn = outp;
    if (fumeManual && !outp) {
      // Manual mode: switched off externally -> firmware enforces ON.
      // (Manual mode can only be left via a long button press.)
      Serial.println("  [shelly] switched OFF externally in manual mode -> ON again");
      shellySet(true);
    } else if (!fumeManual && outp != shellyDesiredOn) {
      // External change (app/button) -> adopt as desired state
      Serial.printf("  [shelly] changed externally, adopting state: %s\n",
                    outp ? "ON" : "OFF");
      shellyDesiredOn = outp;
    }
  }
  jsonFloat(body, "apower", shellyPowerW);

  if (shellyReportedOn && shellyPowerW < LOW_POWER_WARN_W) {
    Serial.printf("  [WARNING] Relay ON, but only %.1f W — is the extractor running?\n",
                  shellyPowerW);
  }
}

// Extractor logic: channel activity -> relay, with run-on delay.
// In manual mode the automatic is suspended; the periodic validation
// keeps running (and holds the relay ON there).
void extractorLogic() {
  unsigned long now = millis();
  if (!fumeManual) {
    if (anyChannelOn) {
      lastAnyChannelOn = now;
      if (!shellyDesiredOn) shellySet(true);
    } else if (shellyDesiredOn &&
               now - lastAnyChannelOn > EXTRACTOR_OFF_DELAY_MS) {
      shellySet(false);
    }
  }

  if (now - lastValidate > SHELLY_VALIDATE_MS) {
    lastValidate = now;
    shellyValidate();
  }
}

// ================= Push button =================
// The driver (buttonPoll) only detects events and sets flags —
// execution (buttonService, does HTTP) runs outside the WX2
// transactions, analogous to the I2C rule for oled.display().
//   short (< 800ms, on release): cancel run-on -> OFF immediately
//   long (>= 800ms, fires while held): toggle manual mode

static bool     btnStable   = true;   // debounced level, HIGH = released
static bool     btnLastRaw  = true;
static uint32_t btnEdgeMs   = 0;
static uint32_t btnDownMs   = 0;
static bool     btnLongDone = false;
static bool     btnEvtShort = false;  // pending events
static bool     btnEvtLong  = false;

void buttonInit() {
  pinMode(BTN_PIN, INPUT_PULLUP);     // button to GND
}

void buttonPoll() {
  bool raw = digitalRead(BTN_PIN);
  uint32_t now = millis();

  if (raw != btnLastRaw) { btnEdgeMs = now; btnLastRaw = raw; }
  if (now - btnEdgeMs < BTN_DEBOUNCE_MS) return;

  if (raw != btnStable) {
    btnStable = raw;
    if (!btnStable) {                          // press edge
      btnDownMs   = now;
      btnLongDone = false;
    } else if (!btnLongDone &&
               now - btnDownMs < BTN_LONGPRESS_MS) {
      btnEvtShort = true;                      // released before threshold
    }
  } else if (!btnStable && !btnLongDone &&
             now - btnDownMs >= BTN_LONGPRESS_MS) {
    btnLongDone = true;                        // fires once, while held
    btnEvtLong = true;
  }
}

// Executes pending button events. true = state changed
// (caller can re-render immediately).
bool buttonService() {
  bool acted = false;

  if (btnEvtShort) {
    btnEvtShort = false;
    // Only meaningful during run-on: channels off, relay (desired) still ON
    if (!fumeManual && shellyDesiredOn && !anyChannelOn) {
      Serial.println("[btn] short: run-on cancelled -> OFF");
      shellySet(false);
      acted = true;
    }
  }

  if (btnEvtLong) {
    btnEvtLong = false;
    fumeManual = !fumeManual;
    if (fumeManual) {
      Serial.println("[btn] long: manual mode ON (continuous run)");
      shellySet(true);
    } else {
      Serial.println("[btn] long: manual mode OFF -> automatic");
      // Follow the actual channel state immediately, no run-on restart
      shellySet(anyChannelOn);
    }
    acted = true;
  }

  return acted;
}

// ================= OLED HMI =================

static uint8_t wifiBars() {
  if (WiFi.status() != WL_CONNECTED) return 0;
  int r = WiFi.RSSI();
  return r > -55 ? 4 : r > -65 ? 3 : r > -75 ? 2 : 1;
}

// Text width at the current text size (Adafruit GFX)
static uint16_t textW(const char* t) {
  int16_t x1, y1; uint16_t w, h;
  oled.getTextBounds(t, 0, 0, &x1, &y1, &w, &h);
  return w;
}

// Centered around column cx; y = TOP edge of the text (GFX convention)
static void oledCentered(int cx, int y, const char* t) {
  oled.setCursor(cx - textW(t) / 2, y);
  oled.print(t);
}

// Right-aligned, right edge at xr; y = top edge
static void oledRight(int xr, int y, const char* t) {
  oled.setCursor(xr - textW(t), y);
  oled.print(t);
}

// Short form of the channel state so it fits into the 64px half
// at text size 2 (the full word would be too wide).
const char* statusShort(char code) {
  switch (code) {
    case '0': return "OFF";
    case '1': return "ON";
    case '2': return "STBY";     // STANDBY
    case '3': return "AUTO";     // AUTO-OFF
    default:  return "?";
  }
}

// Background per channel:
//   ON            -> white (inverted)
//   STANDBY/AUTO  -> blinking (alternating black/white)
//   OFF/unknown   -> black
static bool chInverted(char code, bool blink) {
  switch (code) {
    case '1': return true;      // ON
    case '2':                   // STANDBY
    case '3': return blink;     // AUTO-OFF
    default:  return false;     // OFF / '?'
  }
}

// 3-band layout: top = status, middle = channels, bottom = extractor.
// Called once per poll cycle at the end of loop() (after the serial
// traffic), so the I2C flush never lands in the middle of a WX2
// transaction.
// GFX coordinates: setCursor(x, y) => y is the TOP edge of the text.
void oledRender() {
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1);                          // default font 6x8

  // ---- Top band: status ----
  oled.setCursor(0, 1);  oled.print("WX2");
  wx2Connected    ? oled.fillRect(20, 1, 7, 7, SSD1306_WHITE)
                  : oled.drawRect(20, 1, 7, 7, SSD1306_WHITE);
  oled.setCursor(33, 1); oled.print("PLUG");
  shellyConnected ? oled.fillRect(58, 1, 7, 7, SSD1306_WHITE)
                  : oled.drawRect(58, 1, 7, 7, SSD1306_WHITE);
  uint8_t bars = wifiBars();                    // WiFi bars, bottom-aligned
  for (int i = 0; i < 4; i++) {
    int h = 2 + i * 2, x = 111 + i * 4, y = 9 - h;
    (i < bars) ? oled.fillRect(x, y, 3, h, SSD1306_WHITE)
               : oled.drawRect(x, y, 3, h, SSD1306_WHITE);
  }
  oled.drawFastHLine(0, 11, 128, SSD1306_WHITE);

  // ---- Middle band: channels (number + state, large) ----
  // Blink phase: advanced once per poll cycle in loop(), so extra
  // re-renders after button actions do not disturb the rhythm.
  bool blink = blinkPhase;
  bool ch1Inv = chInverted(chStatus[0], blink);
  bool ch2Inv = chInverted(chStatus[1], blink);
  if (ch1Inv) oled.fillRect(0,  12, 64, 39, SSD1306_WHITE);   // left half
  if (ch2Inv) oled.fillRect(65, 12, 63, 39, SSD1306_WHITE);   // right half
  // Divider is black when both halves are white, otherwise white.
  oled.drawFastVLine(64, 13, 38,
                     (ch1Inv && ch2Inv) ? SSD1306_BLACK : SSD1306_WHITE);

  oled.setTextSize(2);
  oled.setTextColor(ch1Inv ? SSD1306_BLACK : SSD1306_WHITE);
  oledCentered(32, 15, "CH1");
  oledCentered(32, 34, statusShort(chStatus[0]));
  oled.setTextColor(ch2Inv ? SSD1306_BLACK : SSD1306_WHITE);
  oledCentered(96, 15, "CH2");
  oledCentered(96, 34, statusShort(chStatus[1]));
  oled.setTextColor(SSD1306_WHITE);             // reset color
  oled.setTextSize(1);

  // Divider between channel band and extractor band: its color is the
  // opposite of the extractor background (white stays visible).
  bool lowPower = shellyReportedOn && shellyPowerW < LOW_POWER_WARN_W;
  bool fumeInv  = shellyReportedOn && (lowPower ? blink : true);
  oled.drawFastHLine(0, 51, 128, fumeInv ? SSD1306_BLACK : SSD1306_WHITE);

  // Run-on: channels off, relay (desired) still ON -> the white bar
  // shrinks from right to left in proportion to the remaining time.
  // Manual mode has no run-on -> the bar stays full.
  bool runOn = !fumeManual && shellyDesiredOn && !anyChannelOn;
  int fumeW = 128;                              // full width by default
  if (runOn) {
    unsigned long elapsed = millis() - lastAnyChannelOn;
    long remain = (long)EXTRACTOR_OFF_DELAY_MS - (long)elapsed;
    if (remain < 0) remain = 0;
    fumeW = (int)((remain * 128L) / (long)EXTRACTOR_OFF_DELAY_MS);
  }

  if (fumeInv && fumeW > 0) oled.fillRect(0, 52, fumeW, 12, SSD1306_WHITE);
  // INVERSE: glyph pixels flip the background - the text stays readable
  // even when the bar edge runs right through the label.
  oled.setTextColor(SSD1306_INVERSE);
  char fumeBuf[20];
  snprintf(fumeBuf, sizeof fumeBuf, "FUME-X  %s",
           shellyReportedOn ? "ON" : "OFF");
  oledCentered(64, 54, fumeBuf);

  // MAN chip (variant C): 23x11 at x=103, polarity opposite to the
  // band background; does not blink by itself, but flips along with
  // the band during the low-power warning.
  if (fumeManual) {
    uint16_t chipBg = fumeInv ? SSD1306_BLACK : SSD1306_WHITE;
    uint16_t chipFg = fumeInv ? SSD1306_WHITE : SSD1306_BLACK;
    oled.fillRect(103, 52, 23, 11, chipBg);
    oled.setTextColor(chipFg);
    oled.setCursor(106, 54);
    oled.print("MAN");
  }
  oled.setTextColor(SSD1306_WHITE);             // reset color

  oled.display();
}

// ================= Setup / Loop =================

void setup() {
  Serial.begin(115200);
  wx.begin(1200, SERIAL_8N1, WX_RX, WX_TX);
  buttonInit();

  // OLED (default I2C on the XIAO: SDA=D4/GPIO6, SCL=D5/GPIO7)
  if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("  [oled] SSD1306 not found (check address/wiring)");
  }
  wx2_splash(oled, BTN_PIN);  // Animated splash (short press to skip)

  // Immediate visual feedback that splash is done and app is starting
  oled.clearDisplay();
  oled.drawRect(0, 0, 128, 64, SSD1306_WHITE);
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(10, 28);
  oled.print("Connecting...");
  oled.display();

  Serial.println("\n=== WX2 Hello World + Shelly ===");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting to WiFi");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(WiFi.status() == WL_CONNECTED
                 ? String(" OK, IP: ") + WiFi.localIP().toString()
                 : " FAILED (continuing without Shelly)");

  String f[2];
  Serial.println("Enabling remote mode...");
  int n = wxCommand("remote1", f, 1);
  Serial.printf("  remote1 -> %s\n", n ? f[0].c_str() : "(none)");

  if (wxCommand("?", f, 1)) {
    char id = f[0][2] != '0' ? f[0][2] : f[0][5];
    const char* model =
      id == '1' ? "WX 1"  : id == '2' ? "WX 2"  :
      id == '3' ? "WX 2D" : id == '4' ? "WX 2A" :
      id == '5' ? "WX 1D" : id == '6' ? "WX 1A" : "unknown";
    Serial.printf("Station model: %s (raw %s)\n", model, f[0].c_str());
  }

  // Take over the actual Shelly state as the starting point
  shellyValidate();
  shellyDesiredOn = shellyReportedOn;
  lastAnyChannelOn = millis();
}

void loop() {
  Serial.println("--------------------------");
  String f[2];

  // Channel status
  if (wxCommand("Q", f, 1)) {
    char s1 = f[0][2], s2 = f[0][3];
    anyChannelOn = (s1 == '1') || (s2 == '1');
    chStatus[0] = s1; chStatus[1] = s2;         // keep for the OLED
    wx2Connected = true;
    Serial.printf("CH1: %-8s  CH2: %-8s\n", statusName(s1), statusName(s2));
  } else {
    wx2Connected = false;                        // no valid response
  }

  // Actual temperatures
  float t[2] = {NAN, NAN};
  int n = wxCommand("R", f, 2);
  for (int i = 0; i < n; i++) t[frameChannel(f[i])] = frameValue(f[i]) / 10.0f;
  chActual[0] = t[0]; chActual[1] = t[1];        // keep for the OLED
  if (n) Serial.printf("Actual temp  CH1: %.1f C   CH2: %.1f C\n", t[0], t[1]);

  // Set temperatures
  t[0] = t[1] = NAN;
  n = wxCommand("S", f, 2);
  for (int i = 0; i < n; i++) t[frameChannel(f[i])] = frameValue(f[i]) / 10.0f;
  if (n) Serial.printf("Set temp     CH1: %.1f C   CH2: %.1f C\n", t[0], t[1]);

  // Tools
  int tool[2] = {-1, -1};
  n = wxCommand("Y", f, 2);
  for (int i = 0; i < n; i++) tool[frameChannel(f[i])] = frameValue(f[i]);
  if (n) Serial.printf("Tools        CH1: %s   CH2: %s\n",
                       toolName(tool[0]), toolName(tool[1]));

  // Execute button events captured during the serial window now
  buttonPoll();
  buttonService();

  // Extractor
  extractorLogic();
  Serial.printf("Extractor: desired=%s  actual=%s  %.1f W  %s\n",
                shellyDesiredOn ? "ON" : "OFF",
                shellyReportedOn ? "ON" : "OFF",
                shellyPowerW,
                fumeManual ? "[MANUAL]" : "");

  // Update the OLED (after this cycle's serial traffic)
  blinkPhase = !blinkPhase;
  oledRender();

  // Poll pause: the button stays responsive, actions take effect
  // immediately (incl. re-render, so the MAN chip appears without delay).
  unsigned long t0 = millis();
  while (millis() - t0 < 1000) {
    buttonPoll();
    if (buttonService()) oledRender();
    delay(5);
  }
}
