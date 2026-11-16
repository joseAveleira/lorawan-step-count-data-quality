"""Simulador de eventos discretos del nodo hij_node, la red LoRaWAN (TTN) y el backend.

Reproduce la lógica del firmware (bins, buffer persistente, estrategias S0 a S3, protocolo de ACK, escenarios
de cobertura, reinicios) y las restricciones de red: tiempo en el aire de LoRa, límite de airtime de RadioLib
(fair use policy de TTN) y presupuesto de downlinks. Devuelve un objeto Experiment como el de los datos reales,
así que las métricas se calculan con el mismo código (backend/hij/quality.py).

Las unidades de tiempo son segundos de simulación. Para reproducir un experimento del laboratorio se usan los
mismos parámetros que el nodo (Δ = 60 s, periodo 240 s); para un despliegue real, Δ = 900 s y periodo 3600 s.

Pérdidas: sin cobertura emulada (escenarios C2 a C5) la trama no sale al aire ('suppressed', sin airtime y sin pasar
por el límite de airtime), como en el firmware. Las pérdidas de radio (radio_loss) y los cortes reales (gw_block: el
gateway descarta las tramas del nodo) afectan a tramas transmitidas: estado 'sent', con su airtime y su paso por el
límite, pero el backend no las recibe y no llega ningún downlink.
"""
import math
import pathlib
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
from hij.ack import AckTracker  # noqa: E402
from hij.experiment import Experiment  # noqa: E402
from hij.payload import (AGE_UNKNOWN, FLAG_EPOCH_END, FLAG_OVERFLOW, FLAG_RETX, HDR_LEN, Frame)  # noqa: E402
from hij.synthetic import mix32, synthetic_steps  # noqa: E402

# EU868: factor de ensanchamiento y carga útil máxima de aplicación por DR (sin FOpts)
DR_SF = {0: 12, 1: 11, 2: 10, 3: 9, 4: 8, 5: 7}
DR_MAX_PAYLOAD = {0: 51, 1: 51, 2: 51, 3: 115, 4: 222, 5: 222}
LORAWAN_OVERHEAD = 13  # MHDR + FHDR + FPort + MIC
EU868_DUTY_MS_PER_HOUR = 36000  # 1 %
GW_RESTART_S = 20      # corte real: el forwarder se reinicia al quitar el filtro (como hij.quality.outage_windows)


def lora_toa_ms(phy_len: int, sf: int, bw: float = 125e3, cr: int = 1, preamble: int = 8) -> float:
    """Tiempo en el aire (Semtech AN1200.13): cabecera explícita, CRC y LDRO en SF11 y SF12 a 125 kHz."""
    t_sym = (2 ** sf) / bw * 1000
    de = 1 if (sf >= 11 and bw == 125e3) else 0
    num = 8 * phy_len - 4 * sf + 28 + 16
    n_payload = 8 + max(math.ceil(num / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (preamble + 4.25) * t_sym + n_payload * t_sym


def radiolib_fup_delay_ms(ms_per_hour: int, toa_ms: int) -> float:
    """Espera tras una trama según RadioLib (LoRaWANNode::dutyCycleInterval), con su división entera."""
    if ms_per_hour == 0 or toa_ms == 0:
        return 0.0
    n_packets = ms_per_hour // toa_ms
    if n_packets == 0:
        return float("inf")
    return 2 + 3600000 / n_packets - toa_ms


@dataclass
class SimConfig:
    strategy: int
    duration_s: int
    seed: int = 20261016
    delta_s: int = 60
    period_s: int = 240
    k: int = 4
    n_ack: int = 40
    buf: int = 1440
    dr: int = 5
    fup_ms_per_hour: int = 1250          # 0 = solo el duty cycle legal del 1 %
    downlinks_per_window: int = 10       # presupuesto de downlinks...
    downlink_window_s: float = 86400     # ...en esta ventana (24 h reales)
    scenario: int = 1
    cycle: int = 5760
    window: int = 600
    ge_loss: int = 10
    ge_burst: int = 3
    out_start: int = 1800
    out_len: int = 5760
    reboots_at: tuple = ()               # instantes (s desde el inicio) de reinicios por software
    radio_loss: float = 0.0              # pérdida natural del enlace (Bernoulli por trama)
    gw_block: tuple = ()                 # cortes reales: (inicio, fin) en s desde el inicio en que el gateway descarta
                                         # las tramas del nodo, más GW_RESTART_S tras el fin (tramas transmitidas)
    network_latency_s: float = 0.3       # de fin de la trama a recepción en el backend
    ack_same_window: bool = True         # el ACK llega en la ventana RX de la misma trama (medido en TTN)
    s4_fup: bool = True                  # S4: pedir confirmación solo mientras quede presupuesto de downlinks
    rng_seed: int = 1
    label: str = ""
    eval_s: int | None = None             # inicio del vaciado final con cobertura continua
    tx_lead_s: float = 0.5                # el nodo arranca este tiempo antes del inicio del experimento y ancla al
                                          # arranque sus bins y sus envíos; solo fija la fase (en el laboratorio, en el
                                          # reloj del nodo, de 0,17 a 0,29 s: validate_lab.py lo mide en cada prueba)
    ack_capacity_rule: bool = True        # backend: bins perdidos por la capacidad del buffer (False: por FLAG_RETX,
                                          # la regla anterior; igual en todo el laboratorio salvo C5-overflow)
    visit_offset_s: int = 0               # C2: inicio de la primera visita respecto al inicio (0 = como el firmware)
    loss_in_window: bool = False          # C2: pérdidas en ráfaga de C3 (ge_loss, ge_burst) durante las visitas


@dataclass
class _Buf:
    cap: int
    bins: list = field(default_factory=list)   # (epoch, seq, steps, t_start)
    sent: int = 0
    acked: int = 0
    sent_high: int = 0
    overflow: bool = False

    def drop_front(self, m):
        m = min(m, len(self.bins))
        del self.bins[:m]
        self.sent = max(self.sent - m, 0)
        self.acked = max(self.acked - m, 0)
        self.sent_high = max(self.sent_high - m, 0)

    def segment_bounds(self, idx):
        e = self.bins[idx][0]
        a = idx
        while a > 0 and self.bins[a - 1][0] == e:
            a -= 1
        b = idx
        while b < len(self.bins) and self.bins[b][0] == e:
            b += 1
        return a, b


def ge_lost(cfg: SimConfig, tx_index: int, ge_state: list) -> bool:
    """Un paso de la cadena de Gilbert-Elliott de C3 (True = trama perdida), como en el firmware."""
    pi = cfg.ge_loss / 100
    r = 1 / max(cfg.ge_burst, 1)
    p = 1.0 if pi >= 1 else r * pi / (1 - pi)
    u = mix32(cfg.seed ^ 0xC3A5C85C ^ ((0x9E3779B9 * (tx_index + 1)) & 0xFFFFFFFF)) / 4294967296.0
    ge_state[0] = (u >= r) if ge_state[0] else (u < p)
    return ge_state[0]


def coverage(cfg: SimConfig, t: float, tx_index: int, ge_state: list) -> bool:
    """Misma lógica que firmware/hij_node/src/scenario.cpp (t en s desde el inicio del experimento).

    Solo en el simulador: visit_offset_s desplaza las visitas de C2 y loss_in_window añade dentro de ellas las
    pérdidas en ráfaga de C3 (la cadena solo avanza con las tramas enviadas durante una visita)."""
    if cfg.eval_s is not None and t >= cfg.eval_s:
        return True  # vaciado final: el orquestador pasa el nodo a C1
    if cfg.scenario == 2:
        inside = ((int(t) - cfg.visit_offset_s) % cfg.cycle) < cfg.window
        return inside and not (cfg.loss_in_window and ge_lost(cfg, tx_index, ge_state))
    if cfg.scenario == 3:
        return not ge_lost(cfg, tx_index, ge_state)
    if cfg.scenario in (4, 5):
        return not (cfg.out_start <= t < cfg.out_start + cfg.out_len)
    return True


def simulate(cfg: SimConfig) -> Experiment:
    rng = np.random.default_rng(cfg.rng_seed)
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    buf = _Buf(cfg.buf)
    tracker, gt, tx, ups = AckTracker(cfg.buf if cfg.ack_capacity_rule else None), [], [], []
    evs = []                               # downlinks entregados: ACK de la S3 y confirmaciones de la S4
    epoch, open_seq, cumulative = 0, 0, 0
    boot_t = -cfg.tx_lead_s
    open_start = boot_t
    next_tx = boot_t + cfg.period_s
    next_allowed = 0.0
    fcnt = 0
    tx_index = 0
    ge = [False]
    s0_cum, s0_first = 0, 0
    s3_since_ack, s3_frames = 0, 0
    prev_last, cur_last = None, None
    pending_ack = None                     # ACK en la cola de TTN, se entrega en la siguiente ventana RX
    backend_s3_since_ack = 0
    downlink_times = []
    reboots = sorted(cfg.reboots_at)
    max_bins = (DR_MAX_PAYLOAD[cfg.dr] - HDR_LEN) // 2
    ms_per_hour = cfg.fup_ms_per_hour or EU868_DUTY_MS_PER_HOUR

    def k_index(t_start):
        rel = t_start  # el experimento empieza en t = 0
        return int(math.floor(rel / cfg.delta_s + 0.5))

    def push(e, s, steps, t_start):
        lost_first = None
        if len(buf.bins) >= buf.cap:
            done = buf.acked > 0 if cfg.strategy >= 3 else buf.sent > 0
            if not done:
                buf.overflow = True
                lost_first = buf.bins[0][:2]
            buf.drop_front(1)
        buf.bins.append((e, s, steps, t_start))
        return lost_first

    def release():
        if cfg.strategy >= 3:
            m = buf.acked
        elif cfg.strategy == 2:
            m = max(buf.sent - cfg.k, 0)
        else:
            m = buf.sent
        buf.drop_front(m)

    def close_bins(until):
        nonlocal open_seq, open_start, cumulative
        while open_start + cfg.delta_s <= until:
            k = k_index(open_start)
            steps = synthetic_steps(cfg.seed, k, 60)  # pasos por bin de 15 min reales, como en el laboratorio
            cumulative += steps
            gt.append(dict(epoch=epoch, seq=open_seq, t_start_ms=int(open_start * 1000), steps=steps,
                           cumulative=cumulative, k=k))
            push(epoch, open_seq, steps, open_start)
            open_seq = (open_seq + 1) & 0xFFFF
            open_start += cfg.delta_s

    def age_units(t_start, now):
        # en ms enteros, como el firmware (ageUnits): el floor de una resta en coma flotante podía dar una unidad de
        # menos cuando tx_lead_s no tiene representación exacta
        ms = int(round((now - t_start) * 1000))
        return min(ms * 15 // (cfg.delta_s * 1000), AGE_UNKNOWN - 1)

    def gw_blocked(t):
        return any(a <= t < b + GW_RESTART_S for a, b in cfg.gw_block)

    def build():
        nonlocal s0_first
        if cfg.strategy == 0:
            if s0_first == open_seq:
                return None
            inc = min(cumulative - s0_cum, 0xFFFF)
            t_first = boot_t + ((s0_first - 0) & 0xFFFF) * cfg.delta_s
            return dict(epoch=epoch, s1=s0_first, n=1, bins=[inc], t_first=t_first, end_idx=len(buf.bins), flags=0)
        first = buf.sent
        if first >= len(buf.bins):
            return None
        a, b = buf.segment_bounds(first)
        n_new = min(b - first, max_bins)
        n_old = min(cfg.k if cfg.strategy == 2 else 0, max_bins - n_new, first - a)
        start = first - n_old
        flags = FLAG_RETX if (n_old > 0 or start < buf.sent_high) else 0
        end_idx = first + n_new
        if end_idx == b and buf.bins[start][0] != epoch:
            flags |= FLAG_EPOCH_END
        sel = buf.bins[start:end_idx]
        return dict(epoch=sel[0][0], s1=sel[0][1], n=len(sel), bins=[x[2] for x in sel], t_first=sel[0][3],
                    end_idx=end_idx, flags=flags, last=(sel[-1][0], sel[-1][1]))

    def frame_done(f):
        nonlocal s0_cum, s0_first, s3_since_ack, s3_frames, prev_last, cur_last
        buf.overflow = False
        if cfg.strategy == 0:
            s0_cum, s0_first = cumulative, open_seq
        buf.sent = max(buf.sent, min(f["end_idx"], len(buf.bins)))
        buf.sent_high = max(buf.sent_high, buf.sent)
        if cfg.strategy == 3:
            prev_last, cur_last = cur_last, f["last"]
            s3_frames += 1
            s3_since_ack += 1
            if s3_since_ack >= 2 * cfg.n_ack + 2 and buf.sent > buf.acked:
                buf.sent = buf.acked
                s3_since_ack = 0
        release()

    def deliver_ack(point):
        nonlocal s3_since_ack
        e, s = point
        idx = next((i for i, b in enumerate(buf.bins) if b[0] == e and b[1] == s), -1)
        if idx >= 0:
            buf.acked = max(buf.acked, idx + 1)
            buf.sent = max(buf.sent, buf.acked)
            buf.sent_high = max(buf.sent_high, buf.sent)
        s3_since_ack = 0
        if cfg.strategy == 3 and s3_frames >= 2 and prev_last is not None:
            pidx = next((i for i, b in enumerate(buf.bins) if (b[0], b[1]) == prev_last), -1)
            if pidx >= 0 and (idx < 0 or idx < pidx) and buf.sent > buf.acked:
                buf.sent = buf.acked
        release()

    def backend_receive(fr: Frame, t_rx):
        nonlocal backend_s3_since_ack, pending_ack
        tracker.add(fr)
        if fr.strategy != 3:
            return
        backend_s3_since_ack += 1
        # como collect_ttn._on_uplink: ACK si falta algo delante de la trama, y si no, cada N uplinks
        want = tracker.beyond_point(fr) or backend_s3_since_ack >= cfg.n_ack
        target = tracker.ack_target()  # como collect_ttn.send_ack: sin el primer bin, "nada confirmado"
        if want and target is not None:
            recent = [x for x in downlink_times if t_rx - x < cfg.downlink_window_s]
            if len(recent) < cfg.downlinks_per_window:
                downlink_times[:] = recent + [t_rx]
                pending_ack = target
                backend_s3_since_ack = 0

    t = 0.0
    while t < cfg.duration_s:
        # siguiente evento: transmisión o reinicio
        t_next = min(next_tx, reboots[0] if reboots else math.inf, cfg.duration_s)
        close_bins(t_next)
        t = t_next
        if t >= cfg.duration_s:
            break
        if reboots and t >= reboots[0]:
            reboots.pop(0)
            epoch = (epoch + 1) & 0xFF
            open_seq, cumulative, boot_t, open_start = 0, 0, t, t
            s0_cum, s0_first, s3_since_ack, s3_frames, prev_last, cur_last = 0, 0, 0, 0, None, None
            ge = [False]
            tx_index = 0
            next_tx = t + cfg.period_s
            continue
        # transmisión (como el firmware: si el límite de airtime bloquea menos de 15 s, se espera)
        next_tx += cfg.period_s
        if 0 < next_allowed - t <= 15:
            t = next_allowed
            close_bins(t)
        f = build()
        if f is None:
            continue
        if buf.overflow:
            f["flags"] |= FLAG_OVERFLOW
        n_bytes = HDR_LEN + 2 * f["n"]
        toa = lora_toa_ms(n_bytes + LORAWAN_OVERHEAD, DR_SF[cfg.dr])
        tx_index += 1
        age = age_units(f["t_first"], t)
        row = dict(epoch=f["epoch"], fcnt=-1, s1=f["s1"], n=f["n"], age_units=age, dr=-1, bytes=n_bytes,
                   status="", toa_ms=0, t_ms=int(t * 1000))
        # S4: uplink confirmado si queda presupuesto de downlinks (o siempre, sin respetar la FUP)
        s4_confirm = False
        if cfg.strategy == 4:
            recent = [x for x in downlink_times if t - x < cfg.downlink_window_s]
            s4_confirm = (not cfg.s4_fup) or len(recent) < cfg.downlinks_per_window
        if not coverage(cfg, t, tx_index, ge):
            if not s4_confirm:
                frame_done(f)  # en S4 con confirmación no llega el ACK: los bins siguen pendientes
                if cfg.strategy == 4:
                    buf.acked = max(buf.acked, buf.sent)
                    release()
            row.update(status="suppressed", dr=cfg.dr)
            tx.append(row)
            continue
        if t < next_allowed:
            row.update(status="dropped")
            tx.append(row)
            continue
        toa_i = int(toa)  # RadioLib trunca a ms enteros (calculateTimeOnAir / 1000), también para su límite
        next_allowed = t + toa / 1000 + radiolib_fup_delay_ms(ms_per_hour, toa_i) / 1000
        delivered = rng.random() >= cfg.radio_loss
        if gw_blocked(t):
            delivered = False  # corte real: la trama sale al aire y el gateway la descarta
        if cfg.strategy == 4:
            if s4_confirm:
                if delivered:  # ACK de LoRaWAN: los bins de la trama quedan confirmados
                    downlink_times.append(t)
                    evs.append(dict(host_time=t0 + pd.Timedelta(seconds=t), type="confirm", detail=str(fcnt)))
                    buf.acked = max(buf.acked, min(f["end_idx"], len(buf.bins)))
                    buf.sent = max(buf.sent, buf.acked)
                    buf.sent_high = max(buf.sent_high, buf.sent)
                    release()
            else:  # sin presupuesto: se envía sin confirmar y los bins se dan por entregados (como S1)
                frame_done(f)
                buf.acked = max(buf.acked, buf.sent)
                release()
        else:
            frame_done(f)
        row.update(status="sent", fcnt=fcnt, dr=cfg.dr, toa_ms=toa_i)
        tx.append(row)
        this_fcnt = fcnt
        fcnt += 1
        # un ACK pendiente se entrega en la ventana RX de esta trama (si la trama llega a la red)
        if delivered and pending_ack is not None and not cfg.ack_same_window:
            ack, pending_ack = pending_ack, None
            deliver_ack(ack)
            evs.append(dict(host_time=t0 + pd.Timedelta(seconds=t), type="ack", detail=f"{ack[0]},{ack[1]}"))
        if delivered:
            t_rx = t + toa / 1000 + cfg.network_latency_s
            fr = Frame(cfg.strategy, f["flags"], f["epoch"], f["s1"], f["n"], age, cumulative, f["bins"])
            ups.append(dict(received_at=t0 + pd.Timedelta(seconds=t_rx), fcnt=this_fcnt, fport=10 + cfg.strategy,
                            strategy=cfg.strategy, flags=f["flags"], epoch=f["epoch"], s1=f["s1"], n=f["n"],
                            age_units=age, cumulative=cumulative, bins=f["bins"], rssi=None, snr=None,
                            airtime_s=toa / 1000))
            backend_receive(fr, t_rx)
            if pending_ack is not None and cfg.ack_same_window:
                ack, pending_ack = pending_ack, None
                deliver_ack(ack)
                evs.append(dict(host_time=t0 + pd.Timedelta(seconds=t), type="ack", detail=f"{ack[0]},{ack[1]}"))

    gt_df = pd.DataFrame(gt)
    tx_df = pd.DataFrame(tx, columns=["epoch", "fcnt", "s1", "n", "age_units", "dr", "bytes", "status", "toa_ms", "t_ms"])
    config = dict(strategy=cfg.strategy, scenario=cfg.scenario, delta=cfg.delta_s, period=cfg.period_s, k=cfg.k,
                  n=cfg.n_ack, buf=cfg.buf)
    exp = Experiment(pathlib.Path(cfg.label or f"sim_S{cfg.strategy}_C{cfg.scenario}"),
                     dict(id=cfg.label or "sim", eval_s=cfg.eval_s, duration_s=cfg.duration_s),
                     config, cfg.delta_s, t0, t0 + pd.Timedelta(seconds=cfg.duration_s), t0.timestamp(),
                     gt_df, tx_df, pd.DataFrame(evs, columns=["host_time", "type", "detail"]),
                     pd.DataFrame(ups), pd.DataFrame(columns=["time", "fcnt", "copies"]))
    if cfg.gw_block:  # cortes reales, como los registra el orquestador (hij.quality.outage_windows añade GW_RESTART_S)
        exp.gw_actions = pd.DataFrame([dict(time=t0 + pd.Timedelta(seconds=x), action=act)
                                       for a, b in sorted(cfg.gw_block) for x, act in ((a, "block"), (b, "unblock"))])
    if not gt_df.empty:
        gt_df["t_start"] = exp.node_to_utc(gt_df.t_start_ms)
        gt_df["t_end"] = gt_df["t_start"] + pd.Timedelta(seconds=cfg.delta_s)
    if not tx_df.empty:
        tx_df["t_tx"] = exp.node_to_utc(tx_df.t_ms)
    return exp
