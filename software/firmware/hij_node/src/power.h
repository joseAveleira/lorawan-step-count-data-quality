#pragma once
#include <stdint.h>

// Encendido y apagado con el botón PRG (GPIO 0).
//   Apagar:   mantener pulsado; a los POWER_HOLD_MS se enciende el LED y, al soltar, el nodo se apaga.
//   Encender: igual: mantener hasta que se encienda el LED y soltar (2 destellos al arrancar).
//   Pulsación corta: enciende la pantalla un rato.
// Una pulsación de más de POWER_STUCK_MS no cuenta: el GPIO 0 también está conectado a las líneas DTR/RTS
// del CP2102, que lo dejan en bajo cuando Windows suspende el puerto USB.
// Apagado = sueño profundo con la radio, el BNO085 y el GPS dormidos y Vext cortada.

void powerBootCheck();     // primera llamada de setup(): si se viene de un apagado, decide si arrancar
bool powerWokeByButton();  // el arranque actual es un encendido con el botón
void powerBegin();         // tarea que vigila el botón
bool powerOffRequested();
bool powerTakeShortPress();
[[noreturn]] void powerSleep();  // con los periféricos ya dormidos

void gpsSleep();           // el GPS no se usa: modo backup (UBX-RXM-PMREQ)
void ledBlink(uint8_t times, uint16_t onMs = 60, uint16_t offMs = 140);
