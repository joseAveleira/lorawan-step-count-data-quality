#include "ui.h"

#include <SSD1306Wire.h>

#include "config.h"

namespace {

constexpr uint32_t kAutoOnMs = 30000;

SSD1306Wire display(0x3C, PIN_OLED_SDA, PIN_OLED_SCL, GEOMETRY_128_64);
bool ok = false;
bool on = false;
uint8_t mode = DEF_DISPLAY;
uint8_t page = 0;  // 0 = vista principal, 1 = vista técnica
uint32_t onUntil = 0;
uint32_t lastDraw = 0;

void setPower(bool v) {
  if (!ok || v == on) return;
  on = v;
  if (v) display.displayOn();
  else display.displayOff();
}

bool lit() { return on && (mode == 2 || (int32_t)(millis() - onUntil) <= 0); }

void applyFlip(bool flip) {
  display.resetOrientation();
  if (flip) display.flipScreenVertically();
}

// Barras de cobertura (15 x 9 px): rellenas con red, huecas sin ella
void drawBars(int16_t x, int16_t y, bool full) {
  for (int16_t i = 0; i < 4; i++) {
    const int16_t h = 3 + 2 * i;
    const int16_t bx = x + 4 * i;
    const int16_t by = y + 9 - h;
    if (full) display.fillRect(bx, by, 3, h);
    else display.drawRect(bx, by, 3, h);
  }
}

// Flecha hacia arriba (7 x 9 px): última subida de datos
void drawUpArrow(int16_t x, int16_t y) {
  display.drawLine(x + 3, y, x + 3, y + 8);
  display.drawLine(x + 3, y, x, y + 3);
  display.drawLine(x + 3, y, x + 6, y + 3);
}

// 12345 -> "12 345" (espacio como separador de miles)
void groupThousands(uint32_t v, char* out, size_t n) {
  char raw[12];
  snprintf(raw, sizeof raw, "%lu", (unsigned long)v);
  const size_t len = strlen(raw);
  size_t j = 0;
  for (size_t i = 0; i < len && j + 2 < n; i++) {
    if (i > 0 && (len - i) % 3 == 0) out[j++] = ' ';
    out[j++] = raw[i];
  }
  out[j] = '\0';
}

void formatAgo(uint32_t s, char* out, size_t n) {
  if (s < 60) snprintf(out, n, "now");
  else if (s < 3600) snprintf(out, n, "%lu min", (unsigned long)(s / 60));
  else if (s < 86400) snprintf(out, n, "%lu h", (unsigned long)(s / 3600));
  else snprintf(out, n, "%lu d", (unsigned long)(s / 86400));
}

// Vista principal. Alturas medidas en las fuentes: en ArialMT_Plain_10 las cifras ocupan las filas 3 a 9 del
// cuadro y en ArialMT_Plain_24 las filas 5 a 21, así que "steps" (y + 32) comparte línea base con el número (y + 20).
void drawMain(const UiStatus& s) {
  bool net = false;
  const char* label = "Checking";
  if (!s.joined) {
    label = "Not joined";
  } else if (!s.checkOn) {
    net = true;
    label = "LoRaWAN";
  } else if (s.cov == 1) {
    net = true;
    label = "In coverage";
  } else if (s.cov == 2) {
    label = "No coverage";
  }
  drawBars(0, 1, net);
  display.setFont(ArialMT_Plain_10);
  display.setTextAlignment(TEXT_ALIGN_LEFT);
  display.drawString(19, 0, label);
  if (s.lastTx) {
    char ago[12];
    formatAgo(s.lastTxAgoS, ago, sizeof ago);
    display.setTextAlignment(TEXT_ALIGN_RIGHT);
    display.drawString(127, 0, ago);
    drawUpArrow(127 - display.getStringWidth(ago) - 10, 1);
  }
  display.drawHorizontalLine(0, 13, 128);

  char num[16];
  groupThousands(s.cumulative, num, sizeof num);
  display.setTextAlignment(TEXT_ALIGN_LEFT);
  display.setFont(ArialMT_Plain_24);
  const int16_t wNum = display.getStringWidth(num);
  display.setFont(ArialMT_Plain_10);
  const int16_t wLbl = display.getStringWidth("steps");
  if (wNum + 5 + wLbl <= 128) {
    const int16_t x0 = (128 - (wNum + 5 + wLbl)) / 2;
    display.setFont(ArialMT_Plain_24);
    display.drawString(x0, 20, num);
    display.setFont(ArialMT_Plain_10);
    display.drawString(x0 + wNum + 5, 32, "steps");
  } else {
    display.setTextAlignment(TEXT_ALIGN_CENTER);
    display.setFont(ArialMT_Plain_24);
    display.drawString(64, 15, num);
    display.setFont(ArialMT_Plain_10);
    display.drawString(64, 39, "steps");
  }

  char l[32];
  if (s.pending == 0) snprintf(l, sizeof l, "All data sent");
  else snprintf(l, sizeof l, "%u interval%s pending", s.pending, s.pending == 1 ? "" : "s");
  display.setFont(ArialMT_Plain_10);
  display.setTextAlignment(TEXT_ALIGN_CENTER);
  display.drawString(64, 51, l);
  display.setTextAlignment(TEXT_ALIGN_LEFT);
}

// Vista técnica (la pantalla de las versiones anteriores)
void drawTech(const UiStatus& s) {
  char l[40];
  display.setFont(ArialMT_Plain_10);
  display.setTextAlignment(TEXT_ALIGN_LEFT);
  snprintf(l, sizeof l, "S%u  C%u  ep %u  %s", s.strategy, s.scenario, s.epoch, s.sensor ? "BNO085" : "sintetico");
  display.drawString(0, 0, l);
  // "unido" despistaba: solo dice que hay sesión, no que algún gateway lo oiga. Con "check" se sabe de verdad.
  const unsigned long agoMin = s.replyAgoS == UINT32_MAX ? 0 : (unsigned long)(s.replyAgoS / 60);
  if (!s.checkOn) {
    snprintf(l, sizeof l, "Red: %s  fcnt %lu", s.joined ? "registrado" : "sin registrar", (unsigned long)s.fcnt);
  } else if (s.cov == 1 && s.gw) {
    snprintf(l, sizeof l, "Cobertura: si hace %lum (%u gw)", agoMin, s.gw);
    if (display.getStringWidth(String(l)) > 128) snprintf(l, sizeof l, "Cobertura: si %lum %ugw", agoMin, s.gw);
  } else if (s.cov == 1) {
    snprintf(l, sizeof l, "Cobertura: si hace %lum", agoMin);
  } else if (s.cov == 2 && s.replyAgoS != UINT32_MAX) {
    snprintf(l, sizeof l, "SIN cobertura hace %lum", agoMin);
  } else if (s.cov == 2) {
    snprintf(l, sizeof l, "SIN cobertura");
  } else {
    snprintf(l, sizeof l, "Cobertura: comprobando");
  }
  display.drawString(0, 13, l);
  snprintf(l, sizeof l, "Bin %u   pendientes %u", s.openSeq, s.pending);
  display.drawString(0, 26, l);
  snprintf(l, sizeof l, "Pasos %lu  %s", (unsigned long)s.cumulative, s.gps ? s.gps : "");
  display.drawString(0, 39, l);
  if (s.lastTx) snprintf(l, sizeof l, "TX %s hace %lu s", s.lastTx, (unsigned long)s.lastTxAgoS);
  else snprintf(l, sizeof l, "Sin tramas todavia");
  display.drawString(0, 52, l);
}

}  // namespace

void uiBegin(uint8_t m, bool flip) {
  mode = m;
  pinMode(PIN_OLED_RST, OUTPUT);
  digitalWrite(PIN_OLED_RST, LOW);
  delay(20);
  digitalWrite(PIN_OLED_RST, HIGH);
  delay(20);
  ok = display.init();
  if (!ok) return;
  applyFlip(flip);
  display.setFont(ArialMT_Plain_10);
  on = true;
  if (mode == 0) setPower(false);
  else uiWake();
}

void uiSetMode(uint8_t m) {
  mode = m;
  if (m == 0) setPower(false);
  else uiWake();
}

void uiSetFlip(bool flip) {
  if (!ok) return;
  applyFlip(flip);
  lastDraw = 0;  // redibuja en la siguiente actualización
}

void uiWake() {
  if (mode == 0) return;
  onUntil = millis() + kAutoOnMs;
  lastDraw = 0;
  setPower(true);
}

void uiButton() {
  if (lit()) page ^= 1;
  else page = 0;
  uiWake();
}

void uiMessage(const char* title, const char* line) {
  if (!ok) return;
  setPower(true);
  display.clear();
  display.setTextAlignment(TEXT_ALIGN_CENTER);
  display.setFont(ArialMT_Plain_16);
  display.drawString(64, 14, title);
  if (line) {
    display.setFont(ArialMT_Plain_10);
    display.drawString(64, 38, line);
  }
  display.setTextAlignment(TEXT_ALIGN_LEFT);
  display.setFont(ArialMT_Plain_10);
  display.display();
}

void uiUpdate(const UiStatus& s) {
  if (!ok) return;
  if (mode == 0 || (mode == 1 && (int32_t)(millis() - onUntil) > 0)) {
    setPower(false);
    return;
  }
  if (lastDraw && millis() - lastDraw < 1000) return;
  lastDraw = millis();
  setPower(true);
  display.clear();
  if (page == 0) drawMain(s);
  else drawTech(s);
  display.display();
}

void uiSleep() {
  if (!ok) return;
  display.clear();
  display.display();
  setPower(false);
}
