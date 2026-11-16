"""Carga de un experimento: verdad terreno del nodo, uplinks de TTN y registros del gateway.

Todas las marcas de tiempo se dan en UTC. La verdad terreno (inicio de cada bin, envío de cada trama, inicio y fin
de la prueba) está en el reloj del nodo (ms desde el último arranque en frío) y se pasa a UTC con un reloj de
referencia (Experiment.clock_ref):

gateway  Por defecto. Cada trama enviada que llega a TTN trae la hora del gateway (rx_metadata.time, al acabar la
         recepción; menos el tiempo en el aire de su línea TX, inicio de la trama). Las tramas se emparejan con su
         línea TX (t_ms, reloj del nodo) por contador de tramas, época, s1 y n, y se ajusta por mínimos cuadrados
             hora del gateway = desfase del tramo + deriva * reloj del nodo,
         con una deriva común (el oscilador del nodo es el mismo en toda la prueba) y un desfase por tramo de
         arranque: el reloj del nodo sigue en los reinicios por software y en los apagados con el botón, pero puede
         saltar en ellos (en el sueño profundo lo lleva el oscilador RC). La deriva solo se estima si las tramas de
         algún tramo abarcan al menos CLOCK_MIN_SPAN_S; si no, vale 1. Se descartan las tramas con un residuo de más
         de CLOCK_MAX_RESID_S y se repite el ajuste. Un tramo sin tramas emparejadas toma el ajuste del tramo
         anterior (o del siguiente), con aviso. En la campaña el residuo fue de 17 ms como mucho.
pc       Respaldo, con aviso (Experiment.clock_note), si ninguna trama se puede emparejar: sin ttn.jsonl, sin
         tramas recibidas o sin la hora del gateway. Desfase mínimo de (hora del PC - hora del nodo) en las líneas
         que el nodo imprime en instantes conocidos (cierre de cada bin e inicio del experimento), porque la latencia
         de la línea serie solo puede ser positiva. El reloj del PC se desvía del gateway: en la campaña, de unas
         décimas de segundo, y en C2_S2 llegó a ir 3,3 s por detrás hasta que Windows lo resincronizó (+2,73 s a las
         17:20:14 UTC del 19/10).
offset   Experiment construido sin load_experiment (simulador): utc = ms_nodo / 1000 + node_offset_s.

Las horas del PC del registro (host_time) que se comparan con la verdad terreno (fin de la prueba y cortes reales
del gateway) se pasan al reloj de referencia con el error del PC medido en la última línea GT anterior, que sigue los
saltos del reloj del PC (Experiment.host_to_utc). host_time, host_start y host_end quedan en el reloj del PC, para
compararlas entre sí (por ejemplo, los downlinks de node.log desde el inicio de la prueba).
"""
import base64
import json
import pathlib
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .nodelog import read_node_log
from .payload import FPORT_BASE, decode

REAL_DELTA_S = 900          # duración de un bin a escala real (15 min)
CLOCK_MAX_RESID_S = 0.5     # residuo máximo de una trama en el ajuste al reloj del gateway
CLOCK_MIN_SPAN_S = 1800     # tramas de un tramo que deben abarcar este tiempo para estimar la deriva (con ruido de
                            # unos 5 ms, la incertidumbre de la deriva queda por debajo de la deriva típica, 5 ppm)
GW_TIME_MAX_DIFF_S = 60     # hora del gateway descartada si se aleja más de esto de la recepción en TTN


@dataclass
class Experiment:
    path: pathlib.Path
    plan: dict
    config: dict
    delta_s: int
    exp_start: pd.Timestamp          # UTC (reloj de referencia)
    end: pd.Timestamp                # UTC (reloj de referencia): último registro del experimento
    node_offset_s: float             # reloj del PC (o del simulador): utc = ms_nodo / 1000 + desfase
    gt: pd.DataFrame                 # bins generados (verdad terreno)
    tx: pd.DataFrame                 # tramas, enviadas o no
    ev: pd.DataFrame
    uplinks: pd.DataFrame            # uplinks recibidos por TTN, decodificados (gw_time: hora del gateway)
    gateway: pd.DataFrame            # uplinks del nodo registrados por el gateway
    gw_actions: pd.DataFrame = field(default_factory=pd.DataFrame)  # cortes reales (filtro del gateway)
    clock_ref: str = "offset"        # reloj de la verdad terreno: "gateway", "pc" u "offset" (ver el docstring)
    clock_fit: list = field(default_factory=list)   # tramos del ajuste al gateway: start_ms, x_s, y_s, frames
    clock_drift: float = 1.0         # pendiente común del ajuste (s del gateway por s del nodo)
    clock_info: dict = field(default_factory=dict)  # tramas emparejadas, descartadas, residuo máximo, deriva
    clock_note: str = ""             # aviso cuando no se ha podido usar el reloj del gateway
    host_start: pd.Timestamp | None = None   # inicio y fin de la prueba en el reloj del PC
    host_end: pd.Timestamp | None = None
    host_error: pd.DataFrame = field(default_factory=pd.DataFrame)  # error del PC en cada línea GT: host_s, err_s

    @property
    def compression(self) -> float:
        """Factor de compresión temporal del laboratorio (15 con Δ = 60 s)."""
        return REAL_DELTA_S / self.delta_s

    def node_to_utc(self, node_ms):
        """Reloj del nodo (ms) -> UTC en el reloj de referencia."""
        x = np.asarray(node_ms, dtype="float64") / 1000.0
        if self.clock_fit:
            starts = np.array([s["start_ms"] for s in self.clock_fit], dtype="float64") / 1000.0
            i = np.clip(np.searchsorted(starts, x, side="right") - 1, 0, len(starts) - 1)
            xs = np.array([s["x_s"] for s in self.clock_fit])[i]
            ys = np.array([s["y_s"] for s in self.clock_fit])[i]
            utc = ys + self.clock_drift * (x - xs)
        else:
            utc = x + self.node_offset_s
        return pd.to_datetime(utc, unit="s", utc=True)

    def host_to_utc(self, host_times) -> pd.DatetimeIndex:
        """Horas del PC (host_time) -> reloj de referencia, restando el error del PC medido en la última línea GT
        anterior (o en la primera, si no hay ninguna antes). Sin ajuste al gateway se devuelven igual."""
        t = pd.DatetimeIndex(pd.to_datetime(host_times, utc=True))
        if self.clock_ref != "gateway" or self.host_error.empty or not len(t):
            return t
        ref = self.host_error.host_s.to_numpy()
        t_s = np.array([x.timestamp() for x in t])
        i = np.clip(np.searchsorted(ref, t_s, side="right") - 1, 0, len(ref) - 1)
        return t - pd.to_timedelta(self.host_error.err_s.to_numpy()[i], unit="s")


FRAME_KEYS = ["fcnt", "epoch", "s1", "n"]   # identifican una trama en node.log (TX) y en TTN


def frame_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Columnas FRAME_KEYS como números reales (NaN si faltan; TTN omite f_cnt cuando vale 0), para emparejar las
    líneas TX del nodo con los uplinks de TTN."""
    return pd.DataFrame({k: pd.to_numeric(df[k], errors="coerce").astype("float64").to_numpy() for k in FRAME_KEYS})


def boot_starts(gt: pd.DataFrame) -> pd.DataFrame:
    """Arranques del nodo durante el experimento, en orden: época e instante del arranque en el reloj del nodo (ms).

    Cada arranque abre una época y su primer bin empieza en ese instante (en el firmware, epochStartMs =
    openStartMs). Con el reloj del nodo continuo (reinicios por software y apagados con el botón), el orden de
    t_start_ms es el de los arranques."""
    if gt.empty:
        return pd.DataFrame({"epoch": pd.Series(dtype="int64"), "start_ms": pd.Series(dtype="float64")})
    g = gt.sort_values("t_start_ms", kind="stable")
    new = g.epoch.ne(g.epoch.shift()).to_numpy()
    return pd.DataFrame({"epoch": g.epoch.to_numpy()[new].astype("int64"),
                         "start_ms": g.t_start_ms.to_numpy()[new].astype("float64")})


def _parse_config(detail: str) -> dict:
    out = {}
    for item in detail.split(","):
        k, _, v = item.partition("=")
        if v.lstrip("-").isdigit():
            out[k] = int(v)
    return out


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _gateway_time(up: dict, received_at: pd.Timestamp):
    """Hora del gateway al acabar de recibir la trama (primera rx_metadata con hora), o None si no la hay o no es
    creíble (gateway sin hora sincronizada)."""
    t = next((md.get("time") for md in up.get("rx_metadata") or [] if md.get("time")), None)
    t = t or (up.get("settings") or {}).get("time")
    if not t:
        return None
    t = pd.Timestamp(t)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t if abs((t - received_at).total_seconds()) <= GW_TIME_MAX_DIFF_S else None


def _drift(x: np.ndarray, y: np.ndarray, seg: np.ndarray) -> float:
    """Pendiente común de y frente a x con un desfase por tramo; 1 si ningún tramo abarca CLOCK_MIN_SPAN_S."""
    num = den = span = 0.0
    for s in np.unique(seg):
        xs, ys = x[seg == s], y[seg == s]
        if len(xs) < 2:
            continue
        dx = xs - xs.mean()
        num += float(dx @ (ys - ys.mean()))
        den += float(dx @ dx)
        span = max(span, float(xs.max() - xs.min()))
    return num / den if den > 0 and span >= CLOCK_MIN_SPAN_S else 1.0


def _fit_segments(x: np.ndarray, y: np.ndarray, seg: np.ndarray, keep: np.ndarray):
    """Ajuste con las tramas keep: deriva, punto medio (x, y) de cada tramo con tramas y residuo de todas las tramas
    (infinito en los tramos sin tramas conservadas)."""
    b = _drift(x[keep], y[keep], seg[keep])
    xm = {int(s): float(x[keep & (seg == s)].mean()) for s in np.unique(seg[keep])}
    ym = {int(s): float(y[keep & (seg == s)].mean()) for s in np.unique(seg[keep])}
    resid = np.array([y[i] - (ym[s] + b * (x[i] - xm[s])) if s in xm else np.inf
                      for i, s in enumerate(seg.tolist())])
    return b, xm, ym, resid


def fit_gateway_clock(gt: pd.DataFrame, tx: pd.DataFrame, ups: pd.DataFrame):
    """Ajuste reloj del nodo -> reloj del gateway con las tramas emparejadas (ver el docstring del módulo).

    Devuelve (tramos, deriva, info, aviso). tramos: una entrada por arranque con start_ms (inicio del tramo en el
    reloj del nodo), x_s e y_s (punto del ajuste: s del nodo y s UTC) y frames (tramas emparejadas del tramo).
    Sin ninguna trama emparejada, tramos = [] y aviso dice por qué."""
    if ups.empty or "gw_time" not in ups or ups.gw_time.isna().all():
        return [], 1.0, {}, "ningún uplink de TTN trae la hora del gateway"
    if tx.empty:
        return [], 1.0, {}, "no hay líneas TX"
    sent = tx[tx.status == "sent"].drop_duplicates("fcnt", keep=False)
    u = ups.dropna(subset=["gw_time"]).drop_duplicates("fcnt", keep=False)
    m = (frame_keys(sent).assign(t_ms=sent.t_ms.to_numpy(dtype="float64"),
                                 toa_ms=sent.toa_ms.to_numpy(dtype="float64"))
         .merge(frame_keys(u).assign(gw_time=u.gw_time.to_numpy()), on=FRAME_KEYS))
    if m.empty:
        return [], 1.0, {}, "ninguna trama enviada se ha podido emparejar con su recepción en el gateway"
    x = m.t_ms.to_numpy(dtype="float64") / 1000.0
    y = np.array([t.timestamp() for t in m.gw_time]) - m.toa_ms.to_numpy(dtype="float64") / 1000.0
    boots = boot_starts(gt)
    starts = boots.start_ms.to_numpy() / 1000.0 if len(boots) else np.array([x.min()])
    seg = np.clip(np.searchsorted(starts, x, side="right") - 1, 0, len(starts) - 1)
    keep = np.ones(len(x), dtype=bool)
    for _ in range(3):   # ajuste y, si alguna trama se sale, otro sin ellas
        b, xm, ym, resid = _fit_segments(x, y, seg, keep)
        bad = keep & (np.abs(resid) > CLOCK_MAX_RESID_S)
        if not bad.any():
            break
        keep &= ~bad
        if not keep.any():
            return [], 1.0, {}, "ninguna trama emparejada ajusta con el reloj del gateway"
    else:
        b, xm, ym, resid = _fit_segments(x, y, seg, keep)
    notes = []
    segments = []
    for s in range(len(starts)):
        n = int((keep & (seg == s)).sum())
        if n:
            segments.append(dict(start_ms=float(starts[s] * 1000), x_s=xm[s], y_s=ym[s], frames=n))
            continue
        near = max((t for t in xm if t < s), default=min(xm))
        segments.append(dict(start_ms=float(starts[s] * 1000), x_s=xm[near], y_s=ym[near], frames=0))
        notes.append(f"el tramo del arranque {s + 1} no tiene tramas emparejadas y toma el ajuste del arranque "
                     f"{near + 1}")
    used = np.abs(resid[keep])
    info = dict(frames=int(keep.sum()), dropped=int((~keep).sum()), resid_max_ms=float(used.max() * 1000),
                drift_ppm=float((b - 1) * 1e6), segments=len(segments))
    if (~keep).any():
        notes.append(f"{int((~keep).sum())} tramas descartadas por residuo > {CLOCK_MAX_RESID_S:g} s")
    return segments, b, info, "; ".join(notes)


def load_experiment(path: pathlib.Path, clock: str = "gateway") -> Experiment:
    """Carga una prueba de data/raw. clock = "gateway" (por defecto, con respaldo al PC) o "pc"."""
    if clock not in ("gateway", "pc"):
        raise ValueError(f"reloj de referencia desconocido: {clock}")
    path = pathlib.Path(path)
    plan = json.loads((path / "plan.json").read_text(encoding="utf-8")) if (path / "plan.json").exists() else {}
    node = read_node_log(path / "node.log")
    gt, tx, ev = node["gt"], node["tx"], node["ev"]

    starts = ev[(ev.type == "exp") & ev.detail.str.startswith("start,")]
    if starts.empty:
        raise ValueError(f"{path}: no hay línea EV,exp,start")
    start_row = starts.iloc[-1]
    exp_start_ms = int(start_row.detail.split(",")[1])
    configs = ev[(ev.type == "config") & (ev.host_time <= start_row.host_time)]
    config = _parse_config(configs.iloc[-1].detail) if not configs.empty else {}
    delta_s = config.get("delta", 60)

    # bins del experimento: los cerrados después del inicio (incluidas las épocas tras un reinicio)
    gt = gt[(gt.host_time >= start_row.host_time) & (gt.t_start_ms >= exp_start_ms - delta_s * 500)].copy()
    tx = tx[tx.host_time >= start_row.host_time].copy()

    # desfase reloj del nodo -> reloj del PC (respaldo): líneas impresas al cerrar cada bin y al iniciar la prueba
    host_s = np.append(gt.host_time.map(pd.Timestamp.timestamp).to_numpy(dtype="float64"),
                       start_row.host_time.timestamp())
    node_s = np.append((gt.t_start_ms.to_numpy(dtype="float64") + delta_s * 1000) / 1000.0, exp_start_ms / 1000.0)
    offset = float(np.min(host_s - node_s))
    host_start = pd.Timestamp(start_row.host_time)
    host_end = pd.Timestamp(node["ev"].host_time.max())
    host_end = max(host_end, gt.host_time.max() if not gt.empty else host_end)
    exp = Experiment(path, plan, config, delta_s, host_start, host_end, offset, gt, tx, ev, pd.DataFrame(),
                     pd.DataFrame(), clock_ref="pc", host_start=host_start, host_end=host_end)

    # uplinks de TTN (se decodifican aquí, sin depender del formatter de TTN)
    rows, dev_addr = [], None
    for rec in _read_jsonl(path / "ttn.jsonl"):
        if not rec["topic"].endswith("/up"):
            continue
        msg = rec["msg"]
        up = msg.get("uplink_message", {})
        dev_addr = msg.get("end_device_ids", {}).get("dev_addr", dev_addr)
        fport = up.get("f_port")
        if fport is None or not FPORT_BASE <= fport <= FPORT_BASE + 4:
            continue
        frame = decode(base64.b64decode(up.get("frm_payload", "")), fport)
        meta = (up.get("rx_metadata") or [{}])[0]
        received_at = pd.Timestamp(msg.get("received_at") or up.get("received_at"))
        rows.append(dict(
            received_at=received_at,
            fcnt=up.get("f_cnt"), fport=fport, strategy=frame.strategy, flags=frame.flags, epoch=frame.epoch,
            s1=frame.s1, n=frame.n, age_units=frame.age_units, cumulative=frame.cumulative, bins=frame.bins,
            rssi=meta.get("rssi"), snr=meta.get("snr"),
            airtime_s=float(str(up.get("consumed_airtime", "0s")).rstrip("s") or 0),
            gw_time=_gateway_time(up, received_at)))
    ups = pd.DataFrame(rows)
    if not ups.empty:
        ups["received_at"] = pd.to_datetime(ups["received_at"], utc=True)
        ups["gw_time"] = pd.to_datetime(ups["gw_time"], utc=True)
        # solo las de la prueba (la preparación puede repetir contadores de trama de otra sesión)
        ups_run = ups[ups.received_at >= host_start - pd.Timedelta(minutes=1)]
    else:
        ups_run = ups

    # reloj de referencia
    if clock == "gateway":
        segments, drift, info, note = fit_gateway_clock(gt, tx, ups_run)
        if segments:
            exp.clock_ref, exp.clock_fit, exp.clock_drift, exp.clock_info = "gateway", segments, drift, info
            exp.clock_note = note
        else:
            exp.clock_note = f"{note}; se usa el reloj del PC"
        if exp.clock_note:
            warnings.warn(f"{path.name}: {exp.clock_note}", stacklevel=2)
    if exp.clock_ref == "gateway":
        exp.host_error = pd.DataFrame({"host_s": host_s, "err_s": host_s - exp.node_to_utc(node_s * 1000).map(
            pd.Timestamp.timestamp).to_numpy(dtype="float64")}).sort_values("host_s", kind="stable")
        exp.exp_start = exp.node_to_utc([exp_start_ms])[0]
        exp.end = exp.host_to_utc([host_end])[0]

    gt["t_start"] = exp.node_to_utc(gt.t_start_ms)
    gt["t_end"] = gt["t_start"] + pd.Timedelta(seconds=delta_s)
    tx["t_tx"] = exp.node_to_utc(tx.t_ms)

    exp.uplinks = ups
    if not ups.empty:
        exp.uplinks = ups[ups.received_at >= exp.exp_start].reset_index(drop=True)

    # registros del gateway: una fila por trama (DevAddr del nodo) y número de copias (1 = filtrada)
    g = []
    for rec in _read_jsonl(path / "gateway.jsonl"):
        r = rec["row"]
        content = r.get("content") or ""
        if not r.get("pdtype", "").endswith("UP") or ":" not in content:
            continue
        try:
            c = json.loads(content.split(":", 1)[1])
        except ValueError:
            continue
        addr = c.get("ADDR")
        if dev_addr and addr != dev_addr.upper():
            continue
        year = exp.exp_start.year
        t = pd.to_datetime(f"{year}/{r['time']}", format="%Y/%m/%d-%H:%M:%S", utc=True)
        g.append(dict(id=r["id"], time=t, freq=float(r["freq"]), fcnt=c.get("FCnt"), fport=c.get("FPort"),
                      rssi=c.get("Rssi"), snr=c.get("snr"), size=c.get("Size"), addr=addr))
    gw = pd.DataFrame(g)
    if not gw.empty:
        gw = (gw.drop_duplicates("id").groupby(["time", "fcnt"], as_index=False)
              .agg(freq=("freq", "first"), fport=("fport", "first"), rssi=("rssi", "first"), snr=("snr", "first"),
                   size=("size", "first"), copies=("id", "count")))
        gw = gw[gw.time >= exp.exp_start.floor("s")].reset_index(drop=True)
    exp.gateway = gw

    # cortes reales aplicados por el orquestador ("# gw block|unblock <devaddr>" en node.log), en el reloj de
    # referencia
    acts = []
    for line in (path / "node.log").read_text(encoding="utf-8").splitlines():
        host, _, rec = line.partition("\t")
        if rec.startswith("# gw "):
            parts = rec.split()
            acts.append(dict(time=pd.Timestamp(host), action=parts[2]))
    exp.gw_actions = pd.DataFrame(acts)
    if not exp.gw_actions.empty:
        exp.gw_actions["time"] = exp.host_to_utc(exp.gw_actions["time"])
    return exp
