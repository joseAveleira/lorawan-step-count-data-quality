#pragma once
#include <Arduino.h>

// Patrón de actividad sintético: pasos del intervalo k (número de intervalos Δ desde el inicio del
// experimento), función determinista de la semilla. Al no depender de la época, todas las estrategias de un
// mismo escenario ven la misma actividad (comparaciones pareadas), también si el nodo se reinicia.
uint16_t syntheticSteps(uint32_t seed, uint32_t k, uint16_t deltaS);

// Contador de pasos del BNO085 (SH2_STEP_COUNTER) en el segundo bus I2C
bool sensorBegin();
bool sensorOk();
uint8_t sensorAddr();
void sensorPoll();          // leer los eventos pendientes; llamar a menudo
uint32_t sensorSteps();     // pasos acumulados desde sensorBegin()
void sensorSleep();
