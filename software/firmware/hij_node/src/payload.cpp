#include "payload.h"

size_t encodeFrame(uint8_t* out, uint8_t flags, uint8_t epoch, uint16_t s1, uint8_t n, uint16_t age,
                   uint32_t cumulative, const uint16_t* bins) {
  size_t i = 0;
  out[i++] = flags;
  out[i++] = epoch;
  out[i++] = s1 >> 8;
  out[i++] = s1 & 0xFF;
  out[i++] = n;
  out[i++] = age >> 8;
  out[i++] = age & 0xFF;
  out[i++] = cumulative >> 24;
  out[i++] = (cumulative >> 16) & 0xFF;
  out[i++] = (cumulative >> 8) & 0xFF;
  out[i++] = cumulative & 0xFF;
  for (uint8_t k = 0; k < n; k++) {
    out[i++] = bins[k] >> 8;
    out[i++] = bins[k] & 0xFF;
  }
  return i;
}
