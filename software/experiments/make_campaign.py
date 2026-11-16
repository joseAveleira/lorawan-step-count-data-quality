"""Genera los planes de la campaña de experimentos en experiments/plans/campaign/ y el orden de ejecución.

Escala del laboratorio: Δ = 60 s equivale a 15 min (compresión 15x), así que un día son 5760 s y el periodo de
transmisión de 240 s equivale a 1 h. Es la compresión máxima compatible con la fair use policy de TTN (unos
26 s de airtime al día con DR5). Cada escenario usa la misma semilla para todas las estrategias, de modo que
todas ven la misma actividad (comparaciones pareadas).

Uso:
    python experiments/make_campaign.py
"""
import json
import pathlib

DAY = 5760          # 24 h a escala real
PERIOD = 240        # 1 h a escala real
OUT_START = 1800    # 7,5 h a escala real
DEVADDR = "260BA8DC"
HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "plans" / "campaign"

BASE = ["set scenario 1", "set period 240", "set fup 1", "set dr 5", "set adr 0", "set k 4", "set n 40",
        "set delta 60", "set source 0", "set buf 1440", "set display 1"]
TEARDOWN = ["set strategy 1", "set scenario 1", "set buf 1440"]


DRAIN = 1800  # vaciado final con cobertura continua (7,5 h reales)


def plan(pid, desc, strategy, seed, duration, extra=(), actions=(), tier=1, drain=0):
    """Con drain, el escenario termina a los `duration` s y sigue un vaciado con cobertura continua para que lo
    pendiente pueda llegar; las métricas solo cuentan los bins generados antes (eval_s)."""
    actions = list(actions)
    if drain:
        actions.append({"at": duration, "node": "set scenario 1", "note": "vaciado con cobertura continua"})
    return dict(id=pid, description=desc, tier=tier, duration_s=duration + drain, eval_s=duration,
                devaddr=DEVADDR, setup=BASE + [f"set strategy {strategy}", f"set seed {seed}", *extra],
                actions=actions, teardown=TEARDOWN, ack_every=40 if strategy == 3 else 0)


def campaign():
    p = []
    S = [0, 1, 2, 3]
    # C1: cobertura continua, 2 días
    for s in S:
        p.append(plan(f"C1_S{s}", "Cobertura continua (control)", s, 1101, 2 * DAY))
    # C4: corte de 24 h a las 7,5 h, más 24 h de cobertura
    for s in S:
        p.append(plan(f"C4-24_S{s}", "Corte de 24 h", s, 1193, OUT_START + DAY + DAY,
                      ["set scenario 4", f"set outstart {OUT_START}", f"set outlen {DAY}"]))
    # C2: visita diaria de 2,5 h (tres envíos por visita), 3 días
    for s in S:
        p.append(plan(f"C2_S{s}", "Visita diaria de 2,5 h", s, 2599, 3 * DAY + 600,
                      ["set scenario 2", f"set cycle {DAY}", "set window 600"], drain=DRAIN))
    # C3: pérdidas en ráfaga del 30 % (L = 3), 2 días
    for s in S:
        p.append(plan(f"C3-30_S{s}", "Gilbert-Elliott, 30 % de pérdida, ráfaga media de 3 tramas", s, 1111, 2 * DAY,
                      ["set scenario 3", "set geloss 30", "set geburst 3"], drain=DRAIN))
    # C5: fallos del dispositivo con la S3
    p.append(plan("C5-reboot_S3", "Reinicio a mitad de un corte de 24 h", 3, 1193, OUT_START + 2 * DAY,
                  ["set scenario 5", f"set outstart {OUT_START}", f"set outlen {DAY}"],
                  [{"at": OUT_START + DAY // 2 + PERIOD // 2, "node": "reboot"}]))
    p.append(plan("C5-overflow_S3", "Corte de 72 h con B = 96 bins (24 h)", 3, 1144, OUT_START + 3 * DAY + DAY,
                  ["set scenario 5", f"set outstart {OUT_START}", f"set outlen {3 * DAY}", "set buf 96"]))
    # Validación con cortes reales: el gateway descarta los uplinks del nodo durante 24 h
    for s in (1, 3):
        p.append(plan(f"REAL-C4-24_S{s}", "Corte real de 24 h con el filtro DevAddr del gateway", s, 1193,
                      OUT_START + DAY + DAY, [],
                      [{"at": OUT_START, "gw": "block"}, {"at": OUT_START + DAY, "gw": "unblock"}]))
    # Segunda prioridad
    for s in S:
        p.append(plan(f"C3-10_S{s}", "Gilbert-Elliott, 10 % de pérdida, ráfaga media de 3 tramas", s, 1186, 2 * DAY,
                      ["set scenario 3", "set geloss 10", "set geburst 3"], tier=2, drain=DRAIN))
    # Validación corta de la S4 (uplinks confirmados de LoRaWAN): unos 6 ACK, dentro del límite diario de TTN.
    # La comparación completa con la S3 se hace en el simulador a escala de despliegue.
    p.append(plan("S4-valid", "S4 (uplinks confirmados): 3 tramas, corte de 3 tramas y recuperación", 4, 1193, 2400,
                  ["set scenario 4", "set outstart 720", "set outlen 720"], tier=2))
    for hours, seed in ((48, 1322), (72, 1079)):
        for s in (0, 3):
            p.append(plan(f"C4-{hours}_S{s}", f"Corte de {hours} h", s, seed, OUT_START + hours // 24 * DAY + DAY,
                          ["set scenario 4", f"set outstart {OUT_START}", f"set outlen {hours // 24 * DAY}"], tier=2))
    return p


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    plans = campaign()
    for pl in plans:
        (OUT / f"{pl['id']}.json").write_text(json.dumps(pl, indent=2, ensure_ascii=False), encoding="utf-8")
    order = [pl["id"] for pl in sorted(plans, key=lambda x: x["tier"])]
    (OUT / "order.txt").write_text("\n".join(order) + "\n", encoding="utf-8")
    hours = {t: sum(pl["duration_s"] for pl in plans if pl["tier"] == t) / 3600 for t in (1, 2)}
    print(f"{len(plans)} planes: prioridad 1 = {hours[1]:.1f} h, prioridad 2 = {hours[2]:.1f} h de laboratorio")


if __name__ == "__main__":
    main()
