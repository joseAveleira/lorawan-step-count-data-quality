#include "power.h"

#include <Arduino.h>
#include <driver/gpio.h>
#include <driver/rtc_io.h>
#include <esp_attr.h>
#include <esp_sleep.h>

#include "config.h"

#define OFF_MAGIC 0x4F46464EUL  // "OFFN": el nodo está apagado con el botón

namespace {

constexpr uint32_t kShortMinMs = 50;
constexpr uint32_t kShortMaxMs = 1000;
constexpr uint64_t kStuckRetryUs = 30ULL * 1000000ULL;

RTC_NOINIT_ATTR uint32_t s_offMagic;
volatile bool s_offRequested = false;
volatile bool s_shortPress = false;
bool s_wokeByButton = false;

// Líneas que se mantienen fijas durante el sueño profundo:
//   NSS de la radio en alto: un flanco de bajada despertaría al SX1262
//   TX hacia el GPS en alto (reposo de la UART): cualquier dato despertaría al GPS
//   Vext en alto: OLED y pin Ve sin alimentación
const gpio_num_t kHeld[] = {(gpio_num_t)PIN_LORA_NSS, (gpio_num_t)PIN_GPS_TX, (gpio_num_t)PIN_VEXT};

void releaseHolds() {
  for (gpio_num_t p : kHeld) gpio_hold_dis(p);
}

bool buttonDown() { return digitalRead(PIN_BUTTON) == LOW; }

// Duerme hasta una pulsación del botón o, si la línea está bloqueada en bajo, hasta un temporizador
// (con el botón como fuente de despertar se despertaría en el acto).
[[noreturn]] void sleepNow() {
  const bool stuck = buttonDown();
  for (gpio_num_t p : kHeld) {
    pinMode(p, OUTPUT);
    digitalWrite(p, HIGH);
    gpio_hold_en(p);
  }
  if (stuck) {
    esp_sleep_enable_timer_wakeup(kStuckRetryUs);
  } else {
    esp_sleep_enable_ext0_wakeup((gpio_num_t)PIN_BUTTON, 0);
    rtc_gpio_pullup_en((gpio_num_t)PIN_BUTTON);
    rtc_gpio_pulldown_dis((gpio_num_t)PIN_BUTTON);
  }
  s_offMagic = OFF_MAGIC;
  Serial.flush();
  esp_deep_sleep_start();
}

void buttonTask(void*) {
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  bool armed = !buttonDown();  // si arranca pulsado (o con la línea bloqueada), se espera a que se suelte
  bool down = false;
  bool ledOn = false;
  uint32_t t0 = 0;
  for (;;) {
    const bool low = buttonDown();
    const uint32_t now = millis();
    if (!armed) {
      armed = !low;
    } else if (low && !down) {
      down = true;
      t0 = now;
    } else if (low) {
      const uint32_t held = now - t0;
      const bool led = held >= POWER_HOLD_MS && held < POWER_STUCK_MS;  // soltar ahora = apagar
      if (led != ledOn) {
        ledOn = led;
        digitalWrite(PIN_LED, led ? HIGH : LOW);
      }
    } else if (down) {
      down = false;
      const uint32_t held = now - t0;
      if (ledOn) {
        ledOn = false;
        digitalWrite(PIN_LED, LOW);
      }
      if (held >= kShortMinMs && held < kShortMaxMs) s_shortPress = true;
      if (held >= POWER_HOLD_MS && held < POWER_STUCK_MS) s_offRequested = true;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

}  // namespace

void ledBlink(uint8_t times, uint16_t onMs, uint16_t offMs) {
  pinMode(PIN_LED, OUTPUT);
  for (uint8_t i = 0; i < times; i++) {
    digitalWrite(PIN_LED, HIGH);
    delay(onMs);
    digitalWrite(PIN_LED, LOW);
    if (i + 1 < times) delay(offMs);
  }
}

void powerBootCheck() {
  const esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  const bool fromOff =
      (cause == ESP_SLEEP_WAKEUP_EXT0 || cause == ESP_SLEEP_WAKEUP_TIMER) && s_offMagic == OFF_MAGIC;
  if (!fromOff) {
    s_offMagic = 0;  // arranque normal (alimentación, RST o reinicio por software)
    return;
  }
  rtc_gpio_deinit((gpio_num_t)PIN_BUTTON);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  // Despertar por el temporizador: la línea estaba bloqueada; se vuelve a dormir sin más
  if (cause == ESP_SLEEP_WAKEUP_TIMER || !buttonDown()) sleepNow();
  // millis() cuenta desde el despertar (más el arranque, unos 0,2 s)
  while (buttonDown() && millis() < POWER_STUCK_MS) {
    digitalWrite(PIN_LED, millis() >= POWER_HOLD_MS ? HIGH : LOW);
    delay(10);
  }
  digitalWrite(PIN_LED, LOW);
  if (buttonDown() || millis() < POWER_HOLD_MS) sleepNow();  // bloqueada o pulsación corta: sigue apagado
  s_offMagic = 0;
  s_wokeByButton = true;
  releaseHolds();
  ledBlink(2);
}

bool powerWokeByButton() { return s_wokeByButton; }

void powerBegin() { xTaskCreatePinnedToCore(buttonTask, "button", 2048, nullptr, 1, nullptr, 0); }

bool powerOffRequested() { return s_offRequested; }

bool powerTakeShortPress() {
  if (!s_shortPress) return false;
  s_shortPress = false;
  return true;
}

void powerSleep() {
  digitalWrite(PIN_LED, LOW);
  delay(200);
  ledBlink(3);
  sleepNow();
}

void gpsSleep() {
  // UBX-RXM-PMREQ con duración 0 (indefinida) y bit de backup: el NEO-6M pasa de unos 45 mA a microamperios
  static const uint8_t kPmreq[] = {0xB5, 0x62, 0x02, 0x41, 0x08, 0x00, 0x00, 0x00,
                                   0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x4D, 0x3B};
  Serial2.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  for (int i = 0; i < 2; i++) {
    Serial2.write(kPmreq, sizeof(kPmreq));
    Serial2.flush();
    delay(100);
  }
  Serial2.end();
  pinMode(PIN_GPS_TX, OUTPUT);
  digitalWrite(PIN_GPS_TX, HIGH);  // línea en reposo para que no se despierte
}
