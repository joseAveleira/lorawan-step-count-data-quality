"""Simulación de 30 días de seguimiento a escala real con pacientes virtuales.

Cada paciente virtual tiene su propio patrón de actividad (semilla), su hora de visita (fase respecto a los envíos
horarios del nodo, uniforme dentro de una hora) y su realización de las pérdidas. Las cuatro estrategias se simulan
con los mismos pacientes (comparación pareada). El seguimiento empieza en el alta, con cobertura (la primera visita
empieza dentro de la primera hora), y dura 30 días; después el nodo tiene cobertura continua hasta vaciar el
buffer, como el vaciado final del laboratorio. Así DC_end mide lo que se acaba recuperando y DC_followup lo que ya
ha llegado al terminar los 30 días; DC_1h y DC_24h se calculan solo dentro del seguimiento (bins que acaban al
menos h antes del día 30), para que el vaciado final no los cuente.

Parámetros a escala real (los del laboratorio sin comprimir): Δ = 15 min, un envío por hora, DR5, k = 4, N = 40,
B = 1440 bins (15 días), 30 s de airtime y 10 downlinks al día (fair use policy de TTN) y visitas de 2,5 h. Las
pérdidas durante las visitas siguen el modelo de ráfagas de C3 (sin pérdidas, como en el laboratorio, y con
pérdida media del 10 % y del 30 % y ráfagas de 3 tramas).

Columnas de sim30_runs.csv: las métricas de hij.quality.metrics (KEEP; en la S0, FZR_naive y FZR_aware con cada
trama sobre los bins que cubre y FZR_naive_lastbin con el incremento en el último bin), las del seguimiento
(followup_metrics), downlinks_per_day y el airtime:
    airtime_s_per_day      airtime recibido por la red: tramas 'sent' (las que llegan al backend), media de los 33
                           días simulados
    airtime_tx_s_per_day   airtime emitido por el nodo, media de los 33 días: tramas 'sent' y 'suppressed'. Como en
                           el firmware del laboratorio, el simulador no transmite las tramas sin cobertura ni las
                           perdidas en la visita (toa_ms = 0); en un despliegue el nodo las transmite igual. ToA desde
                           los bytes, truncado a ms como RadioLib
    airtime_tx_max_day_s   máximo del airtime emitido en un día (días de 24 h contados desde el alta)
    tx_limiter_conflicts   tramas que el límite de airtime de RadioLib habría retrasado si también se hubieran
                           transmitido las 'suppressed'. Con 0, el airtime emitido es exacto sin volver a simular
                           (a escala real la espera tras una trama llena es de 20 min y el periodo de 1 h)

Uso:
    python simulator/sim30.py [--patients 50] [--workers 8] [--buf 1440] [--strategies 0 1 2 3] [--suffix _B2880]
Guarda software/data/processed/sim30_runs<sufijo>.csv (una fila por simulación) y sim30_summary<sufijo>.csv
(mediana y cuartiles por patrón de visitas, pérdidas y estrategia).
"""
import argparse
import dataclasses
import os
import pathlib
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "backend"))
from hijsim import (DR_SF, EU868_DUTY_MS_PER_HOUR, LORAWAN_OVERHEAD, SimConfig, lora_toa_ms,  # noqa: E402
                    radiolib_fup_delay_ms, simulate)
from hij.quality import metrics, received_copies, s0_views  # noqa: E402

PROCESSED = HERE.parent / "data" / "processed"
DAY = 86400
FOLLOW_UP_DAYS, DRAIN_DAYS = 30, 3
PATTERNS = {"daily": 1, "every 2 days": 2, "twice weekly": 3.5, "weekly": 7}  # días entre visitas
LOSSES = [0, 10, 30]                                                             # % medio durante las visitas
STRATEGIES = [0, 1, 2, 3]
KEEP = ["bins_generated", "bins_received", "bins_exposed", "DC_1h", "DC_24h", "DC_end", "RR", "SR_bins",
        "SR_counter", "counter_gap_max_h_real", "TI", "time_err_med_s", "time_err_p95_s", "time_err_max_s", "DR",
        "OOR", "delay_med_h_real", "delay_p95_h_real", "delay_max_h_real", "FZR_naive", "FZR_aware",
        "FZR_naive_lastbin", "SR_increments", "S0_interval_coverage", "airtime_s_per_day", "frames_node_dropped"]
AIRTIME = ["airtime_tx_s_per_day", "airtime_tx_max_day_s", "tx_limiter_conflicts"]


def patient(i: int) -> dict:
    """Semilla y hora de visita del paciente virtual i (las mismas en todas las configuraciones)."""
    rng = np.random.default_rng(20261019 + i)
    return dict(patient=i, seed=int(rng.integers(1, 2**31 - 1)), visit_offset_s=int(rng.integers(0, 3600)))


def run(job: tuple) -> dict:
    pattern, loss, strategy, p, buf = job
    cfg = SimConfig(strategy=strategy, duration_s=(FOLLOW_UP_DAYS + DRAIN_DAYS) * DAY, eval_s=FOLLOW_UP_DAYS * DAY,
                    delta_s=900, period_s=3600, scenario=2, cycle=int(PATTERNS[pattern] * DAY), window=9000,
                    visit_offset_s=p["visit_offset_s"], loss_in_window=loss > 0, ge_loss=loss, ge_burst=3,
                    seed=p["seed"], buf=buf, label=f"{pattern}_L{loss}_S{strategy}_p{p['patient']}")
    exp = simulate(cfg)
    m = metrics(exp)
    row = dict(pattern=pattern, loss=loss, strategy=strategy, buf=buf, **p)
    row.update({k: m.get(k, np.nan) for k in KEEP})
    row["downlinks_per_day"] = int((exp.ev.type == "ack").sum()) / (FOLLOW_UP_DAYS + DRAIN_DAYS)
    row.update(followup_metrics(exp))
    row.update(airtime_emitted(exp, cfg))
    return row


def airtime_emitted(exp, cfg: SimConfig) -> dict:
    """Airtime emitido por el nodo (tramas 'sent' y 'suppressed'; ver el docstring del módulo)."""
    tx = exp.tx[exp.tx.status.isin(["sent", "suppressed"])].sort_values("t_ms", kind="stable")
    if tx.empty:
        return {"airtime_tx_s_per_day": 0.0, "airtime_tx_max_day_s": 0.0, "tx_limiter_conflicts": 0}
    sf = DR_SF[cfg.dr]
    toa = np.array([lora_toa_ms(b + LORAWAN_OVERHEAD, sf) for b in tx.bytes])   # ms, como el simulador
    toa_i = toa.astype(int)                                                       # truncado, como RadioLib
    t_ms = tx.t_ms.to_numpy(dtype="int64")
    per_day = pd.Series(toa_i).groupby(t_ms // (DAY * 1000)).sum()
    # espera que exige el límite de airtime tras cada trama (como next_allowed en hijsim), con 1 ms de margen por
    # el truncado de t_ms
    ms_per_hour = cfg.fup_ms_per_hour or EU868_DUTY_MS_PER_HOUR
    need_ms = toa[:-1] + np.array([radiolib_fup_delay_ms(ms_per_hour, int(x)) for x in toa_i[:-1]])
    conflicts = int((np.diff(t_ms) + 1 < need_ms).sum())
    return {"airtime_tx_s_per_day": float(toa_i.sum()) / 1000 / (FOLLOW_UP_DAYS + DRAIN_DAYS),
            "airtime_tx_max_day_s": float(per_day.max()) / 1000,
            "tx_limiter_conflicts": conflicts}


def followup_metrics(exp) -> dict:
    """Métricas del seguimiento, sin el vaciado final: DC_followup (recibido antes de acabar el día 30), DC(1 h) y
    DC(24 h) sobre los bins que acaban al menos h antes del día 30, para que el vaciado no los cuente (sustituyen a
    los de metrics(), que miden hasta el final de la simulación), y FZR_naive_followup: falsos ceros de la vista
    ingenua tal como la vería el clínico el día 30 (lo no recibido se pinta como 0; en la S0, cada trama recibida
    hasta entonces sobre los bins que cubre, como FZR_naive de hij.quality)."""
    cut = exp.exp_start + pd.Timedelta(seconds=FOLLOW_UP_DAYS * DAY)
    gt = exp.gt[exp.gt.t_end <= cut]
    c = received_copies(exp)
    c = c.sort_values("arrival") if not c.empty else c
    first = c.groupby(["epoch", "seq"]).arrival.min().to_dict() if not c.empty else {}
    value = c.groupby(["epoch", "seq"]).value.first().to_dict() if not c.empty else {}
    keys = list(zip(gt.epoch, gt.seq))
    arrival = pd.Series([first.get(k, pd.NaT) for k in keys], index=gt.index, dtype=gt.t_end.dtype)
    delay = (arrival - gt.t_end).dt.total_seconds()
    out = {"DC_followup": float((arrival <= cut).mean()) if len(gt) else np.nan}
    for h in (1, 24):
        sel = gt.t_end <= cut - pd.Timedelta(hours=h)
        out[f"DC_{h}h"] = float((delay[sel] <= h * 3600).mean()) if sel.any() else np.nan
    active = gt.steps.to_numpy() > 0
    if not len(gt):
        out["FZR_naive_followup"] = np.nan
        return out
    if exp.config.get("strategy") == 0:
        up = exp.uplinks
        early = dataclasses.replace(exp, uplinks=up[up.received_at <= cut]) if len(up) else exp
        covered, increment, _ = s0_views(early, gt)
        zeros = ~covered | (increment == 0)
    else:
        got = (arrival <= cut).to_numpy()
        zeros = np.where(got, [value.get(k, 0) for k in keys], 0) == 0
    out["FZR_naive_followup"] = float((active & zeros).sum() / zeros.sum()) if zeros.any() else np.nan
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patients", type=int, default=50)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--buf", type=int, default=1440, help="capacidad B del buffer (bins)")
    ap.add_argument("--strategies", type=int, nargs="*", default=STRATEGIES)
    ap.add_argument("--suffix", default="", help="sufijo de los ficheros de salida, p. ej. _B2880")
    args = ap.parse_args()
    jobs = [(pat, loss, s, patient(i), args.buf) for pat in PATTERNS for loss in LOSSES for s in args.strategies
            for i in range(args.patients)]
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(run, jobs, chunksize=8))
    df = pd.DataFrame(rows)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    df.to_csv(PROCESSED / f"sim30_runs{args.suffix}.csv", index=False)

    cols = KEEP + ["downlinks_per_day", "DC_followup", "FZR_naive_followup"] + AIRTIME
    g = df.groupby(["pattern", "loss", "strategy"], sort=False)[cols]
    summary = pd.concat({"median": g.median(), "q25": g.quantile(0.25), "q75": g.quantile(0.75)}, axis=1)
    summary.to_csv(PROCESSED / f"sim30_summary{args.suffix}.csv")
    show = ["DC_24h", "DC_followup", "DC_end", "FZR_naive", "FZR_naive_followup", "FZR_aware", "delay_med_h_real",
            "airtime_s_per_day", "airtime_tx_s_per_day", "downlinks_per_day"]
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 20)
    print(f"{len(df)} simulaciones ({args.patients} pacientes virtuales por configuración); medianas:")
    print(summary["median"][show].round(2).to_string())
    print(f"Máximos (media de {FOLLOW_UP_DAYS + DRAIN_DAYS} días): airtime recibido "
          f"{df.airtime_s_per_day.max():.3f} s/día, emitido {df.airtime_tx_s_per_day.max():.3f} s/día; emitido en un "
          f"día {df.airtime_tx_max_day_s.max():.3f} s; downlinks {df.downlinks_per_day.max():.3f} al día")
    bad = int((df.tx_limiter_conflicts > 0).sum())
    if bad:
        print(f"AVISO: en {bad} simulaciones el límite de airtime habría retrasado alguna trama si se hubieran "
              "transmitido las tramas sin cobertura: airtime_tx_s_per_day es una aproximación")


if __name__ == "__main__":
    main()
