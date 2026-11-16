"""Resumen para el profesional clínico (vista diaria y semanal), calculado solo con lo que llega por la red.

El tiempo se divide en franjas de Δ (15 min a escala real). Cada franja tiene un estado:
    observed      dato recibido (también cuando vale 0)
    aggregate     S0: la franja está cubierta por un total de varias franjas, sin detalle
    pending       hueco que todavía puede llegar (S2 dentro de la redundancia; S3 y S4 mientras quepa en el
                  buffer del nodo)
    lost          hueco que ya no va a llegar (S1; S2 fuera de la redundancia; S3 y S4, si ya no cabe en el buffer)
    no_contact    sin noticias del dispositivo (antes del primer dato o después del último)
Los huecos se etiquetan con la misma regla que la exportación FHIR (hij.fhir.gap_state y lost_bins, es decir, la
regla de capacidad del ACK de la S3 con la B de la prueba).
En el laboratorio el tiempo va comprimido; aquí se pasa a tiempo "virtual" del paciente (x15 con Δ = 60 s) para
que los días y las horas se lean como en un despliegue real.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .fhir import gap_state, lost_bins, node_buffer_bins
from .payload import FLAG_OVERFLOW, FLAG_RETX
from .quality import received_copies, s0_intervals

SLOT_REAL_S = 900
SLOTS_PER_DAY = 96


@dataclass
class ClinicalSummary:
    slots: pd.DataFrame      # una fila por franja: day, slot, t (virtual), state, value
    days: pd.DataFrame       # una fila por día
    sessions: pd.DataFrame   # conexiones (grupos de uplinks seguidos)
    events: list             # incidentes en texto
    t_now: pd.Timestamp      # instante virtual de la evaluación
    last_contact: pd.Timestamp | None
    strategy: int
    buffer_bins: int | None = None  # capacidad B del buffer del nodo (franjas de 15 min)


def _to_virtual(exp, t: pd.Timestamp, v0: pd.Timestamp) -> pd.Timestamp:
    return v0 + (t - exp.exp_start) * exp.compression


def summarize(exp, T: pd.Timestamp | None = None, virtual_start: pd.Timestamp | None = None,
              buffer_bins: int | None = None) -> ClinicalSummary:
    """buffer_bins: capacidad B del buffer del nodo; por defecto, la de la prueba (hij.fhir.node_buffer_bins)."""
    T = exp.end if T is None else T
    v0 = virtual_start if virtual_start is not None else exp.exp_start.normalize()
    c = exp.compression
    strategy = exp.config.get("strategy", 1)
    k_red = exp.config.get("k", 4)
    period = exp.config.get("period", 240)
    B = node_buffer_bins(exp) if buffer_bins is None else buffer_bins
    n_slots = int(np.ceil((T - exp.exp_start).total_seconds() / exp.delta_s))
    t_slots = [v0 + pd.Timedelta(seconds=i * SLOT_REAL_S) for i in range(n_slots)]
    state = np.array(["no_contact"] * n_slots, dtype=object)
    value = np.full(n_slots, np.nan)

    def slot_of(t_lab: pd.Timestamp) -> int:
        return int(round((t_lab - exp.exp_start).total_seconds() / exp.delta_s))

    ups = exp.uplinks[exp.uplinks.received_at <= T] if not exp.uplinks.empty else exp.uplinks
    if strategy == 0:
        for r in s0_intervals(exp).itertuples():
            if r.arrival > T:
                continue
            # el inicio del intervalo sale de la edad de la trama
            u = ups[(ups.epoch == r.epoch) & (ups.s1 == r.s_first)]
            if u.empty:
                continue
            u = u.iloc[0]
            t_first = u.received_at - pd.Timedelta(seconds=u.age_units * exp.delta_s / 15)
            a = slot_of(t_first)
            for i in range(a, a + (r.s_last - r.s_first) + 1):
                if 0 <= i < n_slots:
                    state[i] = "aggregate"
    else:
        cp = received_copies(exp)
        cp = cp[cp.arrival <= T].sort_values("arrival").groupby(["epoch", "seq"], as_index=False).first()
        # S3 y S4: un hueco solo es perdido si ya no cabe en el buffer del nodo (la regla del ACK de la S3)
        gone = (lost_bins(ups.sort_values("received_at", kind="stable"), B)
                if strategy >= 3 and not ups.empty else {})
        for epoch, g in cp.groupby("epoch"):
            g = g.dropna(subset=["t_hat"]).sort_values("seq")
            if g.empty:
                continue
            gone_e = gone.get(int(epoch), set())
            idx = {s: slot_of(t) for s, t in zip(g.seq, g.t_hat)}
            for r in g.itertuples():
                i = idx[r.seq]
                if 0 <= i < n_slots:
                    state[i], value[i] = "observed", r.value
            # huecos entre el primer y el último bin recibido de la época
            ref_s, ref_i = g.seq.iloc[0], idx[g.seq.iloc[0]]
            s_max = g.seq.max()
            for s in range(g.seq.min(), s_max + 1):
                if s in idx:
                    continue
                i = ref_i + (s - ref_s)
                if 0 <= i < n_slots and state[i] == "no_contact":
                    state[i] = gap_state(strategy, int(s_max - s), k_red, int(s) in gone_e)
    slots = pd.DataFrame(dict(slot=range(n_slots), t=t_slots, state=state, value=value))
    slots["day"] = slots.slot // SLOTS_PER_DAY
    slots["hour"] = (slots.slot % SLOTS_PER_DAY) / 4

    # días (el último, si no ha terminado, es el día en curso)
    days = []
    last_day = int(slots.day.max()) if len(slots) else -1
    for d, g in slots.groupby("day"):
        counts = g.state.value_counts()
        obs = int(counts.get("observed", 0))
        n = len(g)
        steps = float(np.nansum(g.value)) if obs else 0.0
        if counts.get("aggregate", 0):
            status = "aggregate"
        elif obs == SLOTS_PER_DAY:
            status = "inactive" if steps == 0 else "complete"
        elif d == last_day and n < SLOTS_PER_DAY or (d == last_day and counts.get("no_contact", 0) and
                                                     g.state.iloc[-1] == "no_contact"):
            status = "ongoing"
        elif obs == 0:
            status = "no_data"
        else:
            status = "partial"
        days.append(dict(day=int(d), date=g.t.iloc[0].normalize(), slots=n, observed=obs,
                         pending=int(counts.get("pending", 0)), lost=int(counts.get("lost", 0)),
                         no_contact=int(counts.get("no_contact", 0)), aggregate=int(counts.get("aggregate", 0)),
                         steps=steps, status=status))
    days = pd.DataFrame(days)

    # sesiones de conexión: uplinks separados por menos de dos periodos
    sessions = []
    if not ups.empty:
        u = ups.sort_values("received_at")
        gap = u.received_at.diff().dt.total_seconds().fillna(0) > 2.5 * period
        for _, g in u.groupby(gap.cumsum()):
            sessions.append(dict(start=_to_virtual(exp, g.received_at.iloc[0], v0),
                                 end=_to_virtual(exp, g.received_at.iloc[-1], v0), frames=len(g),
                                 bins=int(g.n.sum()), rssi_min=g.rssi.min(), rssi_max=g.rssi.max()))
    sessions = pd.DataFrame(sessions)
    last_contact = _to_virtual(exp, ups.received_at.max(), v0) if not ups.empty else None

    # incidentes
    events = []
    if not ups.empty:
        epochs = list(dict.fromkeys(ups.sort_values("received_at").epoch))
        if len(epochs) > 1:
            events.append(f"El dispositivo se reinició {len(epochs) - 1} vez/veces; los datos guardados se conservaron.")
        if (ups["flags"] & FLAG_OVERFLOW).any():
            events.append("La memoria del dispositivo se llenó durante un corte: se perdieron los datos más antiguos.")
        if (ups["flags"] & FLAG_RETX).any() and strategy >= 3:
            events.append("Parte de los datos llegó con retraso, al recuperarse la conexión.")
    if len(sessions) > 1:
        gaps = (sessions.start.iloc[1:].values - sessions.end.iloc[:-1].values) / np.timedelta64(1, "h")
        events.append(f"El corte de conexión más largo duró {gaps.max():.0f} h.")
    return ClinicalSummary(slots, days, sessions, events, _to_virtual(exp, T, v0), last_contact, strategy, B)


def weekly_text(s: ClinicalSummary) -> str:
    d = s.days
    full = d[d.status.isin(["complete", "inactive"])]
    parts = [f"{len(full)} de {len(d)} días completos."]
    measured = d[d.observed > 0]
    if len(full):
        parts.append(f"Media en los días completos: {full.steps.mean():,.0f} pasos/día.".replace(",", "."))
    partial = d[d.status == "partial"]
    if len(partial):
        pend = int(partial.pending.sum())
        lost = int(partial.lost.sum())
        txt = f"{len(partial)} día(s) parcial(es)"
        if pend:
            txt += f", con {pend * 15 / 60:.1f} h de datos aún pendientes de llegar"
        if lost:
            txt += f", y {lost * 15 / 60:.1f} h perdidas"
        parts.append(txt + ".")
    nod = d[d.status == "no_data"]
    if len(nod):
        parts.append(f"{len(nod)} día(s) sin datos (no hubo conexión; no equivale a 0 pasos).")
    if s.last_contact is not None:
        h = (s.t_now - s.last_contact).total_seconds() / 3600
        if h >= 1:
            parts.append(f"Última conexión hace {h:.0f} h.")
            if s.strategy >= 3 and s.buffer_bins and h * 3600 > s.buffer_bins * SLOT_REAL_S:
                cap_h = s.buffer_bins * SLOT_REAL_S / 3600
                cap = f"los últimos {cap_h / 24:g} días" if cap_h >= 48 else f"las últimas {cap_h:g} h"
                parts.append(f"El dispositivo solo guarda {cap}: lo anterior se ha perdido y lo demás llegará en la "
                             "próxima conexión.")
            elif s.strategy >= 3:
                parts.append("El dispositivo guarda los datos de ese tiempo y los enviará en la próxima conexión.")
            elif h >= 2:
                parts.append("Los datos de ese tiempo podrían no llegar (esta configuración no los reenvía).")
        else:
            parts.append("Conectado en la última hora.")
    parts.extend(s.events)
    return " ".join(parts)
