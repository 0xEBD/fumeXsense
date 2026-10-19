/*
 * wx2_splash.h — Animated splash screen for FUME-X Sense WX2
 *
 * Sequence (128×64 SSD1306, Adafruit_GFX default font):
 *   0ms    White border frame drawn
 *   0ms    "FUME-X" typewriter reveal, 250ms/char, double-drawn bold
 *   1750ms "Sense" small font, right of hero
 *   2350ms "W" above / "2" below the X glyph (reads W/X/2), blink 2×300ms
 *   3850ms "SMART" badge top-right (no blink, just appear)
 *   4450ms "v1.3 - by 0xEBD" bottom-right
 *
 * Call wx2_splash(oled) once in setup() after oled.begin().
 * Requires: Adafruit_SSD1306 oled instance (128×64).
 */

#ifndef WX2_SPLASH_H
#define WX2_SPLASH_H

#include <Adafruit_SSD1306.h>

// --------------- internal helpers ---------------

// Text width at current size (mirrors textW in main sketch)
static uint16_t _splTextW(Adafruit_SSD1306& d, const char* t) {
  int16_t x1, y1; uint16_t w, h;
  d.getTextBounds(t, 0, 0, &x1, &y1, &w, &h);
  return w;
}

// Print text centered horizontally at baseline y (GFX y = top of glyph).
// For default font size 1: glyph height = 8, so baseline y means top = y-8+1.
// We use GFX convention: y passed here is the setCursor y (top of glyph).
static void _splCentered(Adafruit_SSD1306& d, int y, const char* t) {
  uint16_t w = _splTextW(d, t);
  d.setCursor((128 - w) / 2, y);
  d.print(t);
}

// Print bold (double-draw with 1px offset) centered at y
static void _splCenteredBold(Adafruit_SSD1306& d, int y, const char* t) {
  uint16_t w = _splTextW(d, t);
  int x = (128 - w) / 2;
  d.setCursor(x, y);
  d.print(t);
  d.setCursor(x + 1, y);  // 1px right for bold effect
  d.print(t);
}

// Interruptible delay: returns true if button was pressed (splash should exit).
// Button is INPUT_PULLUP, so LOW = pressed.
// Uses a static latch: once ANY LOW is read the press is permanently recorded
// and all subsequent calls return immediately. No debounce confirmation is
// needed because a false-positive (noise spike) only skips the splash — a
// harmless outcome — while requiring confirmation was unreliable with bouncy
// switches.
static bool _splBtnPressed = false;  // latched across all _splDelay calls

static bool _splDelay(int btnPin, unsigned long ms) {
  if (btnPin < 0) { delay(ms); return false; }
  if (_splBtnPressed) return true;  // already latched from earlier

  unsigned long start = millis();
  while (millis() - start < ms) {
    if (digitalRead(btnPin) == LOW) {
      _splBtnPressed = true;
      return true;
    }
    delay(1);
  }
  return false;
}

// --------------- splash entry point ---------------
// btnPin: GPIO with INPUT_PULLUP button (LOW = pressed). Short press skips splash.

static void wx2_splash(Adafruit_SSD1306& oled, int btnPin = -1) {
  _splBtnPressed = false;  // reset latch for this run
  // Adafruit_GFX default font metrics at size 2: char width ~12px, height 16px.
  // At size 1: char width 6px, height 8px.

  const char* hero = "FUME-X";
  const int heroLen = 6;
  const int heroSize = 2;             // textSize for hero
  const int heroCharW = 12;           // 6px base * size 2
  const int heroH = 16;              // 8px base * size 2
  // Hero baseline y=40 in spec. GFX setCursor y = top of glyph.
  // At size 2, glyph height = 16. Baseline ≈ top + 16. So top = 40 - 16 = 24.
  const int heroY = 24;

  // Total hero width including "Sense" suffix (size 1 = 6px/char, 5 chars = 30px + 2px gap)
  const int heroW = heroLen * heroCharW;
  const int sensePixW = 5 * 6;  // "Sense" at size 1
  const int comboW = heroW + 2 + sensePixW;  // hero + gap + sense
  const int heroX = (128 - comboW) / 2;

  // --- Draw white border frame around entire display ---
  oled.clearDisplay();
  oled.drawRect(0, 0, 128, 64, SSD1306_WHITE);
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(heroSize);

  // --- Step 1: Typewriter reveal "FUME-X" (250ms/char for slower pace) ---
  for (int i = 0; i < heroLen; i++) {
    int cx = heroX + i * heroCharW;
    // Double-draw for bold
    oled.setCursor(cx, heroY);
    oled.write(hero[i]);
    oled.setCursor(cx + 1, heroY);
    oled.write(hero[i]);
    oled.display();
    if (_splDelay(btnPin, 250)) return;
  }

  // --- Step 2: "Sense" tag ---
  if (_splDelay(btnPin, 250)) return;

  // "Sense" in small font, positioned right after hero text
  oled.setTextSize(1);
  // Place "Sense" just to the right of "FUME-X", vertically aligned near bottom
  // of hero. Hero bottom = heroY + heroH = 40. "Sense" top = heroY + heroH - 8 = 32
  int senseX = heroX + heroW + 2;
  int senseY = heroY + heroH - 8;  // align bottom of Sense with bottom of hero
  oled.setCursor(senseX, senseY);
  oled.print("Sense");
  oled.display();
  if (_splDelay(btnPin, 400)) return;

  // --- Step 3: "W" above X / "2" below X ---
  if (_splDelay(btnPin, 200)) return;

  // Find center of the 'X' glyph in "FUME-X".
  // 'X' is the 6th character (index 5). At size 2, each char is 12px wide.
  int xGlyphCenterX = heroX + 5 * heroCharW + heroCharW / 2;

  // "W" above the X glyph
  oled.setTextSize(1);
  int wW = _splTextW(oled, "W");
  int wX = xGlyphCenterX - wW / 2;
  int wY = 14;  // top of "W" glyph, placing it above "FUME-X"

  // "2" below the X glyph
  int twoW = _splTextW(oled, "2");
  int twoX = xGlyphCenterX - twoW / 2;
  int twoY = 42;  // below the hero line

  // Blink pattern: 2× (300ms on, 300ms off), then on.
  for (int i = 0; i < 2; i++) {
    // ON
    oled.setCursor(wX, wY);
    oled.setTextColor(SSD1306_WHITE);
    oled.print("W");
    oled.setCursor(twoX, twoY);
    oled.print("2");
    oled.display();
    if (_splDelay(btnPin, 300)) return;
    // OFF
    oled.setCursor(wX, wY);
    oled.setTextColor(SSD1306_BLACK);
    oled.print("W");
    oled.setCursor(twoX, twoY);
    oled.print("2");
    oled.display();
    if (_splDelay(btnPin, 300)) return;
  }
  // Settle ON
  oled.setCursor(wX, wY);
  oled.setTextColor(SSD1306_WHITE);
  oled.print("W");
  oled.setCursor(twoX, twoY);
  oled.print("2");
  oled.display();

  // --- Step 4: "SMART" badge (no blink, just appear) ---
  if (_splDelay(btnPin, 400)) return;

  const char* smart = "SMART";
  oled.setTextSize(1);
  int smartW = _splTextW(oled, smart);
  int chipW = smartW + 4;  // 2px padding each side
  int chipH = 9;
  int chipX = 128 - chipW - 2;  // top-right with margin inside border
  int chipY = 2;

  oled.fillRoundRect(chipX, chipY, chipW, chipH, 2, SSD1306_WHITE);
  oled.setTextColor(SSD1306_BLACK);
  oled.setCursor(chipX + 2, chipY + 1);
  oled.print(smart);
  oled.display();

  // --- Step 5: "v1.3 - by 0xEBD" (separate from SMART) ---
  if (_splDelay(btnPin, 600)) return;

  const char* credit = "v1.3 - by 0xEBD";
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  int creditW = _splTextW(oled, credit);
  int creditX = 128 - creditW - 3;  // inside border
  int creditY = 54;  // bottom-right area inside border
  oled.setCursor(creditX, creditY);
  oled.print(credit);
  oled.display();

  // Hold final frame before main UI takes over
  _splDelay(btnPin, 1000);
}

#endif // WX2_SPLASH_H
