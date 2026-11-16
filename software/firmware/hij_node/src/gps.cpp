#include "gps.h"

#include "config.h"
#include "power.h"

namespace {

bool s_on = false;
char s_line[100];
uint8_t s_len = 0;
uint32_t s_nmea = 0;        // frases NMEA válidas desde que se encendió
uint32_t s_wakeMs = 0;
int32_t s_lat = 0;          // 1e-6 grados
int32_t s_lon = 0;
uint8_t s_sats = 0;
uint8_t s_hdop10 = 255;
bool s_fix = false;         // la última GGA tenía posición
bool s_everFix = false;
uint32_t s_fixMs = 0;
char s_short[16];

uint8_t hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return 0xFF;
}

// "ddmm.mmmm" (latitud, 2 cifras de grados) o "dddmm.mmmm" (longitud, 3) a grados x 1e6
bool parseCoord(const char* f, const char* hemi, int degDigits, int32_t& out) {
  if (strlen(f) < (size_t)degDigits + 2 || !hemi[0]) return false;
  int deg = 0;
  for (int i = 0; i < degDigits; i++) {
    if (f[i] < '0' || f[i] > '9') return false;
    deg = deg * 10 + (f[i] - '0');
  }
  double v = deg + atof(f + degDigits) / 60.0;
  if (hemi[0] == 'S' || hemi[0] == 'W') v = -v;
  out = (int32_t)lround(v * 1e6);
  return true;
}

void parseLine(char* s) {
  if (s[0] != '$') return;
  char* star = strchr(s, '*');
  if (!star || hexNibble(star[1]) == 0xFF || hexNibble(star[2]) == 0xFF) return;
  uint8_t sum = 0;
  for (char* p = s + 1; p < star; p++) sum ^= (uint8_t)*p;
  if (sum != ((hexNibble(star[1]) << 4) | hexNibble(star[2]))) return;
  s_nmea++;
  *star = 0;
  if (strlen(s) < 6 || strncmp(s + 3, "GGA", 3) != 0) return;  // $GPGGA o $GNGGA
  // Campos separados por comas; los vacíos también cuentan (strtok se los saltaría)
  char* f[16] = {nullptr};
  int nf = 0;
  f[nf++] = s;
  for (char* p = s; *p && nf < 16; p++) {
    if (*p == ',') {
      *p = 0;
      f[nf++] = p + 1;
    }
  }
  if (nf < 9) return;
  s_sats = (uint8_t)atoi(f[7]);
  int32_t lat, lon;
  s_fix = atoi(f[6]) > 0 && parseCoord(f[2], f[3], 2, lat) && parseCoord(f[4], f[5], 3, lon);
  if (!s_fix) return;
  s_lat = lat;
  s_lon = lon;
  const double h = atof(f[8]);
  s_hdop10 = h <= 0 ? 255 : (h >= 25.4 ? 254 : (uint8_t)lround(h * 10));
  s_everFix = true;
  s_fixMs = millis();
}

void wake() {
  // gpsSleep() lo deja en modo backup; cualquier dato en su RX lo despierta (los primeros bytes se pierden)
  for (int i = 0; i < 16; i++) Serial2.write(0xFF);
  Serial2.flush();
  s_wakeMs = millis();
}

void put32(uint8_t* o, int32_t v) {
  const uint32_t u = (uint32_t)v;
  o[0] = u >> 24;
  o[1] = (u >> 16) & 0xFF;
  o[2] = (u >> 8) & 0xFF;
  o[3] = u & 0xFF;
}

}  // namespace

void gpsStart() {
  if (s_on) return;
  Serial2.setRxBufferSize(1024);  // la trama bloquea unos 6 s (ventanas RX): que no se pierdan frases
  Serial2.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  s_on = true;
  s_len = 0;
  wake();
  Serial.println("EV,gps,on");
}

void gpsStop() {
  if (!s_on) return;
  s_on = false;
  gpsSleep();  // UBX-RXM-PMREQ y Serial2.end()
  Serial.println("EV,gps,off");
}

bool gpsActive() { return s_on; }

void gpsPoll() {
  if (!s_on) return;
  while (Serial2.available()) {
    const char c = Serial2.read();
    if (c == '\r') continue;
    if (c == '\n') {
      s_line[s_len] = 0;
      if (s_len) parseLine(s_line);
      s_len = 0;
    } else if (s_len < sizeof(s_line) - 1) {
      s_line[s_len++] = c;
    } else {
      s_len = 0;  // línea demasiado larga: basura
    }
  }
  // si no habla, se repite el despertar cada 5 s
  if (s_nmea == 0 && millis() - s_wakeMs > 5000) wake();
}

GpsState gpsState() {
  GpsState g;
  g.lat = s_lat;
  g.lon = s_lon;
  g.sats = s_sats;
  g.fix = s_fix ? 1 : 0;
  g.hdop10 = s_hdop10;
  if (!s_everFix) {
    g.ageS = 255;
  } else {
    const uint32_t ageS = (millis() - s_fixMs) / 1000;
    g.ageS = ageS > 254 ? 254 : (uint8_t)ageS;
  }
  return g;
}

size_t gpsTrailer(uint8_t* out) {
  const GpsState g = gpsState();
  put32(out, g.lat);
  put32(out + 4, g.lon);
  out[8] = g.sats;
  out[9] = g.hdop10;
  out[10] = g.ageS;
  return GPS_TRAILER_LEN;
}

void gpsLog() {
  Serial.printf("EV,gps,on=%d,nmea=%lu,fix=%d,sats=%u,lat=%.6f,lon=%.6f,hdop=%.1f\n", s_on, (unsigned long)s_nmea,
                s_fix, s_sats, s_lat / 1e6, s_lon / 1e6, s_hdop10 == 255 ? -1.0 : s_hdop10 / 10.0);
}

const char* gpsShort() {
  if (!s_on) return nullptr;
  if (s_nmea == 0) snprintf(s_short, sizeof s_short, "GPS mudo");
  else if (!s_fix) snprintf(s_short, sizeof s_short, "GPS busca %u", s_sats);
  else snprintf(s_short, sizeof s_short, "GPS ok %u", s_sats);
  return s_short;
}
