// Payload formatter de TTN (uplink) para el nodo hij_node. Se configura a nivel de dispositivo.
// Payload v2 (big-endian): flags (1) | época (1) | s1 (2) | n (1) | edad (2) | acumulado (4) | bins (2n)
// FPort 10 a 14 = estrategia S0 a S4. En la S0 el único "bin" son los pasos desde el último envío.
function decodeUplink(input) {
  var b = input.bytes;
  var port = input.fPort;
  if (port < 10 || port > 14) {
    return { errors: ["FPort " + port + " no pertenece al payload v2"] };
  }
  if (b.length < 11) {
    return { errors: ["trama demasiado corta: " + b.length + " bytes"] };
  }
  var flags = b[0];
  var n = b[4];
  if (b.length < 11 + 2 * n) {
    return { errors: ["faltan bins: n = " + n + ", " + b.length + " bytes"] };
  }
  var bins = [];
  for (var k = 0; k < n; k++) {
    bins.push((b[11 + 2 * k] << 8) | b[12 + 2 * k]);
  }
  return {
    data: {
      strategy: "S" + (port - 10),
      epoch: b[1],
      s1: (b[2] << 8) | b[3],
      n: n,
      age_units: (b[5] << 8) | b[6],
      cumulative_steps: ((b[7] << 24) >>> 0) + (b[8] << 16) + (b[9] << 8) + b[10],
      bins: bins,
      flags: {
        time_sync: (flags & 0x01) !== 0,
        overflow: (flags & 0x02) !== 0,
        low_battery: (flags & 0x04) !== 0,
        sensor_fail: (flags & 0x08) !== 0,
        retransmission: (flags & 0x10) !== 0,
        epoch_end: (flags & 0x20) !== 0
      }
    }
  };
}
