#include "link.h"

#include <Preferences.h>
#include <RadioLib.h>

#include "config.h"
#if defined(NODE_ID) && NODE_ID == 2
#include "secrets_nodo2.h"  // segundo nodo (entorno nodo2 de platformio.ini)
#elif defined(NODE_ID) && NODE_ID == 3
#include "secrets_nodo3.h"  // tercer nodo (entorno nodo3)
#else
#include "secrets.h"
#endif
#include "settings.h"

namespace {

SX1262 radio = new Module(PIN_LORA_NSS, PIN_LORA_DIO1, PIN_LORA_RST, PIN_LORA_BUSY);
LoRaWANNode node(&radio, &EU868);
uint8_t appKey[16] = LORAWAN_APP_KEY;
bool radioOk = false;
bool joined = false;

constexpr size_t kSessionSize = RADIOLIB_LORAWAN_SESSION_BUF_SIZE;
constexpr size_t kNoncesSize = RADIOLIB_LORAWAN_NONCES_BUF_SIZE;
constexpr size_t kPersistSize = kSessionSize > kNoncesSize ? kSessionSize : kNoncesSize;
uint8_t persistBuf[kPersistSize];

void saveNonces() {
  Preferences p;
  p.begin("lorawan", false);
  p.putBytes("nonces", node.getBufferNonces(), RADIOLIB_LORAWAN_NONCES_BUF_SIZE);
  p.end();
}

}  // namespace

bool linkBegin() {
  int16_t st = radio.begin(868.1, 125.0, 9, 7, RADIOLIB_SX126X_SYNC_WORD_PRIVATE, 14, 8, LORA_TCXO_V, false);
  if (st != RADIOLIB_ERR_NONE) {
    Serial.printf("EV,radio,error,%d\n", st);
    return false;
  }
  st = node.beginOTAA(LORAWAN_JOIN_EUI, LORAWAN_DEV_EUI, nullptr, appKey);
  if (st != RADIOLIB_ERR_NONE) {
    Serial.printf("EV,radio,otaa_error,%d\n", st);
    return false;
  }
  radioOk = true;
  node.setDutyCycle(true, cfg.fup ? FUP_MS_PER_HOUR : 0);

  // El DevNonce tiene que seguir creciendo entre arranques o TTN rechaza el join
  Preferences p;
  p.begin("lorawan", false);
  if (p.getBytes("nonces", persistBuf, RADIOLIB_LORAWAN_NONCES_BUF_SIZE) == RADIOLIB_LORAWAN_NONCES_BUF_SIZE &&
      node.setBufferNonces(persistBuf) == RADIOLIB_ERR_NONE &&
      p.getBytes("session", persistBuf, RADIOLIB_LORAWAN_SESSION_BUF_SIZE) == RADIOLIB_LORAWAN_SESSION_BUF_SIZE) {
    node.setBufferSession(persistBuf);
  }
  p.end();
  return true;
}

bool linkReady() { return radioOk; }

bool linkJoined() { return joined; }

int16_t linkJoin() {
  if (!radioOk) return RADIOLIB_ERR_INVALID_MODE;
  const int16_t st = node.activateOTAA();
  saveNonces();  // cada intento consume un DevNonce
  if (st == RADIOLIB_LORAWAN_NEW_SESSION || st == RADIOLIB_LORAWAN_SESSION_RESTORED) {
    joined = true;
    linkApplySettings();
    linkSaveSession();
  }
  return st;
}

bool linkWasRestored(int16_t state) { return state == RADIOLIB_LORAWAN_SESSION_RESTORED; }

void linkApplySettings() {
  if (!radioOk) return;
  node.setDutyCycle(true, cfg.fup ? FUP_MS_PER_HOUR : 0);  // sin FUP queda el duty cycle legal de la banda
  if (!joined) return;
  node.setADR(cfg.adr);
  if (!cfg.adr) node.setDatarate(cfg.dr);
}

LinkResult linkSend(const uint8_t* data, size_t len, uint8_t fport, bool confirmed) {
  LinkResult r = {};
  LoRaWANEvent_t up = {};
  LoRaWANEvent_t down = {};
  size_t downLen = 0;
  r.state = node.sendReceive(data, len, fport, r.down, &downLen, confirmed, &up, &down);
  r.acked = confirmed && r.state > 0 && down.confirming;
  if (r.state >= 0) {
    r.fcnt = node.getFCntUp();  // up.fCnt ya es el contador de la siguiente trama
    r.dr = up.datarate;
    r.toaMs = node.getLastToA();
    r.downLen = r.state > 0 ? downLen : 0;
    r.downPort = down.fPort;
    linkSaveSession();  // el contador de tramas tiene que sobrevivir a un reinicio
  }
  return r;
}

uint32_t linkTimeUntilUplinkMs() { return radioOk ? node.timeUntilUplink() : 0; }

uint8_t linkMaxPayload() {
  if (joined) return node.getMaxPayloadLen();
  static const uint8_t kMaxByDr[] = {51, 51, 51, 115, 222, 222};
  return kMaxByDr[cfg.dr > 5 ? 5 : cfg.dr];
}

void linkSaveSession() {
  if (!joined) return;
  Preferences p;
  p.begin("lorawan", false);
  p.putBytes("session", node.getBufferSession(), RADIOLIB_LORAWAN_SESSION_BUF_SIZE);
  p.end();
}

void linkForgetSession() {
  Preferences p;
  p.begin("lorawan", false);
  p.remove("session");
  p.end();
  node.clearSession();
  joined = false;
}

void linkSleep() {
  if (radioOk) radio.sleep();
}

bool linkRequestLinkCheck() {
  if (!radioOk || !joined) return false;
  return node.sendMacCommandReq(RADIOLIB_LORAWAN_MAC_LINK_CHECK) == RADIOLIB_ERR_NONE;
}

bool linkLinkCheckAns(uint8_t* margin, uint8_t* gw) {
  // RadioLib vacía los MAC del downlink al empezar cada uplink: solo vale tras un uplink con respuesta
  return radioOk && node.getMacLinkCheckAns(margin, gw) == RADIOLIB_ERR_NONE;
}
