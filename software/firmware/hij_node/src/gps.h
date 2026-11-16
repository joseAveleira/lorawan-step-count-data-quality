#pragma once
#include <Arduino.h>

// GPS NEO-6M por UART (pines y velocidad como en el firmware del CBMS). Solo para pruebas de campo propias:
// con el comando "gps 1" se despierta y cada trama lleva al final su última posición (FLAG_GPS). No se guarda
// en NVS: cada arranque lo deja dormido, así que la campaña no lo ve nunca encendido.

#define GPS_TRAILER_LEN 11

void gpsStart();                    // lo saca del modo backup y empieza a leer NMEA
void gpsStop();                     // vuelve al modo backup
bool gpsActive();
void gpsPoll();                     // lee las frases NMEA pendientes (GGA)
size_t gpsTrailer(uint8_t* out);    // lat, lon (int32, 1e-6 grados), satélites, HDOP x10, antigüedad (s; 255 = sin posición)
void gpsLog();                      // EV,gps,...
const char* gpsShort();             // texto corto para la pantalla, o nullptr si está apagado

struct GpsState {
  int32_t lat;      // 1e-6 grados (0 si nunca ha tenido posición)
  int32_t lon;
  uint8_t sats;
  uint8_t fix;      // 1 = la última frase GGA tenía posición
  uint8_t hdop10;   // 255 = desconocido
  uint8_t ageS;     // antigüedad de la última posición en s (255 = ninguna; 254 = 254 s o más)
};
GpsState gpsState();
