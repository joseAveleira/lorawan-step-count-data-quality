#include "track.h"

#include <LittleFS.h>

#include "clock.h"

#define TRACK_FILE "/track.bin"

namespace {
uint16_t s_count = 0;
bool s_full = false;
}  // namespace

void trackBegin() {
  File f = LittleFS.open(TRACK_FILE, "r");
  s_count = f ? (uint16_t)min<size_t>(f.size() / sizeof(TrackRecord), TRACK_MAX_RECORDS) : 0;
  if (f) f.close();
  s_full = s_count >= TRACK_MAX_RECORDS;
}

bool trackAppend(const TrackRecord& r) {
  if (s_count >= TRACK_MAX_RECORDS) {
    if (!s_full) Serial.println("EV,track,full");
    s_full = true;
    return false;
  }
  File f = LittleFS.open(TRACK_FILE, "a");
  if (!f) return false;
  const size_t n = f.write(reinterpret_cast<const uint8_t*>(&r), sizeof(r));
  f.close();
  if (n != sizeof(r)) return false;
  s_count++;
  return true;
}

uint16_t trackCount() { return s_count; }

void trackDump() {
  // Ancla: el PC pone su hora a esta línea; los registros con la misma base de tiempos se sitúan restando
  Serial.printf("EV,now,%llu,%lu\n", nodeTimeMs(), (unsigned long)clockSession());
  File f = LittleFS.open(TRACK_FILE, "r");
  uint16_t i = 0;
  if (f) {
    TrackRecord r;
    while (f.read(reinterpret_cast<uint8_t*>(&r), sizeof(r)) == sizeof(r)) {
      Serial.printf("TRK,%u,%u,%lu,%lu,%.6f,%.6f,%u,%u,%u,%u,%u,%u,%u\n", i++, r.epoch, (unsigned long)r.session,
                    (unsigned long)r.tS, r.lat / 1e6, r.lon / 1e6, r.sats, r.fix, r.hdop10, r.ageS, r.cov, r.gw,
                    r.margin);
      if ((i & 31) == 0) delay(5);  // que no se desborde el buffer de la UART
    }
    f.close();
  }
  Serial.printf("EV,track,%u\n", i);
}

void trackClear() {
  LittleFS.remove(TRACK_FILE);
  s_count = 0;
  s_full = false;
}
