#pragma once
#include <stdint.h>

// Emulación de la cobertura. Si devuelve false, la trama se registra como "suppressed": el nodo la da por
// enviada (como una trama que se pierde) pero la radio no transmite y no se gasta airtime.
//   C1: cobertura continua
//   C2: visita diaria: cobertura durante `window` s al principio de cada ciclo de `cycle` s
//   C3: pérdidas en ráfaga (Gilbert-Elliott) con pérdida media `geloss` % y ráfaga media `geburst` tramas
//   C4: corte de `outlen` s que empieza `outstart` s después del inicio del experimento
//   C5: igual que C4; el reinicio durante el corte y la B reducida los aplica el experimento
// tExpMs es el tiempo desde el inicio del experimento (comando "exp"), que se conserva entre reinicios.
bool coverageAvailable(uint64_t tExpMs, uint32_t txIndex);
