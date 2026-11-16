#pragma once
#include <Arduino.h>

// Registro de la ruta (pruebas de campo propias): con el GPS encendido, cada minuto se añade un registro de
// tamaño fijo a /track.bin en LittleFS. Al llenarse deja de escribir (no sobrescribe). El comando "track" lo
// vuelca por serie como líneas TRK precedidas de EV,now (ancla entre el reloj del nodo y la hora del PC).

#define TRACK_MAX_RECORDS 4000   // unos 2,8 días a un registro por minuto (96 KB de los 1,5 MB de LittleFS)

struct __attribute__((packed)) TrackRecord {
  uint8_t epoch;
  uint32_t session;   // base de tiempos del reloj del nodo (clockSession): cambia en cada arranque en frío
  uint32_t tS;        // reloj del nodo en segundos
  int32_t lat;        // 1e-6 grados
  int32_t lon;
  uint8_t sats;
  uint8_t fix;        // 1 = la última frase GGA tenía posición
  uint8_t hdop10;     // 255 = desconocido
  uint8_t ageS;       // antigüedad de la posición en s (255 = ninguna todavía)
  uint8_t cov;        // cobertura en ese minuto: 0 desconocida, 1 respuesta de la red, 2 comprobación sin respuesta
  uint8_t gw;         // gateways de la última respuesta LinkCheck (0 = desconocido)
  uint8_t margin;     // margen en dB de la última respuesta LinkCheck
};

void trackBegin();                       // tras montar LittleFS (bufBegin)
bool trackAppend(const TrackRecord& r);  // false si está lleno o falla la escritura
uint16_t trackCount();
void trackDump();                        // EV,now,<ms>,<session> + TRK,... + EV,track,<n>
void trackClear();
