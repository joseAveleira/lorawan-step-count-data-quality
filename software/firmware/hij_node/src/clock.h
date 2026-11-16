#pragma once
#include <stdint.h>

// Reloj del nodo en ms. Sigue contando durante el sueño profundo (apagado con el botón) y en los
// reinicios por software, pero vuelve a empezar tras un arranque en frío (alimentación o botón RST).
uint64_t nodeTimeMs();

// Identificador de la base de tiempos actual. Cambia en cada arranque en frío, de modo que los bins
// guardados con otra base de tiempos se pueden reconocer y enviar con edad desconocida.
uint32_t clockSession();

void clockBegin();   // al principio de setup()
void clockTouch();   // en cada vuelta de loop(): guarda el último instante para detectar saltos atrás
