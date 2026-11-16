#pragma once
#include <Arduino.h>

// Pantalla OLED integrada (SSD1306, bus I2C interno). Muestra el estado del nodo; con display = 1 se
// enciende 30 s al arrancar o con una pulsación corta del botón, para no gastar batería.
// Dos vistas: la principal (pasos, cobertura y datos pendientes, en inglés para las fotos del artículo)
// y la técnica (estrategia, escenario, época, bin y última trama). Una pulsación corta con la pantalla
// encendida cambia de vista; con la pantalla apagada la enciende en la vista principal.

struct UiStatus {
  uint8_t strategy;
  uint8_t scenario;
  uint8_t epoch;
  bool sensor;          // fuente: BNO085 (true) o patrón sintético
  bool joined;
  uint32_t fcnt;
  uint16_t openSeq;     // bin abierto
  uint16_t pending;     // bins sin enviar
  uint32_t cumulative;
  const char* lastTx;   // estado de la última trama, o nullptr si no hay
  uint32_t lastTxAgoS;
  const char* gps;      // estado corto del GPS, o nullptr si está apagado
  bool checkOn;         // comprobación de cobertura activa (comando "check")
  uint8_t cov;          // 0 sin datos todavía, 1 la red contestó la última vez, 2 la última comprobación no
  uint32_t replyAgoS;   // segundos desde la última respuesta de la red (UINT32_MAX = nunca)
  uint8_t gw;           // gateways de la última respuesta LinkCheck (0 = desconocido)
};

void uiBegin(uint8_t mode, bool flip);
void uiSetMode(uint8_t mode);
void uiSetFlip(bool flip);   // true = imagen girada 180 grados
void uiWake();
void uiButton();             // pulsación corta del botón
void uiMessage(const char* title, const char* line = nullptr);
void uiUpdate(const UiStatus& s);   // refresca como mucho una vez por segundo
void uiSleep();
