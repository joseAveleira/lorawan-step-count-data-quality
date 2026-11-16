#pragma once
#include <stddef.h>
#include <stdint.h>

// Payload v2 (big-endian): cabecera de 11 bytes y 2 bytes por bin.
//   flags (1) | época (1) | s1 (2) | n (1) | edad del bin s1 en unidades de Δ/15 (2) | acumulado (4) | bins (2n)
// En la S0 (FPort 10) la trama lleva un único valor: los pasos desde el último envío, que cubren los bins
// s1 en adelante.
#define PAYLOAD_HDR_LEN 11
#define PAYLOAD_MAX_BINS 105

#define FLAG_TIME_SYNC 0x01    // hora sincronizada con la red
#define FLAG_OVERFLOW 0x02     // se han perdido bins por desbordamiento del buffer
#define FLAG_LOW_BATT 0x04
#define FLAG_SENSOR_FAIL 0x08
#define FLAG_RETX 0x10         // la trama incluye bins ya enviados
#define FLAG_EPOCH_END 0x20    // el último bin de la trama es el último de una época ya cerrada (reinicio)
#define FLAG_GPS 0x40          // tras los bins va la posición GPS (gps.h); solo en pruebas de campo propias

#define AGE_UNKNOWN 0xFFFF

size_t encodeFrame(uint8_t* out, uint8_t flags, uint8_t epoch, uint16_t s1, uint8_t n, uint16_t age,
                   uint32_t cumulative, const uint16_t* bins);
