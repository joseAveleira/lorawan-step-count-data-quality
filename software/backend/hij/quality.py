"""Reconstrucción de la serie de pasos y métricas de calidad del dato de un experimento.

Bins evaluables (G): los que acaban antes del corte, que es el inicio del vaciado final (eval_s del plan) o, si no lo
hay, el final de la prueba menos un periodo y 30 s. Métricas (tiempos del laboratorio y su equivalente a escala real,
Δ_real = 15 min):
    DC(h)       completitud: bins recibidos como mucho h después de su fin / bins que han tenido ese tiempo
    DC_end      completitud al final del experimento
    RR          recuperación de los bins expuestos a falta de cobertura (su primer envío no llegó a salir o se hizo
                durante un corte real)
    SR_bins     recuperación de pasos por bins (definición del artículo): pasos de los bins recibidos / pasos de G.
                La S0 no envía bins: 0 por construcción
    SR_counter  recuperación de pasos con el contador acumulado: parte de los pasos de G incluida en el acumulado de
                la última trama recibida de cada arranque. El contador vuelve a 0 en cada arranque y cada trama
                lleva el del arranque en que se envió, aunque sus bins sean de una época anterior (frame_boot_epochs).
                Da el total entre tramas recibidas del mismo arranque, sin resolución por bins
                (counter_gap_max_h_real: mayor separación entre dos tramas recibidas seguidas de un arranque)
    SR_cumulative  alias de SR_counter, con el nombre anterior (lo usan sim30.py, validate_lab.py,
                preview_campaign.py y report.py). Hasta el 20/10 agrupaba el contador por la época de la trama y no
                se limitaba a G: tras un reinicio podía pasar de 1
    TI          integridad temporal: |t̂ - t| <= Δ/2
    time_err_med_s, time_err_p95_s, time_err_max_s
                mediana, P95 y máximo de |t̂ - t| en segundos de reloj (sin escalar), en los bins recibidos de G;
                t en el reloj de referencia de la verdad terreno (clock_ref; en el laboratorio, el del gateway)
    DR          duplicados: copias recibidas de más / bins recibidos (DUP en el artículo)
    OOR         llegadas fuera de orden
    d           retraso = llegada - fin del bin (mediana, P95 y máximo)
    FZR         falsos ceros, contados por bin: bins mostrados como 0 que tienen pasos / bins mostrados como 0, con
                los bins sin datos pintados como 0 (FZR_naive) o como "sin datos" (FZR_aware)
S0 (no envía bins: cada trama lleva los pasos desde el envío anterior, que cubren varios bins; s0_intervals):
    FZR_naive, FZR_aware   con cada trama mostrada sobre los bins que cubre (su incremento en todos ellos): solo
                los bins sin trama recibida dan falsos ceros (los mismos que en la S1), y la vista con calidad, que
                los muestra como ausentes, no tiene ninguno
    FZR_naive_lastbin      vista ingenua tipo CBMS: el incremento de cada trama en el último bin que cubre y 0 en
                los demás; añade falsos ceros aunque haya cobertura continua (panel (b) de fig:timeline)
    SR_increments          pasos de G cubiertos por una trama S0 recibida / pasos de G (SR por intervalo de trama);
                hasta el 20/10 sumaba también tramas posteriores al corte (1,07 en C1_S0)
    S0_interval_coverage   fracción de G cubierta por una trama S0 recibida
También clasifica cada trama según la etapa en la que se perdió (atribución por capas).
"""
import numpy as np
import pandas as pd

from .experiment import FRAME_KEYS, Experiment, boot_starts, frame_keys
from .payload import AGE_UNKNOWN, FLAG_RETX

DAY_S = 86400


def received_copies(exp: Experiment) -> pd.DataFrame:
    """Una fila por cada copia de un bin recibida en TTN (estrategias S1 a S4)."""
    rows = []
    for u in exp.uplinks.itertuples():
        if u.strategy == 0:
            continue
        known = u.age_units != AGE_UNKNOWN
        t0 = u.received_at - pd.Timedelta(seconds=u.age_units * exp.delta_s / 15) if known else pd.NaT
        for i, v in enumerate(u.bins):
            rows.append(dict(epoch=u.epoch, seq=(u.s1 + i) & 0xFFFF, value=v, arrival=u.received_at, fcnt=u.fcnt,
                             t_hat=(t0 + pd.Timedelta(seconds=i * exp.delta_s)) if known else pd.NaT,
                             retx=bool(u.flags & FLAG_RETX)))
    return pd.DataFrame(rows, columns=["epoch", "seq", "value", "arrival", "fcnt", "t_hat", "retx"])


def s0_intervals(exp: Experiment) -> pd.DataFrame:
    """S0: cada trama recibida cubre los bins s1 .. s1 + m - 1, con m = edad / Δ (en unidades de Δ/15)."""
    rows = []
    for u in exp.uplinks.itertuples():
        if u.strategy != 0 or u.age_units == AGE_UNKNOWN:
            continue
        m = max(1, int(round(u.age_units / 15)))
        rows.append(dict(epoch=u.epoch, s_first=u.s1, s_last=u.s1 + m - 1, increment=u.bins[0], arrival=u.received_at))
    return pd.DataFrame(rows, columns=["epoch", "s_first", "s_last", "increment", "arrival"])


def outage_windows(exp: Experiment) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Periodos de corte real (filtro del gateway), incluidos los segundos de reinicio del forwarder."""
    out, start = [], None
    for a in exp.gw_actions.itertuples() if not exp.gw_actions.empty else []:
        if a.action == "block":
            start = a.time
        elif a.action == "unblock" and start is not None:
            out.append((start, a.time + pd.Timedelta(seconds=20)))
            start = None
    if start is not None:
        out.append((start, exp.end))
    return out


def frame_fate(exp: Experiment) -> pd.DataFrame:
    """Atribución por capas: en qué etapa se pierde cada trama generada por el nodo."""
    tx = exp.tx.copy()
    heard = exp.gateway.groupby("fcnt")["copies"].max() if not exp.gateway.empty else pd.Series(dtype=int)
    at_ttn = set(exp.uplinks.fcnt) if not exp.uplinks.empty else set()
    windows = outage_windows(exp)

    def fate(r):
        if r.status == "suppressed":
            return "emulated_outage"
        if r.status == "dropped":
            return "node_dropped"
        if r.fcnt in at_ttn:
            return "delivered"
        in_outage = any(a <= r.t_tx <= b for a, b in windows)
        if r.fcnt in heard.index:
            return "gateway_filtered" if (in_outage or heard[r.fcnt] == 1) else "network_lost"
        return "gateway_down_or_filtered" if in_outage else "radio_lost"

    tx["fate"] = tx.apply(fate, axis=1) if not tx.empty else []
    return tx


def bin_table(exp: Experiment) -> pd.DataFrame:
    """Una fila por bin generado, con su primera llegada (si la hubo) y los indicadores de calidad."""
    gt = exp.gt[["epoch", "seq", "k", "t_start", "t_end", "steps", "cumulative"]].copy()
    copies = received_copies(exp)
    if not copies.empty:
        copies = copies.sort_values("arrival")
        first = copies.groupby(["epoch", "seq"], as_index=False).agg(
            value=("value", "first"), arrival=("arrival", "first"), t_hat=("t_hat", "first"),
            copies=("value", "size"), consistent=("value", lambda v: v.nunique() == 1))
        gt = gt.merge(first, on=["epoch", "seq"], how="left")
    else:
        for col in ["value", "copies", "consistent"]:
            gt[col] = np.nan
        for col in ["arrival", "t_hat"]:
            gt[col] = pd.Series(pd.NaT, index=gt.index, dtype="datetime64[ns, UTC]")
    gt["received"] = gt["arrival"].notna()
    gt["delay_s"] = (gt["arrival"] - gt["t_end"]).dt.total_seconds()
    gt["ti_ok"] = (gt["t_hat"] - gt["t_start"]).dt.total_seconds().abs() <= exp.delta_s / 2

    # fuera de orden: llega después de algún bin generado más tarde
    gt = gt.sort_values("t_start").reset_index(drop=True)
    never = np.iinfo("int64").max
    arr = np.array([t.value if pd.notna(t) else never for t in gt["arrival"]], dtype="int64")
    later_min = np.append(np.minimum.accumulate(arr[::-1])[::-1][1:], never)
    gt["oor"] = gt["received"] & (later_min < arr)

    # expuesto a falta de cobertura: su primer envío no salió o salió durante un corte real
    tx = frame_fate(exp)
    first_try = {}
    for r in tx.sort_values("t_tx").itertuples():
        for s in range(r.s1, r.s1 + r.n):
            first_try.setdefault((r.epoch, s & 0xFFFF), r.fate)
    gt["first_fate"] = [first_try.get((e, s)) for e, s in zip(gt.epoch, gt.seq)]
    gt["exposed"] = gt["first_fate"].isin(["emulated_outage", "node_dropped", "gateway_filtered",
                                           "gateway_down_or_filtered", "radio_lost", "network_lost"])
    return gt


def evaluable_bins(exp: Experiment, end_margin_s: float | None = None) -> tuple[pd.DataFrame, pd.Timestamp]:
    """bin_table restringida a los bins evaluables (G) y el instante de corte."""
    period = exp.config.get("period", 240)
    end_margin_s = period + 30 if end_margin_s is None else end_margin_s
    bins = bin_table(exp)
    eval_s = exp.plan.get("eval_s") if exp.plan.get("eval_s") != exp.plan.get("duration_s") else None
    cutoff = (exp.exp_start + pd.Timedelta(seconds=eval_s)) if eval_s else exp.end - pd.Timedelta(seconds=end_margin_s)
    return bins[bins.t_end <= cutoff], cutoff


def _abs_time_error_s(rec: pd.DataFrame) -> pd.Series:
    """|t̂ - t| en segundos de los bins recibidos con edad conocida."""
    return (rec["t_hat"] - rec["t_start"]).dt.total_seconds().abs().dropna()


def time_errors(exp: Experiment, end_margin_s: float | None = None) -> pd.Series:
    """|t̂ - t| (s, reloj de referencia de exp.clock_ref) de cada bin recibido de G, para agregar varias pruebas."""
    elig, _ = evaluable_bins(exp, end_margin_s)
    return _abs_time_error_s(elig[elig.received]).reset_index(drop=True)


def frame_boot_epochs(exp: Experiment) -> pd.Series:
    """Época del arranque en que se envió cada uplink recibido (mismo índice que exp.uplinks; NaN si no se sabe).

    El acumulado de una trama es el del arranque en curso, aunque sus bins sean de una época anterior (tras un
    reinicio, la S3 reenvía los bins no confirmados de la época vieja con el contador del arranque nuevo).
    1) Con la línea TX de la trama (laboratorio y simulador; misma trama por fcnt, época, s1 y n): el arranque en
       curso en su instante de envío (boot_starts).
    2) Sin ella (despliegue): solo si el último bin de la trama es el último cerrado al enviarla (la S0 siempre; en
       las demás, n = floor(edad / 15)), porque entonces la época de la trama es la del arranque en curso."""
    up = exp.uplinks
    if up.empty:
        return pd.Series(dtype="float64")
    boot = np.full(len(up), np.nan)
    boots = boot_starts(exp.gt) if not exp.gt.empty else None
    tx = exp.tx
    if boots is not None and len(boots) and not tx.empty:
        sent = tx[tx.status == "sent"].drop_duplicates("fcnt", keep=False)
        m = frame_keys(up).merge(frame_keys(sent).assign(t_ms=sent.t_ms.to_numpy(dtype="float64")),
                                 on=FRAME_KEYS, how="left", validate="many_to_one")
        t = m.t_ms.to_numpy(dtype="float64")
        known = ~np.isnan(t)
        j = np.clip(np.searchsorted(boots.start_ms.to_numpy(), t[known], side="right") - 1, 0, len(boots) - 1)
        boot[known] = boots.epoch.to_numpy()[j]
    age = up.age_units.to_numpy()
    safe = (up.strategy.to_numpy() == 0) | ((age != AGE_UNKNOWN) & (up.n.to_numpy() == age // 15))
    fill = np.isnan(boot) & safe
    boot[fill] = up.epoch.to_numpy()[fill]
    return pd.Series(boot, index=up.index)


def sr_counter(exp: Experiment, elig: pd.DataFrame, boot: pd.Series | None = None) -> float:
    """SR_counter: parte de los pasos de los bins elig incluida en el acumulado de la última trama recibida de cada
    arranque (boot: frame_boot_epochs). En cada época, el acumulado c cubre los bins con acumulado <= c."""
    total = elig.steps.sum()
    if not total:
        return np.nan
    if exp.uplinks.empty:
        return 0.0
    boot = frame_boot_epochs(exp) if boot is None else boot
    last = exp.uplinks.cumulative.groupby(boot).max()
    last.index = last.index.astype("int64")
    recovered = 0
    for e, g in elig.groupby("epoch"):
        if e not in last.index:
            continue
        g = g.sort_values("t_start")
        lo = int(g.cumulative.iloc[0] - g.steps.iloc[0])   # acumulado antes del primer bin de G de la época
        hi = int(g.cumulative.iloc[-1])
        recovered += min(max(int(last[e]), lo), hi) - lo
    return float(recovered / total)


def counter_gap_max_s(exp: Experiment, boot: pd.Series | None = None) -> float:
    """Mayor separación (s) entre dos tramas recibidas seguidas del mismo arranque (boot: frame_boot_epochs): el
    periodo más largo cuyo total de pasos solo se conoce por el contador acumulado."""
    up = exp.uplinks
    if len(up) < 2:
        return np.nan
    boot = frame_boot_epochs(exp) if boot is None else boot
    gaps = [g.sort_values().diff().dt.total_seconds().max() for _, g in up.received_at.groupby(boot) if len(g) > 1]
    return float(np.nanmax(gaps)) if gaps else np.nan


def s0_views(exp: Experiment, elig: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """S0 sobre los bins elig: cubierto por una trama recibida, incremento de esa trama (NaN si ninguna) y vista del
    último bin (el incremento de cada trama en el último bin evaluable que cubre y 0 en los demás)."""
    n = len(elig)
    covered, increment, last_bin = np.zeros(n, dtype=bool), np.full(n, np.nan), np.zeros(n)
    pos = {k: i for i, k in enumerate(zip(elig.epoch, elig.seq))}
    for r in s0_intervals(exp).itertuples():
        idx = [pos[(r.epoch, s & 0xFFFF)] for s in range(r.s_first, r.s_last + 1) if (r.epoch, s & 0xFFFF) in pos]
        if idx:
            covered[idx] = True
            increment[idx] = r.increment
            last_bin[idx[-1]] = r.increment
    return covered, increment, last_bin


def _share(hits: np.ndarray, base: np.ndarray) -> float:
    return float(hits.sum() / base.sum()) if base.sum() else np.nan


def metrics(exp: Experiment, end_margin_s: float | None = None) -> dict:
    c = exp.compression
    elig, cutoff = evaluable_bins(exp, end_margin_s)
    rec = elig[elig.received]
    out = dict(
        id=exp.plan.get("id", exp.path.name), strategy=exp.config.get("strategy"),
        scenario=exp.config.get("scenario"), delta_s=exp.delta_s, compression=c, clock_ref=exp.clock_ref,
        duration_h_lab=(exp.end - exp.exp_start).total_seconds() / 3600,
        bins_generated=len(elig), bins_received=int(elig.received.sum()),
    )
    if exp.clock_info:
        out.update(clock_frames=exp.clock_info.get("frames"), clock_resid_max_ms=exp.clock_info.get("resid_max_ms"),
                   clock_drift_ppm=exp.clock_info.get("drift_ppm"))
    for h_real in (1, 24):
        h_lab = h_real * 3600 / c
        ok = elig[elig.t_end <= exp.end - pd.Timedelta(seconds=h_lab)]
        out[f"DC_{h_real}h"] = float(((ok.delay_s <= h_lab) & ok.received).mean()) if len(ok) else np.nan
    out["DC_end"] = float(elig.received.mean()) if len(elig) else np.nan
    exp_bins = elig[elig.exposed]
    out["RR"] = float(exp_bins.received.mean()) if len(exp_bins) else np.nan
    out["bins_exposed"] = len(exp_bins)
    gt_steps = elig.steps.sum()
    out["SR_bins"] = float(rec.value.sum() / gt_steps) if gt_steps else np.nan
    boot = frame_boot_epochs(exp)
    out["SR_counter"] = sr_counter(exp, elig, boot)
    out["SR_cumulative"] = out["SR_counter"]   # alias (nombre anterior)
    gap = counter_gap_max_s(exp, boot)
    out["counter_gap_max_h_real"] = gap * c / 3600 if np.isfinite(gap) else np.nan
    out["TI"] = float(rec.ti_ok.mean()) if len(rec) else np.nan
    err = _abs_time_error_s(rec)
    if len(err):
        out.update(time_err_med_s=float(err.median()), time_err_p95_s=float(err.quantile(0.95)),
                   time_err_max_s=float(err.max()))
    out["DR"] = float((rec.copies.sum() - len(rec)) / len(rec)) if len(rec) else np.nan
    out["OOR"] = float(rec.oor.mean()) if len(rec) else np.nan
    if len(rec):
        d = rec.delay_s
        out.update(delay_med_s=float(d.median()), delay_p95_s=float(d.quantile(0.95)), delay_max_s=float(d.max()))
        out.update(delay_med_h_real=out["delay_med_s"] * c / 3600, delay_p95_h_real=out["delay_p95_s"] * c / 3600,
                   delay_max_h_real=out["delay_max_s"] * c / 3600)
    # falsos ceros, por bin
    steps = elig.steps.to_numpy()
    active = steps > 0
    received = elig.received.to_numpy()
    value = elig.value.to_numpy(dtype="float64")
    zeros = ~received | (value == 0)                    # vista ingenua: lo no recibido se pinta como 0
    out["FZR_naive"] = _share(active & zeros, zeros)
    zeros_aware = received & (value == 0)               # vista con calidad: lo no recibido se pinta "sin datos"
    out["FZR_aware"] = _share(active & zeros_aware, zeros_aware)
    # S0: no hay bins sino incrementos que cubren varios bins (se pierde la resolución temporal)
    if exp.config.get("strategy") == 0:
        covered, increment, last_bin = s0_views(exp, elig)
        out["S0_interval_coverage"] = float(covered.mean()) if len(elig) else np.nan
        out["SR_increments"] = float(steps[covered].sum() / gt_steps) if gt_steps else np.nan
        zeros = ~covered | (increment == 0)             # cada trama sobre los bins que cubre; sin trama, 0
        out["FZR_naive"] = _share(active & zeros, zeros)
        zeros_aware = covered & (increment == 0)        # sin trama, "sin datos"
        out["FZR_aware"] = _share(active & zeros_aware, zeros_aware)
        zeros_last = last_bin == 0                      # incremento en el último bin del intervalo
        out["FZR_naive_lastbin"] = _share(active & zeros_last, zeros_last)
    # airtime y atribución por capas
    tx = frame_fate(exp)
    lab_s = max((exp.end - exp.exp_start).total_seconds(), 1)
    out["airtime_s_per_day"] = float(tx.toa_ms.sum() / 1000 * DAY_S / lab_s)
    out["frames"] = len(tx)
    out.update({f"frames_{k}": int(v) for k, v in tx.fate.value_counts().items()})
    return out
