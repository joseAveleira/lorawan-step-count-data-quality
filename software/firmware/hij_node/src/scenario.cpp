#include "scenario.h"

#include <Arduino.h>

#include "settings.h"

namespace {

uint32_t mix32(uint32_t x) {
  x ^= x >> 16;
  x *= 0x7FEB352DUL;
  x ^= x >> 15;
  x *= 0x846CA68BUL;
  x ^= x >> 16;
  return x;
}

bool geBad = false;  // estado del canal de Gilbert-Elliott (se reinicia con cada arranque)

}  // namespace

bool coverageAvailable(uint64_t tExpMs, uint32_t txIndex) {
  const uint64_t t = tExpMs / 1000ULL;
  switch (cfg.scenario) {
    case 2:
      return cfg.cycle > 0 && (t % cfg.cycle) < cfg.window;
    case 3: {
      // pérdida media pi = p / (p + r) y ráfaga media L = 1 / r
      const float pi = cfg.geLoss / 100.0f;
      const float r = 1.0f / (cfg.geBurst ? cfg.geBurst : 1);
      const float p = pi >= 1.0f ? 1.0f : r * pi / (1.0f - pi);
      const float u = mix32(cfg.seed ^ 0xC3A5C85CUL ^ (0x9E3779B9UL * (txIndex + 1))) / 4294967296.0f;
      geBad = geBad ? (u >= r) : (u < p);
      return !geBad;
    }
    case 4:
    case 5:
      return !(t >= cfg.outStart && t < (uint64_t)cfg.outStart + cfg.outLen);
    default:
      return true;
  }
}
