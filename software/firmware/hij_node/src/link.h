#pragma once
#include <Arduino.h>

// Enlace LoRaWAN con RadioLib: OTAA en EU868 con LoRaWAN 1.0.x. Los nonces y la sesión se guardan en NVS,
// así que tras un reinicio o un apagado se restaura la sesión sin repetir el join.

struct LinkResult {
  int16_t state;      // < 0 error de RadioLib; 0 sin downlink; > 0 ventana en la que llegó un downlink
  bool acked;         // uplink confirmado y la red ha devuelto el ACK de LoRaWAN
  uint32_t fcnt;
  uint8_t dr;
  uint32_t toaMs;     // airtime de la trama
  uint8_t downPort;
  size_t downLen;
  uint8_t down[256];
};

bool linkBegin();
bool linkReady();         // radio inicializada
bool linkJoined();
int16_t linkJoin();       // un intento (bloquea unos 6 s si hay que hacer join por radio)
bool linkWasRestored(int16_t state);
void linkApplySettings(); // DR, ADR y límite de airtime según cfg
LinkResult linkSend(const uint8_t* data, size_t len, uint8_t fport, bool confirmed = false);
uint8_t linkMaxPayload();
uint32_t linkTimeUntilUplinkMs();  // espera que impone el límite de airtime (0 = se puede transmitir)
void linkSaveSession();
void linkForgetSession(); // obliga a hacer un join nuevo
void linkSleep();
bool linkRequestLinkCheck();                          // pide LinkCheckReq en el siguiente uplink
bool linkLinkCheckAns(uint8_t* margin, uint8_t* gw);  // respuesta llegada en las ventanas del último uplink
