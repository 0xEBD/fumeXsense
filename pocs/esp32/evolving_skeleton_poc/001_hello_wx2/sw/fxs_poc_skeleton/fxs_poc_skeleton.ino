/*
 * WX2 Hello World — XIAO ESP32-C3 <-> Weller WX2 + Shelly Plug S Gen3
 *
 * Reads the WX2 status via RS232 and controls the fume extractor via
 * a Shelly Plug S Gen3 (HTTP RPC API):
 *   - at least one channel ON -> extractor ON
 *   - no channel ON           -> extractor OFF after run-on delay
 *   - the desired state is periodically validated against the actual
 *     Shelly state (external changes, e.g. button on the plug)
 *   - warning on low power draw despite relay ON
 *
 * !! HARDWARE WARNING !!
 * WX2 RJ12 = true RS232 (+/-12V) -> MAX3232 is mandatory.
 *
 * Wiring (XIAO ESP32-C3, UART1):
 *   D6 / GPIO21 (TX) -> MAX3232 -> WX2 RX
 *   D7 / GPIO20 (RX) <- MAX3232 <- WX2 TX
 *   Common GND.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "secrets.h"  // copy secrets.h.example -> secrets.h

// ---------- Configuration ----------
const char* WIFI_SSID = SECRET_WIFI_SSID;
const char* WIFI_PASS = SECRET_WIFI_PASS;
const char* SHELLY_IP = SECRET_SHELLY_IP;       // Shelly Plug S Gen3

const unsigned long EXTRACTOR_OFF_DELAY_MS = 30000;  // run-on delay
const unsigned long SHELLY_VALIDATE_MS     = 10000;  // re-check state
const float LOW_POWER_WARN_W               = 5.0f;   // warning threshold

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
    delay(2);
  }
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
    shellyDesiredOn = on;
    Serial.printf("Extractor -> %s\n", on ? "ON" : "OFF");
  }
}

// Fetch actual state + power from the Shelly and sync the desired state
void shellyValidate() {
  String body = shellyRpc("Switch.GetStatus?id=0");
  if (!body.length()) return;

  bool outp;
  if (jsonBool(body, "output", outp)) {
    shellyReportedOn = outp;
    if (outp != shellyDesiredOn) {
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

// Extractor logic: channel activity -> relay, with run-on delay
void extractorLogic() {
  unsigned long now = millis();
  if (anyChannelOn) {
    lastAnyChannelOn = now;
    if (!shellyDesiredOn) shellySet(true);
  } else if (shellyDesiredOn &&
             now - lastAnyChannelOn > EXTRACTOR_OFF_DELAY_MS) {
    shellySet(false);
  }

  if (now - lastValidate > SHELLY_VALIDATE_MS) {
    lastValidate = now;
    shellyValidate();
  }
}

// ================= Setup / Loop =================

void setup() {
  Serial.begin(115200);
  wx.begin(1200, SERIAL_8N1, WX_RX, WX_TX);
  delay(500);

  Serial.println("\n=== WX2 Hello World + Shelly ===");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting WiFi");
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

  // Use the Shelly's actual state as starting point
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
    Serial.printf("CH1: %-8s  CH2: %-8s\n", statusName(s1), statusName(s2));
  }

  // Actual temperatures
  float t[2] = {NAN, NAN};
  int n = wxCommand("R", f, 2);
  for (int i = 0; i < n; i++) t[frameChannel(f[i])] = frameValue(f[i]) / 10.0f;
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

  // Extractor
  extractorLogic();
  Serial.printf("Extractor: desired=%s  actual=%s  %.1f W\n",
                shellyDesiredOn ? "ON" : "OFF",
                shellyReportedOn ? "ON" : "OFF",
                shellyPowerW);

  delay(1000);
}
