"""Patrón de actividad sintético del nodo (copia exacta de syntheticSteps en firmware/hij_node/src/activity.cpp).

Los pasos dependen de (semilla, k), con k = número de intervalos Δ desde el inicio del experimento, de modo
que todas las estrategias de un mismo escenario ven la misma actividad. Permite regenerar offline la verdad
terreno.
"""

ACT_BLOCK_BINS = 30
M32 = 0xFFFFFFFF


def mix32(x: int) -> int:
    """Mezclador "lowbias32" (C. Wellons) en aritmética de 32 bits sin signo."""
    x &= M32
    x ^= x >> 16
    x = (x * 0x7FEB352D) & M32
    x ^= x >> 15
    x = (x * 0x846CA68B) & M32
    x ^= x >> 16
    return x


def synthetic_steps(seed: int, k: int, delta_s: int) -> int:
    k &= M32  # en el nodo k es uint32_t (un k negativo, antes del inicio, se interpreta igual)
    base = mix32(seed)
    h_block = mix32(base ^ ((0x9E3779B9 * (k // ACT_BLOCK_BINS + 1)) & M32))
    if h_block % 100 >= 35:
        return 0
    h_bin = mix32(h_block ^ ((0x85EBCA6B * (k + 1)) & M32))
    if h_bin % 100 < 15:
        return 0
    per_min = 20 + (h_bin >> 8) % 91
    return min(per_min * delta_s // 60, 0xFFFF)
