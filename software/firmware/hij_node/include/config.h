#pragma once

#define FW_VERSION "0.5.0"

// ---------------------------------------------------------------------------
// Pines de la Heltec WiFi LoRa 32 V3
// ---------------------------------------------------------------------------
#define PIN_LORA_NSS    8
#define PIN_LORA_DIO1   14
#define PIN_LORA_RST    12
#define PIN_LORA_BUSY   13
#define LORA_TCXO_V     1.8f

#define PIN_BNO_SDA     47
#define PIN_BNO_SCL     48
#define BNO_ADDR_A      0x4A
#define BNO_ADDR_B      0x4B    // dirección del módulo de este nodo

#define PIN_GPS_RX      19      // TX del GPS
#define PIN_GPS_TX      20      // RX del GPS
#define GPS_BAUD        9600

#define PIN_BUTTON      0       // botón PRG, activo en bajo
#define PIN_LED         35
#define PIN_VEXT        36      // alimentación de la OLED y del pin Ve; LOW = encendida

#define PIN_OLED_SDA    17
#define PIN_OLED_SCL    18
#define PIN_OLED_RST    21

// ---------------------------------------------------------------------------
// Encendido y apagado con el botón PRG
// ---------------------------------------------------------------------------
#define POWER_HOLD_MS   3000    // pulsación mínima para apagar o encender
#define POWER_STUCK_MS  10000   // a partir de aquí no es una pulsación sino la línea bloqueada por el USB

// ---------------------------------------------------------------------------
// Valores por defecto de la configuración (se cambian por serie y se guardan en NVS)
// ---------------------------------------------------------------------------
#define DEF_STRATEGY    1       // 0 = S0, 1 = S1, 2 = S2, 3 = S3
#define DEF_SCENARIO    1       // C1 a C5
#define DEF_DELTA_S     60      // duración del bin: 60 s en laboratorio (compresión 15x)
#define DEF_PERIOD_S    240     // periodo de transmisión
#define DEF_K           4       // S2: bins ya enviados que se repiten (4 = la trama anterior entera)
#define DEF_N           40      // S3: uplinks entre ACK (máximo 10 downlinks al día en TTN)
#define DEF_SEED        20261016L
#define DEF_SOURCE      0       // 0 = patrón sintético, 1 = BNO085
#define DEF_DR          5       // data rate fijo (DR5 = SF7)
#define DEF_ADR         0
#define DEF_FUP         1       // limitar el airtime a la fair use policy de TTN
#define DEF_BUF_BINS    1440    // capacidad B del buffer
#define DEF_DISPLAY     1       // pantalla: 0 = apagada, 1 = 30 s al arrancar o pulsar el botón, 2 = siempre
#define DEF_FLIP        0       // 1 = imagen girada 180 grados (hasta la 0.4.0 iba siempre girada)

// Escenarios (en segundos de laboratorio; con compresión 15x, 96 min equivalen a 24 h)
#define DEF_CYCLE_S     5760    // C2: una visita cada 24 h
#define DEF_WINDOW_S    480     // C2: 2 h de cobertura en cada visita
#define DEF_GE_LOSS     10      // C3: 10 % de pérdida media
#define DEF_GE_BURST    3       // C3: ráfagas de 3 tramas de media
#define DEF_OUT_START_S 1800    // C4: el corte empieza a las 7,5 h
#define DEF_OUT_LEN_S   5760    // C4: corte de 24 h

#define MAX_BUF_BINS    2880
#define MAX_SEGMENTS    8       // épocas distintas que puede haber a la vez en el buffer
#define FUP_MS_PER_HOUR 1250    // 30 s de airtime al día
#define FUP_MAX_WAIT_MS 15000   // si el límite bloquea menos que esto, se espera y se transmite

// Patrón sintético: bloques de ACT_BLOCK_BINS bins, activos o en reposo
#define ACT_BLOCK_BINS  30

// FPort de las tramas de datos: FPORT_BASE + estrategia (10 = S0 ... 13 = S3, 14 = S4)
#define FPORT_BASE      10
#define FPORT_ACK       20      // downlink de ACK de la S3: época (1 B) y secuencia (2 B)
