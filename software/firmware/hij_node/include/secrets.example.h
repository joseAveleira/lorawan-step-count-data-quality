#pragma once

// Copiar como secrets.h (está en .gitignore) y rellenar con los datos del dispositivo en TTN.
// Activación OTAA con LoRaWAN 1.0.x: solo hace falta la AppKey.
#define LORAWAN_JOIN_EUI 0x0000000000000000ULL
#define LORAWAN_DEV_EUI  0x0000000000000000ULL
#define LORAWAN_APP_KEY  { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, \
                           0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 }
