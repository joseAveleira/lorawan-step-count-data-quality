#include "settings.h"

#include <Preferences.h>

#include "config.h"

Settings cfg;

namespace {

struct Field {
  const char* key;
  uint8_t size;  // bytes del campo en Settings
  void* ptr;
  long minV;
  long maxV;
  long def;
  bool atBoot;   // solo se aplica en el próximo arranque
};

const Field kFields[] = {
    {"strategy", 1, &cfg.strategy, 0, 4, DEF_STRATEGY, false},
    {"scenario", 1, &cfg.scenario, 1, 5, DEF_SCENARIO, false},
    {"delta", 2, &cfg.deltaS, 10, 3600, DEF_DELTA_S, true},
    {"period", 2, &cfg.periodS, 10, 65535, DEF_PERIOD_S, false},
    {"k", 1, &cfg.k, 0, 50, DEF_K, false},
    {"n", 1, &cfg.n, 1, 200, DEF_N, false},
    {"seed", 4, &cfg.seed, 0, 0x7FFFFFFFL, DEF_SEED, true},
    {"source", 1, &cfg.source, 0, 1, DEF_SOURCE, true},
    {"dr", 1, &cfg.dr, 0, 5, DEF_DR, false},
    {"adr", 1, &cfg.adr, 0, 1, DEF_ADR, false},
    {"fup", 1, &cfg.fup, 0, 1, DEF_FUP, false},
    {"buf", 2, &cfg.bufBins, 16, MAX_BUF_BINS, DEF_BUF_BINS, true},
    {"display", 1, &cfg.display, 0, 2, DEF_DISPLAY, false},
    {"cycle", 4, &cfg.cycle, 60, 0x7FFFFFFFL, DEF_CYCLE_S, false},
    {"window", 4, &cfg.window, 0, 0x7FFFFFFFL, DEF_WINDOW_S, false},
    {"geloss", 1, &cfg.geLoss, 0, 99, DEF_GE_LOSS, false},
    {"geburst", 1, &cfg.geBurst, 1, 100, DEF_GE_BURST, false},
    {"outstart", 4, &cfg.outStart, 0, 0x7FFFFFFFL, DEF_OUT_START_S, false},
    {"outlen", 4, &cfg.outLen, 0, 0x7FFFFFFFL, DEF_OUT_LEN_S, false},
    {"flip", 1, &cfg.flip, 0, 1, DEF_FLIP, false},
};

void writeField(const Field& f, long v) {
  switch (f.size) {
    case 1: *static_cast<uint8_t*>(f.ptr) = v; break;
    case 2: *static_cast<uint16_t*>(f.ptr) = v; break;
    default: *static_cast<uint32_t*>(f.ptr) = v; break;
  }
}

long readField(const Field& f) {
  switch (f.size) {
    case 1: return *static_cast<uint8_t*>(f.ptr);
    case 2: return *static_cast<uint16_t*>(f.ptr);
    default: return *static_cast<uint32_t*>(f.ptr);
  }
}

}  // namespace

void settingsLoad() {
  Preferences p;
  p.begin("hijcfg", false);  // en modo lectura falla (con un error en el log) si el espacio aún no existe
  for (const Field& f : kFields) {
    long v = p.getLong(f.key, f.def);
    if (v < f.minV || v > f.maxV) v = f.def;
    writeField(f, v);
  }
  p.end();
}

int settingsSet(const char* key, long value) {
  for (const Field& f : kFields) {
    if (strcmp(f.key, key) != 0) continue;
    if (value < f.minV || value > f.maxV) return -1;
    Preferences p;
    p.begin("hijcfg", false);
    p.putLong(f.key, value);
    p.end();
    if (f.atBoot) return 1;
    writeField(f, value);
    return 0;
  }
  return -1;
}

void settingsLog() {
  Serial.print("EV,config");
  for (const Field& f : kFields) Serial.printf(",%s=%ld", f.key, readField(f));
  Serial.println();
}
