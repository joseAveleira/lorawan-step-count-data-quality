"""Pruebas de hij.quality (SR por bins y por contador, falsos ceros de la S0) y del paso de la verdad terreno al
reloj del gateway en hij.experiment, con experimentos sintéticos."""
import base64
import json
import math
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hij.experiment import Experiment, load_experiment  # noqa: E402
from hij.quality import frame_boot_epochs, metrics, time_errors  # noqa: E402

T0 = pd.Timestamp("2026-10-20 08:00:00", tz="UTC")
DELTA = 60
LATENCY = 0.3


# ------------------------------------------------------------------------------- experimentos en memoria
def build(bins, frames, strategy, eval_s, duration_s, with_tx=True):
    """Experiment como el del simulador. bins: (época, seq, inicio en s, pasos), con el contador desde 0 en cada
    época (un arranque por época). frames: dict(epoch, s1, n, t, bins, cumulative, received, fcnt); edad = la del
    bin s1 de la época de la trama en el instante t."""
    gt = pd.DataFrame(bins, columns=["epoch", "seq", "t_s", "steps"])
    gt["cumulative"] = gt.groupby("epoch").steps.cumsum()
    gt["t_start_ms"] = (gt.t_s * 1000).astype("int64")
    gt["k"] = (gt.t_s // DELTA).astype("int64")
    start = {(e, s): t for e, s, t in zip(gt.epoch, gt.seq, gt.t_s)}
    tx, ups = [], []
    for f in frames:
        age = int((f["t"] - start[(f["epoch"], f["s1"])]) * 15 // DELTA)
        status = "sent" if f.get("sent", True) else "suppressed"
        tx.append(dict(epoch=f["epoch"], fcnt=f["fcnt"] if status == "sent" else -1, s1=f["s1"], n=f["n"],
                       age_units=age, dr=5, bytes=11 + 2 * f["n"], status=status, toa_ms=70 if status == "sent" else 0,
                       t_ms=int(f["t"] * 1000)))
        if f.get("received", True) and status == "sent":
            ups.append(dict(received_at=T0 + pd.Timedelta(seconds=f["t"] + LATENCY), fcnt=f["fcnt"],
                            fport=10 + strategy, strategy=strategy, flags=0, epoch=f["epoch"], s1=f["s1"], n=f["n"],
                            age_units=age, cumulative=f["cumulative"], bins=f["bins"], rssi=-50, snr=10.0,
                            airtime_s=0.07))
    tx = pd.DataFrame(tx, columns=["epoch", "fcnt", "s1", "n", "age_units", "dr", "bytes", "status", "toa_ms", "t_ms"])
    if not with_tx:
        tx = tx.iloc[0:0]
    ups = pd.DataFrame(ups)
    ups["received_at"] = pd.to_datetime(ups["received_at"], utc=True)
    config = dict(strategy=strategy, scenario=1, delta=DELTA, period=240)
    exp = Experiment(pathlib.Path("TEST"), dict(id="TEST", eval_s=eval_s, duration_s=duration_s), config, DELTA, T0,
                     T0 + pd.Timedelta(seconds=duration_s), T0.timestamp(), gt, tx,
                     pd.DataFrame(columns=["host_time", "type", "detail"]), ups,
                     pd.DataFrame(columns=["time", "fcnt", "copies"]))
    gt["t_start"] = exp.node_to_utc(gt.t_start_ms)
    gt["t_end"] = gt["t_start"] + pd.Timedelta(seconds=DELTA)
    if not tx.empty:
        tx["t_tx"] = exp.node_to_utc(tx.t_ms)
    else:
        tx["t_tx"] = pd.Series(dtype="datetime64[ns, UTC]")
    return exp


# Reinicio a los 240 s. Época 0: bins 0..3; época 1 (arranque a los 240 s): bins 0..6, el 6 fuera de la ventana
# evaluable (corte a los 600 s). La trama B sale tras el reinicio con bins de la época 0 y el contador del arranque
# nuevo (30), como un reenvío de la S3; agrupar por la época de la trama le daría a la época 0 un total de 30.
E0 = [10, 0, 5, 7]                   # acumulado 10, 10, 15, 22
E1 = [20, 5, 0, 5, 0, 1, 9]          # acumulado 20, 25, 25, 30, 30, 31, 40
REBOOT_BINS = ([(0, s, s * DELTA, v) for s, v in enumerate(E0)]
               + [(1, s, 240 + s * DELTA, v) for s, v in enumerate(E1)])
REBOOT_FRAMES = [
    dict(epoch=0, s1=0, n=2, t=120, bins=E0[0:2], cumulative=10, fcnt=1),   # A: arranque 0
    dict(epoch=0, s1=2, n=2, t=540, bins=E0[2:4], cumulative=30, fcnt=2),   # B: arranque 1, bins de la época 0
    dict(epoch=1, s1=0, n=6, t=600, bins=E1[0:6], cumulative=31, fcnt=3),   # C: arranque 1
    dict(epoch=1, s1=6, n=1, t=660, bins=E1[6:7], cumulative=40, fcnt=4),   # D: después del corte de evaluación
]


def test_sr_counter_assigns_each_frame_to_its_boot():
    exp = build(REBOOT_BINS, REBOOT_FRAMES, strategy=3, eval_s=600, duration_s=700)
    assert list(frame_boot_epochs(exp)) == [0, 1, 1, 1]
    m = metrics(exp)
    # época 0: el contador del arranque 0 llega a 10 de 22; época 1: 31 de 31 (la trama D, con 40, no pasa de la
    # ventana). Con el contador agrupado por la época de la trama saldría 1,00.
    assert m["SR_counter"] == pytest.approx((10 + 31) / (22 + 31))
    assert m["SR_cumulative"] == m["SR_counter"]          # alias con el nombre anterior
    assert m["SR_bins"] == pytest.approx(1.0)             # la S3 recibe todos los bins de la ventana
    assert m["counter_gap_max_h_real"] == pytest.approx(60 * 15 / 3600)   # B, C y D: 60 s entre tramas


def test_sr_counter_without_tx_lines_uses_only_frames_with_the_newest_bin():
    exp = build(REBOOT_BINS, REBOOT_FRAMES, strategy=3, eval_s=600, duration_s=700, with_tx=False)
    boot = frame_boot_epochs(exp)
    assert boot.iloc[0] == 0 and math.isnan(boot.iloc[1])   # B no lleva el último bin cerrado: no se sabe
    assert list(boot.iloc[2:]) == [1, 1]
    assert metrics(exp)["SR_counter"] == pytest.approx((10 + 31) / (22 + 31))


def test_sr_counter_stays_within_the_evaluable_bins():
    # 12 bins de 4 pasos; corte a los 480 s (bins 0..7, 32 pasos). La tercera trama no llega: el contador de la
    # segunda (32) cubre toda la ventana, aunque la época tenga 48 pasos (antes salía 32 / 48)
    bins = [(0, s, s * DELTA, 4) for s in range(12)]
    frames = [dict(epoch=0, s1=4 * j, n=4, t=240 * (j + 1), bins=[4] * 4, cumulative=16 * (j + 1), fcnt=j,
                   received=j < 2) for j in range(3)]
    m = metrics(build(bins, frames, strategy=1, eval_s=480, duration_s=800))
    assert m["SR_counter"] == pytest.approx(1.0)
    assert m["SR_bins"] == pytest.approx(1.0)


# S0: pasos por bin; la trama F2 (bins 4..7, con 3 pasos) no llega. F3 cubre 8..11 con incremento 0 y F4, los bins
# 12..15, que quedan después del corte de evaluación (720 s)
S0_STEPS = [0, 5, 0, 0, 0, 0, 3, 0, 0, 0, 0, 0, 1, 1, 1, 1]
S0_FRAMES = [
    dict(epoch=0, s1=0, n=1, t=240, bins=[5], cumulative=5, fcnt=1),
    dict(epoch=0, s1=4, n=1, t=480, bins=[3], cumulative=8, fcnt=2, received=False),
    dict(epoch=0, s1=8, n=1, t=720, bins=[0], cumulative=8, fcnt=3),
    dict(epoch=0, s1=12, n=1, t=960, bins=[4], cumulative=12, fcnt=4),
]


def test_s0_false_zeros_over_the_interval_of_each_frame():
    exp = build([(0, s, s * DELTA, v) for s, v in enumerate(S0_STEPS)], S0_FRAMES, strategy=0, eval_s=720,
                duration_s=1000)
    m = metrics(exp)
    assert m["bins_generated"] == 12
    # sobre el intervalo de cada trama: ceros mostrados = 4..7 (sin trama) y 8..11 (incremento 0); falso, el 6
    assert m["FZR_naive"] == pytest.approx(1 / 8)
    assert m["FZR_aware"] == pytest.approx(0.0)                         # sin trama = "sin datos"
    # incremento en el último bin del intervalo: 11 ceros, falsos el 1 (pasos en el bin 3) y el 6
    assert m["FZR_naive_lastbin"] == pytest.approx(2 / 11)
    # pasos de la ventana cubiertos por una trama recibida (F4 queda fuera; antes se sumaba: 9 / 8)
    assert m["SR_increments"] == pytest.approx(5 / 8)
    assert m["S0_interval_coverage"] == pytest.approx(8 / 12)
    assert m["SR_bins"] == pytest.approx(0.0)
    assert m["SR_counter"] == pytest.approx(1.0)                        # el acumulado de F4 cubre los 8 pasos


def test_s0_interval_view_has_no_false_zeros_with_full_coverage():
    steps = [0, 5, 0, 0, 2, 0, 0, 0]
    frames = [dict(epoch=0, s1=0, n=1, t=240, bins=[5], cumulative=5, fcnt=1),
              dict(epoch=0, s1=4, n=1, t=480, bins=[2], cumulative=7, fcnt=2)]
    m = metrics(build([(0, s, s * DELTA, v) for s, v in enumerate(steps)], frames, strategy=0, eval_s=480,
                      duration_s=600))
    assert math.isnan(m["FZR_naive"])                                   # ningún bin se muestra como 0
    assert m["FZR_naive_lastbin"] == pytest.approx(2 / 6)              # bins 1 y 4 mostrados como 0


# ------------------------------------------------------------------------------- reloj del gateway
GT0 = pd.Timestamp("2026-10-19 13:00:00", tz="UTC")
DRIFT = -5e-6                 # deriva del reloj del nodo respecto al gateway
BOOT1, START = 100_000, 100_200   # arranque y orden de inicio, reloj del nodo (ms)
N_BINS, N_FRAMES = 60, 15     # por arranque: 1 h de bins y una trama cada 240 s
BOOT2 = BOOT1 + N_BINS * 60_000 + 600   # reinicio tras 1 h: el reloj del nodo sigue, pero salta 40 ms
STEP_MS = 40
JUMP = GT0 + pd.Timedelta(seconds=1500)   # el PC va 3 s por detrás y Windows lo adelanta 3,2 s
TOA, NET = 0.071, 0.232


def true_utc(node_ms: float) -> pd.Timestamp:
    step = STEP_MS if node_ms >= BOOT2 else 0
    return GT0 + pd.Timedelta(seconds=(node_ms - BOOT1 - step) / 1000 * (1 + DRIFT))


def pc(u: pd.Timestamp) -> pd.Timestamp:
    return u + pd.Timedelta(seconds=-3.0 if u < JUMP else 0.2)


def frame_payload(epoch, s1, n, age, cum, bins) -> str:
    raw = (bytes([0, epoch]) + s1.to_bytes(2, "big") + bytes([n]) + age.to_bytes(2, "big") + cum.to_bytes(4, "big")
           + b"".join(v.to_bytes(2, "big") for v in bins))
    return base64.b64encode(raw).decode()


def write_run(folder: pathlib.Path, gw_time: bool = True, ttn: bool = True) -> dict:
    """Prueba sintética de S1 en data/raw: node.log, ttn.jsonl y plan.json. Devuelve los instantes verdaderos."""
    lines, records = [], []
    cfg = "strategy=1,scenario=1,delta=60,period=240,k=4,n=40,seed=1,source=0,dr=5,adr=0,fup=1,buf=1440"
    lines.append((pc(true_utc(BOOT1 + 50)), f"EV,config,{cfg}"))
    lines.append((pc(true_utc(START)) + pd.Timedelta(milliseconds=2), f"EV,exp,start,{START}"))
    fcnt = 500
    last_close = 0
    for epoch, boot in ((7, BOOT1), (8, BOOT2)):
        steps = [(s * 7) % 11 for s in range(N_BINS)]
        cum = np.cumsum(steps)
        for s in range(N_BINS):
            close = boot + (s + 1) * 60_000
            lines.append((pc(true_utc(close)) + pd.Timedelta(milliseconds=3),
                          f"GT,{epoch},{s},{boot + s * 60_000},{steps[s]},{cum[s]},{s}"))
            last_close = max(last_close, close)
        for j in range(1, N_FRAMES + 1):          # una trama cada 240 s con los 4 bins cerrados
            t = boot + j * 240_000
            s1 = 4 * (j - 1)
            fcnt += 1
            lines.append((pc(true_utc(t)) + pd.Timedelta(seconds=6),
                          f"TX,{epoch},{fcnt},{s1},4,60,5,19,sent,{int(TOA * 1000)},{t}"))
            rx_end = true_utc(t) + pd.Timedelta(seconds=TOA)
            received = rx_end + pd.Timedelta(seconds=NET)
            md = {"rssi": -50, "snr": 10}
            if gw_time:
                md["time"] = rx_end.isoformat()
            records.append({"host_time": (pc(received) + pd.Timedelta(seconds=0.1)).isoformat(),
                            "topic": "v3/app@ttn/devices/node/up",
                            "msg": {"end_device_ids": {"dev_addr": "260BA8DC"}, "received_at": received.isoformat(),
                                    "uplink_message": {"f_port": 11, "f_cnt": fcnt, "rx_metadata": [md],
                                                       "frm_payload": frame_payload(epoch, s1, 4, 60, int(cum[s1 + 3]),
                                                                                    steps[s1:s1 + 4]),
                                                       "consumed_airtime": f"{TOA}s",
                                                       "received_at": received.isoformat()}}})
    end_node = last_close + 30_000
    lines.append((pc(true_utc(end_node)), "EV,status,joined=1"))
    lines.sort(key=lambda x: x[0])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "node.log").write_text("".join(f"{t.isoformat()}\t{text}\n" for t, text in lines), encoding="utf-8")
    (folder / "plan.json").write_text(json.dumps({"id": "SYN", "duration_s": 3800}), encoding="utf-8")
    if ttn:
        (folder / "ttn.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return dict(start=true_utc(START), end=true_utc(end_node))


def test_ground_truth_on_gateway_clock_with_pc_jump_and_reboot(tmp_path):
    truth = write_run(tmp_path / "SYN")
    exp = load_experiment(tmp_path / "SYN")
    assert exp.clock_ref == "gateway" and exp.clock_note == ""
    assert len(exp.clock_fit) == 2 and all(s["frames"] == N_FRAMES for s in exp.clock_fit)
    assert exp.clock_info["drift_ppm"] == pytest.approx(DRIFT * 1e6, abs=0.2)
    # inicio de cada bin, inicio y fin de la prueba en su hora verdadera (salto del PC y salto del nodo incluidos)
    err_bins = (exp.gt.t_start - exp.gt.t_start_ms.map(true_utc)).dt.total_seconds().abs()
    assert err_bins.max() < 0.002
    assert abs((exp.exp_start - truth["start"]).total_seconds()) < 0.002
    assert abs((exp.end - truth["end"]).total_seconds()) < 0.01
    # el reloj del PC se conserva para comparar con host_time
    assert abs((exp.host_start - (pc(truth["start"]) + pd.Timedelta(milliseconds=2))).total_seconds()) < 1e-6
    # horas del PC tras el salto -> reloj del gateway
    u = GT0 + pd.Timedelta(seconds=3000)
    assert abs((exp.host_to_utc([pc(u)])[0] - u).total_seconds()) < 0.01
    # error de la hora reconstruida: tiempo en el aire y latencia de red, en todas las tramas
    err = time_errors(exp)
    assert len(err) > 0 and err.between(TOA + NET - 0.005, TOA + NET + 0.005).all()
    m = metrics(exp)
    assert m["clock_ref"] == "gateway" and m["time_err_max_s"] == pytest.approx(err.max())
    assert m["TI"] == pytest.approx(1.0)


def test_pc_clock_fallback_without_gateway_time(tmp_path):
    write_run(tmp_path / "SYN", gw_time=False)
    with pytest.warns(UserWarning, match="reloj del PC"):
        exp = load_experiment(tmp_path / "SYN")
    assert exp.clock_ref == "pc" and not exp.clock_fit
    assert exp.exp_start == exp.host_start                 # como antes: hora del PC de la línea EV,exp,start
    # el desfase mínimo se toma antes del salto, con el PC 3 s por detrás: el error de hora pasa de 2,5 s
    assert time_errors(exp).min() > 2.5


def test_pc_clock_fallback_without_ttn_records(tmp_path):
    write_run(tmp_path / "SYN", ttn=False)
    with pytest.warns(UserWarning, match="reloj del PC"):
        exp = load_experiment(tmp_path / "SYN")
    assert exp.clock_ref == "pc" and exp.uplinks.empty
    m = metrics(exp)
    assert m["SR_counter"] == pytest.approx(0.0) and m["bins_received"] == 0


def test_pc_clock_on_request(tmp_path):
    write_run(tmp_path / "SYN")
    exp = load_experiment(tmp_path / "SYN", clock="pc")
    assert exp.clock_ref == "pc" and exp.clock_note == ""
