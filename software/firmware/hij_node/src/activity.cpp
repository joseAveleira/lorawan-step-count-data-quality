#include "activity.h"

#include <Adafruit_BNO08x.h>
#include <Wire.h>

#include "config.h"

// ---------------------------------------------------------------------------
// Patrón sintético
// ---------------------------------------------------------------------------
// Toda la aritmética es de 32 bits sin signo, así que se reproduce en Python aplicando & 0xFFFFFFFF
// tras cada multiplicación.

// Mezclador "lowbias32" (C. Wellons)
static uint32_t mix32(uint32_t x) {
  x ^= x >> 16;
  x *= 0x7FEB352DUL;
  x ^= x >> 15;
  x *= 0x846CA68BUL;
  x ^= x >> 16;
  return x;
}

// El tiempo se divide en bloques de ACT_BLOCK_BINS bins. Un bloque es activo con probabilidad del 35 %;
// en los bloques en reposo todos los bins valen 0 (periodos largos sin pasos). En un bloque activo, cada
// bin es una pausa (0 pasos) con probabilidad del 15 % y, si no, tiene una cadencia de 20 a 110 pasos/min.
uint16_t syntheticSteps(uint32_t seed, uint32_t k, uint16_t deltaS) {
  const uint32_t base = mix32(seed);
  const uint32_t hBlock = mix32(base ^ (0x9E3779B9UL * (k / ACT_BLOCK_BINS + 1)));
  if (hBlock % 100 >= 35) return 0;
  const uint32_t hBin = mix32(hBlock ^ (0x85EBCA6BUL * (k + 1)));
  if (hBin % 100 < 15) return 0;
  const uint32_t perMin = 20 + (hBin >> 8) % 91;
  const uint32_t steps = perMin * deltaS / 60;
  return steps > 0xFFFF ? 0xFFFF : steps;
}

// ---------------------------------------------------------------------------
// BNO085
// ---------------------------------------------------------------------------
static TwoWire s_wire(1);
static Adafruit_BNO08x s_bno(-1);  // el pin RST del módulo no está conectado
static bool s_ok = false;
static uint8_t s_addr = 0;
static uint16_t s_lastRaw = 0;     // el contador del sensor es de 16 bits
static uint32_t s_total = 0;

bool sensorBegin() {
  s_wire.begin(PIN_BNO_SDA, PIN_BNO_SCL);
  for (uint8_t addr : {BNO_ADDR_B, BNO_ADDR_A}) {
    if (s_bno.begin_I2C(addr, &s_wire)) {
      s_addr = addr;
      s_ok = s_bno.enableReport(SH2_STEP_COUNTER, 500000);
      break;
    }
  }
  s_lastRaw = 0;
  return s_ok;
}

bool sensorOk() { return s_ok; }

uint8_t sensorAddr() { return s_addr; }

void sensorPoll() {
  if (!s_ok) return;
  if (s_bno.wasReset()) {
    // tras un reinicio del sensor su contador vuelve a 0
    s_bno.enableReport(SH2_STEP_COUNTER, 500000);
    s_lastRaw = 0;
  }
  // A cero: getSensorEvent devuelve true sin evento nuevo si sensorId vale SH2_GYRO_INTEGRATED_RV, y con basura
  // de la pila ese bucle no terminaría hasta el siguiente informe del contador (que solo llega al dar pasos)
  sh2_SensorValue_t v = {};
  while (s_bno.getSensorEvent(&v)) {
    if (v.sensorId != SH2_STEP_COUNTER) continue;
    const uint16_t raw = v.un.stepCounter.steps;
    s_total += (uint16_t)(raw - s_lastRaw);
    s_lastRaw = raw;
  }
}

uint32_t sensorSteps() { return s_total; }

void sensorSleep() {
  if (s_ok) sh2_devSleep();
}
