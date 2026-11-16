#pragma once
#include <Arduino.h>

// Buffer de bins cerrados, en orden de cierre, persistente en flash (LittleFS) para que sobreviva a
// reinicios y apagados. El estado de cada bin depende de su posición:
//   [0, acked)      confirmados por ACK (S3)
//   [acked, sent)   enviados al menos una vez
//   [sent, count)   pendientes de enviar
// Los bins consecutivos de una misma época forman un segmento con su propia base de tiempos.

struct BinView {
  uint8_t epoch;
  uint16_t seq;
  uint16_t steps;
  uint16_t deltaS;
  uint64_t tStartMs;  // inicio del bin en el reloj del nodo
  bool timeValid;     // false si el bin es de una base de tiempos anterior (arranque en frío)
};

struct PushResult {
  bool lost;          // se ha descartado un bin que no había llegado a enviarse (o a confirmarse en S3)
  uint8_t epoch;      // primer bin descartado
  uint16_t seq;
};

void bufBegin(uint16_t capacity);
PushResult bufPush(uint8_t epoch, uint16_t seq, uint16_t deltaS, uint64_t tStartMs, uint16_t steps, uint8_t strategy);

uint16_t bufCount();
uint16_t bufSent();
uint16_t bufAcked();
uint16_t bufSentHigh();   // hasta dónde se ha enviado alguna vez (detecta retransmisiones)
BinView bufAt(uint16_t idx);
uint16_t bufSegmentStart(uint16_t idx);
uint16_t bufSegmentEnd(uint16_t idx);   // exclusivo

void bufMarkSent(uint16_t end);
int bufAck(uint8_t epoch, uint16_t seq);   // índice confirmado o -1 si no está en el buffer
void bufAckUpTo(uint16_t end);             // S4: confirma los bins [0, end)
uint16_t bufRewind();                       // S3: vuelve a pendientes los no confirmados
void bufRelease(uint8_t strategy, uint8_t k);
bool bufOverflowPending();
void bufClearOverflow();
void bufClear();
void bufSave();                             // escribe en flash si hay cambios
