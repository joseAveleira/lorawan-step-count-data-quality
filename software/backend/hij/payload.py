"""Decodificación del payload v2 del nodo (mismo formato que firmware/hij_node/src/payload.h).

Cabecera de 11 bytes, big-endian: flags (1) | época (1) | s1 (2) | n (1) | edad (2) | acumulado (4),
seguida de n bins de 2 bytes. FPort 10 + estrategia. En la S0 el único bin son los pasos desde el último
envío, que cubren los bins s1 en adelante.
"""
from dataclasses import dataclass, field

FPORT_BASE = 10
FPORT_ACK = 20
HDR_LEN = 11
AGE_UNKNOWN = 0xFFFF

FLAG_TIME_SYNC = 0x01
FLAG_OVERFLOW = 0x02
FLAG_LOW_BATT = 0x04
FLAG_SENSOR_FAIL = 0x08
FLAG_RETX = 0x10
FLAG_EPOCH_END = 0x20


@dataclass
class Frame:
    strategy: int
    flags: int
    epoch: int
    s1: int
    n: int
    age_units: int
    cumulative: int
    bins: list = field(default_factory=list)

    @property
    def age_known(self) -> bool:
        return self.age_units != AGE_UNKNOWN

    def age_seconds(self, delta_s: int) -> float:
        """Edad del bin s1 en segundos (las unidades son Δ/15)."""
        return self.age_units * delta_s / 15.0

    def has(self, flag: int) -> bool:
        return bool(self.flags & flag)


def decode(payload: bytes, fport: int) -> Frame:
    if not FPORT_BASE <= fport <= FPORT_BASE + 4:
        raise ValueError(f"FPort {fport} no pertenece al payload v2")
    if len(payload) < HDR_LEN:
        raise ValueError(f"trama demasiado corta: {len(payload)} bytes")
    n = payload[4]
    if len(payload) < HDR_LEN + 2 * n:
        raise ValueError(f"faltan bins: n = {n}, {len(payload)} bytes")
    bins = [int.from_bytes(payload[HDR_LEN + 2 * k:HDR_LEN + 2 * k + 2], "big") for k in range(n)]
    return Frame(
        strategy=fport - FPORT_BASE,
        flags=payload[0],
        epoch=payload[1],
        s1=int.from_bytes(payload[2:4], "big"),
        n=n,
        age_units=int.from_bytes(payload[5:7], "big"),
        cumulative=int.from_bytes(payload[7:11], "big"),
        bins=bins,
    )


def encode_ack(epoch: int, seq: int) -> bytes:
    """Downlink de ACK de la S3 (FPort 20): última (época, secuencia) recibida sin huecos."""
    return bytes([epoch & 0xFF, (seq >> 8) & 0xFF, seq & 0xFF])
