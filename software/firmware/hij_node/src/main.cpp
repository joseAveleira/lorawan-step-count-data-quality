// Firmware del nodo para la extensión HIJ 2026.
// Agrega los pasos en bins de duración Δ identificados por (época, secuencia), los guarda en un buffer
// persistente y los envía por LoRaWAN (TTN) con la estrategia S0 a S3. Registra por serie la verdad
// terreno (GT), cada trama, enviada o no (TX), y los eventos (EV). Las demás líneas empiezan por '#'.
//
//   GT,<e>,<s>,<t_inicio_ms>,<pasos>,<acumulado>,<k>   (k = intervalo desde el inicio del experimento)
//   TX,<e>,<fcnt>,<s1>,<n>,<edad>,<dr>,<bytes>,<sent|suppressed|dropped>,<toa_ms>,<t_ms>
//      (t_ms = instante de la transmisión en el reloj del nodo; la línea se imprime tras las ventanas RX)
//   EV,<tipo>,...

#include <Arduino.h>
#include <Preferences.h>
#include <esp_sleep.h>

#include "activity.h"
#include "binbuf.h"
#include "clock.h"
#include "config.h"
#include "gps.h"
#include "link.h"
#include "payload.h"
#include "power.h"
#include "scenario.h"
#include "settings.h"
#include "track.h"
#include "ui.h"

namespace {

uint8_t epoch;
uint16_t deltaS;            // Δ de esta época
uint64_t deltaMs;
uint64_t epochStartMs;      // inicio del bin 0
uint64_t expStartMs;        // inicio del experimento (comando "exp"); referencia de los escenarios
uint16_t openSeq = 0;       // secuencia del bin abierto
uint64_t openStartMs;       // inicio del bin abierto
uint32_t cumulative = 0;    // pasos de los bins cerrados desde el arranque
uint32_t sensorAtOpen = 0;
uint64_t nextTxMs;
uint64_t nextJoinMs = 0;
uint32_t joinBackoffS = 60;
uint16_t joinAttempts = 0;
uint32_t txIndex = 0;       // tramas generadas en esta época
uint16_t uplinksSinceAck = 0;
uint32_t s0CumAtSend = 0;   // S0: acumulado y primer bin desde el último envío
uint16_t s0FirstSeq = 0;
// S3: último bin de la trama anterior. Un ACK que llega en la ventana RX de la trama actual se calculó antes
// de recibirla, así que solo indica un hueco si se queda por detrás de la trama anterior.
bool havePrevFrameLast = false;
uint32_t s3Frames = 0;
uint8_t prevFrameLastEpoch = 0;
uint16_t prevFrameLastSeq = 0;
uint8_t curFrameLastEpoch = 0;
uint16_t curFrameLastSeq = 0;
const char* lastTxStatus = nullptr;  // para la pantalla
uint64_t lastTxMs = 0;
uint32_t lastFcnt = 0;

// Comprobación de cobertura (pruebas de campo propias; comando "check", no se guarda: cada arranque empieza
// apagada). Cada checkS segundos el siguiente uplink lleva un LinkCheckReq; cualquier downlink cuenta como respuesta.
uint32_t checkS = 0;
uint64_t nextCheckMs = 0;
bool checkPending = false;
bool haveReply = false;      // alguna vez ha contestado la red
uint64_t lastReplyMs = 0;
bool haveNoReply = false;    // alguna comprobación sin respuesta
uint64_t lastNoReplyMs = 0;
uint8_t covState = 0;        // 0 sin datos, 1 la última comprobación o downlink fue respuesta, 2 la última comprobación no
uint8_t lastGw = 0;
uint8_t lastMargin = 0;
uint64_t nextTrackMs = 0;    // registro de la ruta: uno por minuto con el GPS encendido

struct Frame {
  uint8_t epoch = 0;
  uint16_t s1 = 0;
  uint8_t n = 0;
  uint16_t age = 0;
  uint8_t flags = 0;
  uint16_t endIdx = 0;      // la trama cubre los bins del buffer hasta endIdx (exclusivo)
  uint16_t bins[PAYLOAD_MAX_BINS];
  uint8_t data[PAYLOAD_HDR_LEN + 2 * PAYLOAD_MAX_BINS + GPS_TRAILER_LEN];
  size_t len = 0;
};

// El inicio del experimento se guarda en NVS en el reloj del nodo, que sigue contando en los reinicios
// por software y en los apagados con el botón. Tras un arranque en frío ya no es válido.
void loadExpStart() {
  Preferences p;
  p.begin("hij", false);
  const uint64_t saved = p.getULong64("exp0", 0);
  p.end();
  const bool valid = saved > 0 && saved <= nodeTimeMs();
  expStartMs = valid ? saved : epochStartMs;
  Serial.printf("EV,exp,%s,%llu\n", valid ? "continue" : "boot", expStartMs);
}

void startExperiment() {
  expStartMs = nodeTimeMs();
  Preferences p;
  p.begin("hij", false);
  p.putULong64("exp0", expStartMs);
  p.end();
  Serial.printf("EV,exp,start,%llu\n", expStartMs);
}

uint8_t nextEpoch() {
  Preferences p;
  p.begin("hij", false);
  const uint8_t e = p.getUChar("epoch", 255) + 1;  // 0 en el primer arranque
  p.putUChar("epoch", e);
  p.end();
  return e;
}

uint16_t ageUnits(uint64_t tStartMs, uint16_t binDeltaS, bool valid, uint64_t now) {
  if (!valid || now < tStartMs) return AGE_UNKNOWN;
  const uint64_t units = (now - tStartMs) * 15ULL / ((uint64_t)binDeltaS * 1000ULL);
  return units >= AGE_UNKNOWN ? AGE_UNKNOWN - 1 : (uint16_t)units;
}

// Intervalo del bin abierto contado desde el inicio del experimento. Se redondea porque, tras un reinicio,
// los bins de la época nueva no están alineados con los de la anterior.
uint32_t intervalIndex() {
  const int64_t rel = (int64_t)openStartMs - (int64_t)expStartMs;
  const int64_t half = (int64_t)deltaMs / 2;
  return (uint32_t)((rel >= 0 ? rel + half : rel - half) / (int64_t)deltaMs);
}

void closeBin() {
  const uint32_t k = intervalIndex();
  uint16_t steps;
  if (cfg.source == 0) {
    steps = syntheticSteps(cfg.seed, k, deltaS);
  } else {
    const uint32_t c = sensorSteps();
    const uint32_t d = c - sensorAtOpen;
    sensorAtOpen = c;
    steps = d > 0xFFFF ? 0xFFFF : d;
  }
  cumulative += steps;
  Serial.printf("GT,%u,%u,%llu,%u,%lu,%ld\n", epoch, openSeq, openStartMs, steps, (unsigned long)cumulative,
                (long)(int32_t)k);
  const PushResult r = bufPush(epoch, openSeq, deltaS, openStartMs, steps, cfg.strategy);
  if (r.lost) Serial.printf("EV,overflow,%u,%u\n", r.epoch, r.seq);
  openSeq++;
  openStartMs += deltaMs;
}

uint8_t maxBinsPerFrame() {
  int m = ((int)linkMaxPayload() - PAYLOAD_HDR_LEN - (gpsActive() ? GPS_TRAILER_LEN : 0)) / 2;
  if (m > PAYLOAD_MAX_BINS) m = PAYLOAD_MAX_BINS;
  return m < 1 ? 1 : m;
}

// S0: pasos desde el último envío, sin historial por bins
bool buildS0(Frame& f, uint64_t now) {
  if (s0FirstSeq == openSeq) return false;  // ningún bin cerrado desde el último envío
  const uint32_t inc = cumulative - s0CumAtSend;
  f.epoch = epoch;
  f.s1 = s0FirstSeq;
  f.n = 1;
  f.bins[0] = inc > 0xFFFF ? 0xFFFF : inc;
  f.age = ageUnits(epochStartMs + (uint64_t)s0FirstSeq * deltaMs, deltaS, true, now);
  f.endIdx = bufCount();
  return true;
}

// S1, S2 y S3: bins pendientes del más antiguo al más reciente; en la S2 se añaden delante hasta
// `redundancy` bins ya enviados. Una trama nunca mezcla épocas.
bool buildFromBuffer(Frame& f, uint64_t now, uint8_t redundancy) {
  const uint16_t first = bufSent();
  if (first >= bufCount()) return false;
  const uint16_t segStart = bufSegmentStart(first);
  const uint16_t segEnd = bufSegmentEnd(first);
  const uint8_t maxBins = maxBinsPerFrame();
  const uint16_t nNew = min<uint16_t>(segEnd - first, maxBins);
  const uint16_t nOld = min<uint16_t>(min<uint16_t>(redundancy, maxBins - nNew), first - segStart);
  const uint16_t start = first - nOld;
  const BinView b0 = bufAt(start);
  f.epoch = b0.epoch;
  f.s1 = b0.seq;
  f.n = nOld + nNew;
  f.age = ageUnits(b0.tStartMs, b0.deltaS, b0.timeValid, now);
  for (uint16_t i = 0; i < f.n; i++) f.bins[i] = bufAt(start + i).steps;
  if (nOld > 0 || start < bufSentHigh()) f.flags |= FLAG_RETX;
  f.endIdx = first + nNew;
  // así el backend sabe que la época anterior no tiene más bins y puede confirmar la siguiente (S3)
  if (f.endIdx == segEnd && b0.epoch != epoch) f.flags |= FLAG_EPOCH_END;
  return true;
}

void logTx(const Frame& f, long fcnt, int dr, const char* status, uint32_t toaMs, uint64_t tMs) {
  Serial.printf("TX,%u,%ld,%u,%u,%u,%d,%u,%s,%lu,%llu\n", f.epoch, fcnt, f.s1, f.n, f.age, dr, (unsigned)f.len,
                status, (unsigned long)toaMs, tMs);
  lastTxStatus = status;
  lastTxMs = tMs;
  if (fcnt >= 0) lastFcnt = fcnt;
}

// La trama se da por enviada (transmitida o perdida por el escenario emulado)
void frameDone(const Frame& f) {
  bufClearOverflow();
  if (cfg.strategy == 0) {
    s0CumAtSend = cumulative;
    s0FirstSeq = openSeq;
  }
  bufMarkSent(f.endIdx);
  if (cfg.strategy == 3) {
    prevFrameLastEpoch = curFrameLastEpoch;
    prevFrameLastSeq = curFrameLastSeq;
    curFrameLastEpoch = f.epoch;
    curFrameLastSeq = f.s1 + f.n - 1;
    havePrevFrameLast = ++s3Frames >= 2;
    // red de seguridad (ACK perdido): el backend confirma cada n tramas y avisa de los huecos
    if (++uplinksSinceAck >= 2 * cfg.n + 2 && bufSent() > bufAcked()) {
      Serial.printf("EV,rewind,timeout,%u\n", bufRewind());
      uplinksSinceAck = 0;
    }
  }
  bufRelease(cfg.strategy, cfg.k);
}

void handleDownlink(const LinkResult& r) {
  Serial.printf("EV,downlink,%u,%u\n", r.downPort, (unsigned)r.downLen);
  if (r.downPort != FPORT_ACK || r.downLen < 3) return;
  const uint8_t e = r.down[0];
  const uint16_t s = (r.down[1] << 8) | r.down[2];
  const int idx = bufAck(e, s);
  Serial.printf("EV,ack,%u,%u,%d\n", e, s, idx);
  uplinksSinceAck = 0;
  if (cfg.strategy == 3 && havePrevFrameLast) {
    // ¿Se queda el ACK por detrás del último bin de la trama anterior? Entonces el backend tiene un hueco:
    // se reenvía todo lo no confirmado desde ahí (la trama actual puede salir duplicada).
    int prevIdx = -1;
    for (uint16_t i = 0; i < bufCount(); i++) {
      const BinView b = bufAt(i);
      if (b.epoch == prevFrameLastEpoch && b.seq == prevFrameLastSeq) {
        prevIdx = i;
        break;
      }
    }
    if (prevIdx >= 0 && (idx < 0 || idx < prevIdx) && bufSent() > bufAcked()) {
      Serial.printf("EV,rewind,gap,%u\n", bufRewind());
    }
  }
  bufRelease(cfg.strategy, cfg.k);
}

// Tras un uplink transmitido: ¿ha contestado la red? (respuesta LinkCheck o cualquier downlink)
void noteCoverage(const LinkResult& r, uint64_t now) {
  const bool reply = r.state > 0;
  if (reply) {
    haveReply = true;
    lastReplyMs = now;
    covState = 1;
  }
  if (!checkPending) return;
  checkPending = false;
  uint8_t margin = 0, gw = 0;
  if (reply && linkLinkCheckAns(&margin, &gw)) {
    lastGw = gw;
    lastMargin = margin;
    Serial.printf("EV,linkcheck,ok,%u,%u\n", margin, gw);
  } else if (reply) {
    Serial.println("EV,linkcheck,down");  // contestó la red, pero sin respuesta LinkCheck
  } else {
    haveNoReply = true;
    lastNoReplyMs = now;
    covState = 2;
    Serial.println("EV,linkcheck,none");
  }
}

void trackTick(uint64_t now) {
  if (!gpsActive() || now < nextTrackMs) return;
  nextTrackMs = now + 60000ULL;
  const GpsState g = gpsState();
  TrackRecord r = {};
  r.epoch = epoch;
  r.session = clockSession();
  r.tS = (uint32_t)(now / 1000ULL);
  r.lat = g.lat;
  r.lon = g.lon;
  r.sats = g.sats;
  r.fix = g.fix;
  r.hdop10 = g.hdop10;
  r.ageS = g.ageS;
  if (haveReply && now - lastReplyMs <= 60000ULL) r.cov = 1;
  else if (haveNoReply && now - lastNoReplyMs <= 60000ULL) r.cov = 2;
  r.gw = lastGw;
  r.margin = lastMargin;
  trackAppend(r);
}

void logCheck() {
  const uint64_t now = nodeTimeMs();
  Serial.printf("EV,check,%lu,cov=%u,reply_ago_s=%ld,gw=%u,margin=%u,track=%u\n", (unsigned long)checkS, covState,
                haveReply ? (long)((now - lastReplyMs) / 1000ULL) : -1L, lastGw, lastMargin, trackCount());
}

void transmit(uint64_t now) {
  // RadioLib espacia las tramas según su airtime; si con el periodo fijado solo faltan unos segundos para cumplir
  // el límite, se espera ese poco en vez de perder el turno entero (antes de construir la trama, para que la
  // edad del primer bin sea la del momento real de transmisión)
  const uint32_t waitMs = linkJoined() ? linkTimeUntilUplinkMs() : 0;
  if (waitMs > 0 && waitMs <= FUP_MAX_WAIT_MS) {
    delay(waitMs);
    now = nodeTimeMs();
  }
  // Antes de construir la trama: el MAC ocupa un byte y RadioLib lo descuenta del payload máximo
  if (checkS && !checkPending && linkJoined() && now >= nextCheckMs) {
    checkPending = linkRequestLinkCheck();
    nextCheckMs = now + (uint64_t)checkS * 1000ULL;
  }
  Frame f;
  bool ok;
  switch (cfg.strategy) {
    case 0: ok = buildS0(f, now); break;
    case 2: ok = buildFromBuffer(f, now, cfg.k); break;
    default: ok = buildFromBuffer(f, now, 0); break;
  }
  if (!ok) return;
  if (bufOverflowPending()) f.flags |= FLAG_OVERFLOW;
  if (cfg.source == 1 && !sensorOk()) f.flags |= FLAG_SENSOR_FAIL;
  if (gpsActive()) f.flags |= FLAG_GPS;
  f.len = encodeFrame(f.data, f.flags, f.epoch, f.s1, f.n, f.age, cumulative, f.bins);
  if (gpsActive()) f.len += gpsTrailer(f.data + f.len);  // el backend ignora lo que va detrás de los bins
  txIndex++;

  if (!linkJoined()) {
    logTx(f, -1, -1, "dropped", 0, now);  // sin red: los bins siguen pendientes
    return;
  }
  const bool confirmed = cfg.strategy == 4;  // S4: uplinks confirmados de LoRaWAN (parar y esperar por trama)
  if (!coverageAvailable(now - expStartMs, txIndex)) {
    if (!confirmed) frameDone(f);  // en S4 no llegaría el ACK: los bins siguen pendientes
    logTx(f, -1, cfg.adr ? -1 : cfg.dr, "suppressed", 0, now);
    return;
  }
  const LinkResult r = linkSend(f.data, f.len, FPORT_BASE + cfg.strategy, confirmed);
  if (r.state < 0) {
    // RADIOLIB_ERR_UPLINK_UNAVAILABLE (-1108) = límite de airtime: los bins siguen pendientes
    Serial.printf("EV,txerr,%d\n", r.state);
    logTx(f, -1, -1, "dropped", 0, now);
    return;
  }
  if (confirmed) {
    Serial.printf("EV,confirm,%lu,%d\n", (unsigned long)r.fcnt, r.acked);
    if (r.acked) {
      bufClearOverflow();
      bufAckUpTo(f.endIdx);
      bufRelease(cfg.strategy, cfg.k);
    }
  } else {
    frameDone(f);
  }
  logTx(f, r.fcnt, r.dr, "sent", r.toaMs, now);
  ledBlink(1, 20, 0);
  noteCoverage(r, nodeTimeMs());
  if (r.state > 0 && r.downLen) handleDownlink(r);
}

void tryJoin(uint64_t now) {
  joinAttempts++;
  const int16_t st = linkJoin();
  if (linkJoined()) {
    Serial.printf("EV,join,%s,%d,%u\n", linkWasRestored(st) ? "restored" : "ok", st, joinAttempts);
    joinBackoffS = 60;
    return;
  }
  Serial.printf("EV,join,fail,%d,%u\n", st, joinAttempts);
  nextJoinMs = now + (uint64_t)joinBackoffS * 1000ULL;
  joinBackoffS = min<uint32_t>(joinBackoffS * 2, 900);
}

[[noreturn]] void shutdown(const char* reason) {
  Serial.printf("EV,power,off,%s,%u,%u\n", reason, epoch, openSeq);  // el bin abierto se descarta
  uiMessage("Apagando");
  bufSave();
  linkSaveSession();
  linkSleep();
  sensorSleep();
  gpsSleep();
  delay(800);
  uiSleep();
  digitalWrite(PIN_VEXT, HIGH);
  powerSleep();
}

void logStatus() {
  Serial.printf("EV,status,joined=%d,epoch=%u,open=%u,cum=%lu,count=%u,sent=%u,acked=%u\n", linkJoined(), epoch,
                openSeq, (unsigned long)cumulative, bufCount(), bufSent(), bufAcked());
}

void dumpBuffer() {
  for (uint16_t i = 0; i < bufCount(); i++) {
    const BinView b = bufAt(i);
    const char* st = i < bufAcked() ? "acked" : i < bufSent() ? "sent" : "pending";
    Serial.printf("BUF,%u,%u,%u,%u,%s\n", i, b.epoch, b.seq, b.steps, st);
  }
  logStatus();
}

void applyRuntime(const char* key) {
  if (!strcmp(key, "dr") || !strcmp(key, "adr") || !strcmp(key, "fup")) linkApplySettings();
  if (!strcmp(key, "display")) uiSetMode(cfg.display);
  if (!strcmp(key, "flip")) uiSetFlip(cfg.flip);
  if (!strcmp(key, "period")) nextTxMs = nodeTimeMs() + (uint64_t)cfg.periodS * 1000ULL;
  if (!strcmp(key, "strategy")) {
    s0CumAtSend = cumulative;
    s0FirstSeq = openSeq;
    uplinksSinceAck = 0;
  }
}

void handleCommand(char* line) {
  char* cmd = strtok(line, " \t");
  if (!cmd) return;
  if (!strcmp(cmd, "set")) {
    char* key = strtok(nullptr, " \t");
    char* val = strtok(nullptr, " \t");
    const long v = val ? strtol(val, nullptr, 0) : 0;
    const int r = (key && val) ? settingsSet(key, v) : -1;
    if (r < 0) {
      Serial.println(
          "# uso: set <strategy|scenario|delta|period|k|n|seed|source|dr|adr|fup|buf|display|cycle|window|geloss|"
          "geburst|outstart|outlen|flip> <valor>");
      return;
    }
    Serial.printf("EV,set,%s,%ld%s\n", key, v, r == 1 ? ",next_boot" : "");
    if (r == 0) applyRuntime(key);
  } else if (!strcmp(cmd, "show")) {
    settingsLog();
    logStatus();
    gpsLog();
    logCheck();
  } else if (!strcmp(cmd, "check")) {
    char* val = strtok(nullptr, " \t");
    if (val) {
      checkS = (uint32_t)strtoul(val, nullptr, 0);
      nextCheckMs = nodeTimeMs();  // la primera comprobación va en el siguiente uplink
    }
    logCheck();
  } else if (!strcmp(cmd, "track")) {
    trackDump();
  } else if (!strcmp(cmd, "gps")) {
    char* val = strtok(nullptr, " \t");
    if (val && val[0] == '1') gpsStart();
    else if (val && val[0] == '0') gpsStop();
    else gpsLog();
  } else if (!strcmp(cmd, "dump")) {
    dumpBuffer();
  } else if (!strcmp(cmd, "reboot")) {
    Serial.println("EV,reboot");
    bufSave();
    linkSaveSession();
    Serial.flush();
    ESP.restart();
  } else if (!strcmp(cmd, "rejoin")) {
    linkForgetSession();
    nextJoinMs = 0;
    joinBackoffS = 60;
    Serial.println("EV,rejoin");
  } else if (!strcmp(cmd, "off")) {
    shutdown("cmd");
  } else if (!strcmp(cmd, "clear")) {
    bufClear();
    trackClear();
    Serial.println("EV,clear");
  } else if (!strcmp(cmd, "exp")) {
    startExperiment();
  } else {
    Serial.println("# comandos: set <clave> <valor> | show | dump | exp | reboot | rejoin | off | clear | gps 0|1 | "
                   "check <s> | track");
  }
}

void pollSerial() {
  static char buf[96];
  static uint8_t len = 0;
  while (Serial.available()) {
    const char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[len] = 0;
      if (len) handleCommand(buf);
      len = 0;
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}

void closeDueBins() {
  const uint64_t now = nodeTimeMs();
  while (now >= openStartMs + deltaMs) closeBin();
}

void refreshUi() {
  if (powerTakeShortPress()) uiButton();
  UiStatus s;
  s.strategy = cfg.strategy;
  s.scenario = cfg.scenario;
  s.epoch = epoch;
  s.sensor = cfg.source == 1;
  s.joined = linkJoined();
  s.fcnt = lastFcnt;
  s.openSeq = openSeq;
  s.pending = bufCount() - bufSent();
  s.cumulative = cumulative;
  s.lastTx = lastTxStatus;
  s.lastTxAgoS = lastTxStatus ? (uint32_t)((nodeTimeMs() - lastTxMs) / 1000ULL) : 0;
  s.gps = gpsShort();
  s.checkOn = checkS > 0;
  s.cov = covState;
  s.replyAgoS = haveReply ? (uint32_t)((nodeTimeMs() - lastReplyMs) / 1000ULL) : UINT32_MAX;
  s.gw = lastGw;
  uiUpdate(s);
}

}  // namespace

void setup() {
  powerBootCheck();  // si viene de un apagado con el botón, exige la pulsación larga o vuelve a dormir
  Serial.begin(115200);
  delay(50);
  clockBegin();
  settingsLoad();
  epoch = nextEpoch();
  deltaS = cfg.deltaS;
  deltaMs = (uint64_t)deltaS * 1000ULL;
  Serial.printf("EV,boot,%u,%s,%d,%s\n", epoch, FW_VERSION, (int)esp_reset_reason(),
                powerWokeByButton() ? "button" : "reset");
  if (powerWokeByButton()) Serial.println("EV,power,on");
  settingsLog();

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  pinMode(PIN_VEXT, OUTPUT);
  digitalWrite(PIN_VEXT, LOW);
  delay(50);
  uiBegin(cfg.display, cfg.flip);
  uiMessage(powerWokeByButton() ? "Encendido" : "HIJ node", "v" FW_VERSION);
  gpsSleep();
  const bool sensor = sensorBegin();
  Serial.printf("EV,sensor,%s,0x%02X\n", sensor ? "ok" : "fail", sensorAddr());
  if (sensor && cfg.source == 0) sensorSleep();  // con el patrón sintético no se usa

  bufBegin(cfg.bufBins);
  trackBegin();
  Serial.printf("EV,buffer,%u,%u,%u\n", bufCount(), bufSent(), bufAcked());  // bins de antes del arranque
  linkBegin();

  epochStartMs = openStartMs = nodeTimeMs();
  loadExpStart();
  nextTxMs = openStartMs + (uint64_t)cfg.periodS * 1000ULL;
  powerBegin();
}

void loop() {
  if (powerOffRequested()) shutdown("button");
  pollSerial();
  if (cfg.source == 1) sensorPoll();
  gpsPoll();
  trackTick(nodeTimeMs());
  closeDueBins();
  if (linkReady() && !linkJoined() && nodeTimeMs() >= nextJoinMs) {
    tryJoin(nodeTimeMs());
    closeDueBins();  // el join bloquea unos segundos
  }
  if (nodeTimeMs() >= nextTxMs) {
    transmit(nodeTimeMs());
    nextTxMs += (uint64_t)cfg.periodS * 1000ULL;
    if (nextTxMs <= nodeTimeMs()) nextTxMs = nodeTimeMs() + (uint64_t)cfg.periodS * 1000ULL;
  }
  bufSave();
  refreshUi();
  clockTouch();
  delay(20);
}
