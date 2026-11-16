"""Validación del simulador: reproduce cada experimento terminado del laboratorio y lo compara con el real.

Para cada experimento con DONE en data/raw se simula su plan (mismos parámetros que el nodo) con los instantes
medidos en esa prueba, en el reloj del nodo y desde el inicio del experimento (EV,exp,start):
    - adelanto de los envíos (tx_lead_s): el nodo ancla sus bins y sus envíos a su arranque (o al 'clear'), que es
      anterior al inicio del experimento. Se toma el mayor adelanto de las tramas del primer arranque, porque el
      límite de airtime solo puede retrasar un envío (en la S2 del laboratorio, unos 16 ms por trama);
    - reinicios: inicio de cada época nueva (primer bin del arranque, hij.experiment.boot_starts);
    - cortes reales: filtro del gateway entre las líneas "# gw block" y "# gw unblock" de node.log, con las tramas
      transmitidas y no recibidas (hijsim, gw_block);
    - final: la simulación acaba cuando acabó el bucle de run.py (línea "# cmd dump"). Las tramas posteriores no
      llegan a node.log, y run.py mide la duración con el reloj del PC, que puede saltar: en C2_S2 la prueba acabó
      2,7 s antes que el plan y perdió su última trama. La ventana de observación de las métricas (Experiment.end,
      último registro de la prueba) se copia de la prueba real.
La verdad terreno real va en el reloj del gateway (hij.experiment.load_experiment). Se comparan:
    - la verdad terreno: pasos de cada bin (época relativa, secuencia);
    - las tramas, una a una: primer bin, número de bins, bytes y estado (sent, suppressed, dropped), el instante de
      envío en el reloj del nodo (tx_time_diff_s_med y _max) y el airtime (toa_diff_frames, toa_diff_ms: tramas
      reales más largas por llevar la respuesta a un comando MAC de la red, que el simulador no modela);
    - las métricas de calidad, calculadas con el mismo código (backend/hij/quality.py), y los downlinks (ACK de la S3
      y confirmaciones de la S4).

Uso:
    python simulator/validate_lab.py [ids...]
Guarda software/data/processed/sim_validation.csv.
"""
import json
import pathlib
import sys

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "backend"))
from hijsim import simulate  # noqa: E402
from preview_campaign import plan_to_config  # noqa: E402
from hij.experiment import Experiment, boot_starts, load_experiment  # noqa: E402
from hij.quality import metrics  # noqa: E402

RAW, PROCESSED = HERE.parent / "data" / "raw", HERE.parent / "data" / "processed"
COMPARE = ["DC_1h", "DC_24h", "DC_end", "RR", "SR_bins", "SR_counter", "TI", "DR", "OOR", "delay_med_h_real",
           "delay_p95_h_real", "delay_max_h_real", "FZR_naive", "FZR_aware", "FZR_naive_lastbin", "SR_increments",
           "airtime_s_per_day"]


def relative_epochs(df: pd.DataFrame) -> pd.Series:
    """Época contada desde la primera del experimento (el nodo real arrastra la de arranques anteriores)."""
    return (df.epoch - df.epoch.iloc[0]) & 0xFF if len(df) else df.epoch


def node_ms(exp: Experiment, utc) -> float:
    """Instante en el reloj de referencia (UTC) -> ms del reloj del nodo: inversa de Experiment.node_to_utc."""
    u = pd.Timestamp(utc).timestamp()
    if not exp.clock_fit:
        return (u - exp.node_offset_s) * 1000
    b, seg = exp.clock_drift, exp.clock_fit[0]
    for s in exp.clock_fit:   # tramos en orden de arranque: el último que empieza antes del instante
        if s["y_s"] + b * (s["start_ms"] / 1000 - s["x_s"]) <= u:
            seg = s
    return (seg["x_s"] + (u - seg["y_s"]) / b) * 1000


def exp_start_ms(real: Experiment) -> int:
    """Inicio del experimento en el reloj del nodo (última línea EV,exp,start, como load_experiment)."""
    starts = real.ev[(real.ev.type == "exp") & real.ev.detail.str.startswith("start,")]
    return int(starts.iloc[-1].detail.split(",")[1])


def loop_end_host(real: Experiment) -> pd.Timestamp | None:
    """Hora del PC de la última línea "# cmd dump" de node.log: fin del bucle de run.py."""
    end = None
    for line in (real.path / "node.log").read_text(encoding="utf-8").splitlines():
        host, _, rec = line.partition("\t")
        if rec.strip() == "# cmd dump":
            t = pd.Timestamp(host)
            if real.host_start is None or t >= real.host_start:
                end = t
    return end


def measured_schedule(real: Experiment, period_s: int) -> dict:
    """Instantes de la prueba real en s del reloj del nodo desde el inicio del experimento (ver el docstring)."""
    start = exp_start_ms(real)

    def rel_s(utc) -> float:
        return (node_ms(real, utc) - start) / 1000

    boots = boot_starts(real.gt)
    second_boot = boots.start_ms.iloc[1] if len(boots) > 1 else np.inf
    first = real.tx[real.tx.t_ms < second_boot]
    off = (first.t_ms.to_numpy(dtype="float64") - start) / 1000
    lead = float(np.max(np.round(off / period_s) * period_s - off)) if len(off) else 0.5
    reboots = tuple(float((ms - start) / 1000) for ms in boots.start_ms.iloc[1:])
    end_obs = rel_s(real.end)
    dump = loop_end_host(real)
    end_tx = rel_s(real.host_to_utc([dump])[0]) if dump is not None else end_obs
    blocks, opened = [], None
    for a in (real.gw_actions.sort_values("time").itertuples() if not real.gw_actions.empty else []):
        if a.action == "block" and opened is None:
            opened = rel_s(a.time)
        elif a.action == "unblock" and opened is not None:
            blocks.append((opened, rel_s(a.time)))
            opened = None
    if opened is not None:
        blocks.append((opened, end_obs))
    return dict(start_ms=start, tx_lead_s=lead, reboots_at=reboots, end_tx_s=end_tx, end_obs_s=end_obs,
                gw_block=tuple(blocks))


def downlinks_real(real: Experiment) -> int:
    """Downlinks recibidos por el nodo desde el inicio (EV,ack de la S3 y EV,confirm con acked = 1 de la S4)."""
    ev = real.ev[real.ev.host_time >= real.host_start] if real.host_start is not None else real.ev
    confirmed = ev.loc[ev.type == "confirm", "detail"].str.split(",").str[-1]
    return int((ev.type == "ack").sum()) + int((confirmed == "1").sum())


def compare(pid: str) -> dict:
    real = load_experiment(RAW / pid)
    plan = json.loads((RAW / pid / "plan.json").read_text(encoding="utf-8"))
    cfg = plan_to_config(plan, ack_same_window=True)
    sched = measured_schedule(real, cfg.period_s)
    cfg.tx_lead_s = sched["tx_lead_s"]
    if sched["reboots_at"] or cfg.reboots_at:
        cfg.reboots_at = sched["reboots_at"]
    if sched["gw_block"] or cfg.gw_block:
        cfg.gw_block = sched["gw_block"]
    cfg.duration_s = min(cfg.duration_s, sched["end_tx_s"])
    sim = simulate(cfg)
    sim.end = sim.exp_start + pd.Timedelta(seconds=max(sched["end_obs_s"], cfg.duration_s))  # misma observación

    # verdad terreno: solo los bins que el nodo cerró durante la prueba. Un bin que se cierra mientras el nodo
    # vuelca su memoria tras el final del bucle (línea GT posterior a "# cmd dump"; pasa en C5-reboot_S3) no es de la
    # prueba y el simulador, que acaba en el dump, no lo genera.
    dump = loop_end_host(real)
    gt_run = real.gt[real.gt.host_time < dump] if dump is not None else real.gt
    g_real = gt_run.assign(rel=relative_epochs(gt_run)).set_index(["rel", "seq"]).steps
    g_sim = sim.gt.assign(rel=relative_epochs(sim.gt)).set_index(["rel", "seq"]).steps
    common = g_real.index.intersection(g_sim.index)
    out = dict(id=pid, clock_ref=real.clock_ref, tx_lead_s=cfg.tx_lead_s, duration_plan_s=plan["duration_s"],
               duration_sim_s=cfg.duration_s, end_obs_s=sched["end_obs_s"],
               reboots_at=";".join(f"{x:.3f}" for x in cfg.reboots_at),
               gw_block=";".join(f"{a:.1f}-{b:.1f}" for a, b in cfg.gw_block),
               bins_real=len(g_real), bins_sim=len(g_sim), bins_same_steps=int((g_real[common] == g_sim[common]).sum()))

    # tramas, en orden
    cols = ["s1", "n", "bytes", "status"]
    real_tx, sim_tx = real.tx.sort_values("t_ms", kind="stable"), sim.tx.sort_values("t_ms", kind="stable")
    t_real = real_tx[cols].reset_index(drop=True)
    t_sim = sim_tx[cols].reset_index(drop=True)
    m = min(len(t_real), len(t_sim))
    same = (t_real.iloc[:m] == t_sim.iloc[:m]).all(axis=1)
    out.update(frames_real=len(t_real), frames_sim=len(t_sim), frames_same=int(same.sum()))
    diff = [i for i in range(m) if not same[i]]
    if not diff and len(t_real) != len(t_sim):
        diff = [m]
    out["first_diff"] = ""
    if diff:
        i = diff[0]
        r = tuple(t_real.iloc[i]) if i < len(t_real) else "ninguna"
        s = tuple(t_sim.iloc[i]) if i < len(t_sim) else "ninguna"
        out["first_diff"] = f"#{i}: real {r} / sim {s}"
    for st in ("sent", "suppressed", "dropped"):
        out[f"{st}_real"] = int((t_real.status == st).sum())
        out[f"{st}_sim"] = int((t_sim.status == st).sum())
    # instante de cada trama en el reloj del nodo, desde el inicio del experimento
    off_real = (real_tx.t_ms.to_numpy(dtype="float64") - sched["start_ms"]) / 1000
    off_sim = sim_tx.t_ms.to_numpy(dtype="float64") / 1000
    d = np.abs(off_real[:m] - off_sim[:m])
    out["tx_time_diff_s_med"] = float(np.median(d)) if m else float("nan")
    out["tx_time_diff_s_max"] = float(np.max(d)) if m else float("nan")
    # airtime de cada trama: el real es mayor cuando la trama lleva en FOpts la respuesta a un comando MAC de la red
    # (el simulador no los modela; en la campaña, 5 ms más en una trama de C1_S3, C2_S1 y C3-30_S0)
    toa_d = real_tx.toa_ms.to_numpy()[:m] - sim_tx.toa_ms.to_numpy()[:m]
    out["toa_diff_frames"] = int((toa_d != 0).sum())
    out["toa_diff_ms"] = int(toa_d.sum())

    # métricas
    mr, ms = metrics(real), metrics(sim)
    for k in COMPARE:
        out[f"{k}_real"], out[f"{k}_sim"] = mr.get(k), ms.get(k)
    out["downlinks_real"] = downlinks_real(real)
    out["downlinks_sim"] = int(sim.ev.type.isin(["ack", "confirm"]).sum())
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ids = sys.argv[1:] or sorted(p.parent.name for p in RAW.glob("*/DONE"))
    rows = [compare(pid) for pid in ids]
    df = pd.DataFrame(rows)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    df.to_csv(PROCESSED / "sim_validation.csv", index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 90)
    print(df[["id", "clock_ref", "tx_lead_s", "duration_plan_s", "duration_sim_s", "bins_real", "bins_sim",
              "bins_same_steps", "frames_real", "frames_sim", "frames_same", "tx_time_diff_s_med",
              "tx_time_diff_s_max", "toa_diff_frames", "toa_diff_ms", "downlinks_real", "downlinks_sim"]]
          .round(3).to_string(index=False))
    print()
    print(df[["id"] + [c for st in ("sent", "suppressed", "dropped") for c in (f"{st}_real", f"{st}_sim")]]
          .to_string(index=False))
    for k in COMPARE:
        print(f"\n{k}:")
        print(df[["id", f"{k}_real", f"{k}_sim"]].round(4).to_string(index=False))
    print(f"\n{len(df)} pruebas: tramas iguales {int(df.frames_same.sum())}/{int(df.frames_real.sum())} "
          f"(simuladas {int(df.frames_sim.sum())}), bins con los mismos pasos {int(df.bins_same_steps.sum())}/"
          f"{int(df.bins_real.sum())}, downlinks {int(df.downlinks_real.sum())} reales y {int(df.downlinks_sim.sum())} "
          "simulados")
    bad = df[df.first_diff != ""]
    if len(bad):
        print("\nPrimera trama distinta:")
        print(bad[["id", "first_diff"]].to_string(index=False))


if __name__ == "__main__":
    main()
