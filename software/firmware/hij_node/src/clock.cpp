#include "clock.h"

#include <esp_attr.h>
#include <esp_random.h>
#include <sys/time.h>

#define CLOCK_MAGIC 0x48494A43UL  // "HIJC"

// La memoria RTC sin inicializar se conserva en el sueño profundo y en los reinicios por software
RTC_NOINIT_ATTR static uint32_t s_magic;
RTC_NOINIT_ATTR static uint32_t s_session;
RTC_NOINIT_ATTR static uint64_t s_lastMs;

uint64_t nodeTimeMs() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (uint64_t)tv.tv_sec * 1000ULL + (uint64_t)(tv.tv_usec / 1000);
}

void clockBegin() {
  const uint64_t now = nodeTimeMs();
  if (s_magic != CLOCK_MAGIC || now < s_lastMs) {
    s_magic = CLOCK_MAGIC;
    s_session = esp_random();
  }
  s_lastMs = now;
}

uint32_t clockSession() { return s_session; }

void clockTouch() { s_lastMs = nodeTimeMs(); }
