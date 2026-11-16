#pragma once
#include <Arduino.h>

// Configuración del experimento. Se guarda en NVS y se cambia por serie con "set <clave> <valor>".
struct Settings {
  uint8_t strategy;   // strategy: 0 = S0 ... 3 = S3, 4 = S4 (uplinks confirmados de LoRaWAN)
  uint8_t scenario;   // scenario: 1 = C1 ... 5 = C5
  uint16_t deltaS;    // delta: duración del bin (s)
  uint16_t periodS;   // period: periodo de transmisión (s)
  uint8_t k;          // k: bins repetidos en la S2
  uint8_t n;          // n: uplinks entre ACK en la S3
  uint32_t seed;      // seed: semilla del patrón sintético
  uint8_t source;     // source: 0 = sintético, 1 = BNO085
  uint8_t dr;         // dr: data rate fijo
  uint8_t adr;        // adr: 1 = ADR
  uint8_t fup;        // fup: 1 = limitar a la fair use policy de TTN
  uint16_t bufBins;   // buf: capacidad B del buffer
  uint8_t display;    // display: 0 = apagada, 1 = 30 s al arrancar o pulsar el botón, 2 = siempre
  uint32_t cycle;     // cycle: C2, periodo de las visitas (s)
  uint32_t window;    // window: C2, ventana de cobertura en cada visita (s)
  uint8_t geLoss;     // geloss: C3, pérdida media (%)
  uint8_t geBurst;    // geburst: C3, longitud media de ráfaga (tramas)
  uint32_t outStart;  // outstart: C4 y C5, inicio del corte desde el inicio del experimento (s)
  uint32_t outLen;    // outlen: C4 y C5, duración del corte (s)
  uint8_t flip;       // flip: 1 = pantalla girada 180 grados
};

extern Settings cfg;

void settingsLoad();

// Cambia un parámetro y lo guarda. Devuelve 0 si se aplica ya, 1 si se aplica en el próximo arranque
// (los que definen la época: delta, seed, source y buf) y -1 si la clave o el valor no son válidos.
int settingsSet(const char* key, long value);

void settingsLog();  // línea EV,config con todos los valores
