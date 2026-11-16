"""Representación de la serie reconstruida en FHIR R4 (RQ3).

Para un instante de evaluación T, el backend publica un Bundle con una Observation por cada intervalo de Δ que
conoce (los recibidos y los huecos entre ellos) y un Provenance por cada uplink que aporta valores:

    recibido a tiempo           status = final,       valueQuantity (también cuando es 0)
    recibido tras publicarse    status = final,       valueQuantity: versión nueva del mismo recurso (mismo id)
      como pendiente              (amended no se usa: FHIR lo reserva para cambios después de final)
    hueco recuperable           status = registered,  dataAbsentReason = temp-unknown
      (registered: el intervalo existe, pero aún no hay resultado. S2: aún puede llegar en la
      redundancia; S3 y S4: aún cabe en el buffer del nodo)
    hueco perdido               status = final,       dataAbsentReason = unknown
      (S1; S2 fuera de la redundancia; S3 y S4, si ya no cabe en el buffer del nodo)

Huecos de la S3 y la S4: se usa la misma regla que el ACK de la S3 (hij.ack.AckTracker, alimentado con los uplinks
publicados hasta T). Con la capacidad B del buffer del nodo (node_buffer_bins: el último `set buf` del plan, como
el receptor de TTN de run.py, o el `buf` de la línea EV,config), un bin que no ha llegado solo se da por perdido si
ya no cabe en el buffer; si cabe, sigue pendiente. Sin B, AckTracker aplica su regla de FLAG_RETX.

issued (y Provenance.recorded) es el instante en que el backend publica la versión, que es lo que define FHIR
("made available to providers"). En este sistema cada versión se crea sola, sin revisión, al recibir la trama, así
que por defecto issued = recepción en TTN (received_at). Si el backend recupera tramas más tarde (por ejemplo, de la
Storage Integration de TTN o del registro del gateway tras una caída del backend), published_at (FCnt -> instante)
da la hora a la que se publicaron: issued y recorded pasan a ser esa hora (nunca anterior a la recepción, porque los
relojes del PC y de TTN pueden diferir unos segundos) y el Bundle de un instante T solo incluye lo publicado hasta
T. La hora de recepción sigue en el Provenance (entity.what.display) y t̂ se calcula siempre con ella.

El tiempo del intervalo se reconstruye con la edad de la trama (t̂(s1) = recepción - edad). Para los huecos se
interpola en la rejilla de Δ desde el primer bin de la misma época con hora conocida.

Solo usa lo que sabe el backend (no la verdad terreno), así que el mismo código sirve en tiempo real.

Códigos y displays comprobados con las definiciones oficiales de FHIR R4 (4.0.1) y, para LOINC, con tx.fhir.org.
"""
import uuid

import pandas as pd

from .ack import AckTracker
from .payload import FLAG_RETX, Frame
from .quality import received_copies

LOINC_STEPS = {"system": "http://loinc.org", "code": "55423-8",
               "display": "Number of steps in unspecified time Pedometer"}
CATEGORY_ACTIVITY = {"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "activity",
                     "display": "Activity"}
DAR = "http://terminology.hl7.org/CodeSystem/data-absent-reason"
DAR_DISPLAY = {"temp-unknown": "Temporarily Unknown", "unknown": "Unknown"}
UCUM = "http://unitsofmeasure.org"
ACTIVITY_CREATE = {"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation", "code": "CREATE",
                   "display": "create"}
# "device" no existe en provenance-participant-type (ni en R4 ni en THO 2.0.0). performer: "A person, animal,
# organization or device that who actually and principally carries out the activity".
AGENT_PERFORMER = {"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                   "code": "performer", "display": "Performer"}
STATUS = {"final": "final", "late": "final", "pending": "registered", "lost": "final"}
NS = uuid.UUID("6f5a3c1e-7a0b-4c55-9a52-2c1b4d7e9f10")  # espacio de nombres para identificadores estables


def _uid(*parts) -> str:
    return str(uuid.uuid5(NS, "/".join(map(str, parts))))


def _iso(t: pd.Timestamp) -> str:
    return t.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fcnt(x) -> int:
    """FCnt como entero. TTN no incluye f_cnt en el JSON cuando vale 0, así que un FCnt ausente es 0."""
    return 0 if x is None or pd.isna(x) else int(x)


def node_buffer_bins(exp) -> int | None:
    """Capacidad B del buffer del nodo en la prueba: el último `set buf N` del plan (lo mismo que pasa run.py al
    receptor de TTN para los ACK) o, si el plan no lo fija, el `buf` de la línea EV,config del nodo."""
    values = []
    for cmd in (exp.plan or {}).get("setup", []):
        p = str(cmd).split()
        if p[:2] == ["set", "buf"] and len(p) > 2 and p[2].isdigit():
            values.append(int(p[2]))
    if values:
        return values[-1]
    b = (exp.config or {}).get("buf")
    return int(b) if b is not None else None


def _published(fcnt, received: pd.Timestamp, published_at: dict | None) -> pd.Timestamp:
    """Instante en que el backend publicó lo que trajo la trama: la recepción o, si published_at da otra hora para
    ese FCnt (trama recuperada más tarde), esa hora, nunca anterior a la recepción."""
    if published_at:
        t = published_at.get(_fcnt(fcnt))
        if t is not None and not pd.isna(t):
            t = pd.Timestamp(t)
            t = t.tz_localize("UTC") if t.tzinfo is None else t
            return max(received, t)
    return received


def known_uplinks(exp, T: pd.Timestamp, published_at: dict | None = None) -> pd.DataFrame:
    """Uplinks que el backend ha publicado hasta T, en el orden en que los publicó, con la columna published."""
    ups = exp.uplinks
    if ups.empty:
        return ups
    ups = ups.assign(published=[_published(f, a, published_at) for f, a in zip(ups.fcnt, ups.received_at)])
    return ups[ups.published <= T].sort_values(["published", "received_at"], kind="stable")


def lost_bins(ups: pd.DataFrame, buffer_bins: int | None) -> dict[int, set[int]]:
    """Bins que el backend da por perdidos con los uplinks ups (en el orden en que los conoció): la misma regla que
    el ACK de la S3 (AckTracker.lost). Con B, los que ya no caben en el buffer del nodo; sin B, los que salta un
    reenvío (FLAG_RETX). El ACK los confirma, así que el nodo ya no los enviará."""
    tracker = AckTracker(buffer_bins=buffer_bins)
    for u in ups.itertuples() if not ups.empty else []:
        tracker.add(Frame(int(u.strategy), int(u.flags), int(u.epoch), int(u.s1), int(u.n), int(u.age_units),
                          int(u.cumulative), list(u.bins)))
    return tracker.lost


def gap_state(strategy: int, behind: int, k: int, beyond_buffer: bool) -> str:
    """Estado de un bin que no ha llegado, entre el primero y el último recibidos de su época. behind: bins de la
    época recibidos por delante de él (distancia al último); beyond_buffer: lost_bins lo da por perdido."""
    if strategy >= 3:      # S3 y S4: el nodo guarda lo no confirmado y lo reenvía mientras quepa en el buffer
        return "lost" if beyond_buffer else "pending"
    if strategy == 2:      # S2: aún puede llegar en la redundancia de las tramas siguientes
        return "pending" if behind < k else "lost"
    return "lost"          # S1: cada bin se envía una sola vez


def interval_states(exp, T: pd.Timestamp, published_pending: set | None = None, *,
                    published_at: dict | None = None, buffer_bins: int | None = None) -> pd.DataFrame:
    """Estado de cada intervalo conocido por el backend en el instante T (solo con los uplinks publicados hasta T).

    published_pending: (época, secuencia) publicados antes como pendientes; si ya han llegado, salen como 'late'.
    published_at: FCnt -> instante de publicación de las tramas recuperadas más tarde (por defecto, la recepción).
    buffer_bins: capacidad B del buffer del nodo; por defecto, la de la prueba (node_buffer_bins)."""
    rows = []
    copies = received_copies(exp)
    if copies.empty:
        return pd.DataFrame(rows)
    copies["fcnt"] = [_fcnt(f) for f in copies.fcnt]
    copies["published"] = [_published(f, a, published_at) for f, a in zip(copies.fcnt, copies.arrival)]
    copies = copies[copies.published <= T].sort_values(["published", "arrival"], kind="stable")
    if copies.empty:
        return pd.DataFrame(rows)
    strategy = exp.config.get("strategy")
    if strategy is None:  # sin línea EV,config: la de los uplinks (FPort 10 + estrategia)
        strategy = int(exp.uplinks.strategy.iloc[-1])
    k = exp.config.get("k", 4)
    lost = {}
    if strategy >= 3:
        B = node_buffer_bins(exp) if buffer_bins is None else buffer_bins
        lost = lost_bins(known_uplinks(exp, T, published_at), B)
    first = copies.groupby(["epoch", "seq"], as_index=False).first()
    for epoch, grp in first.groupby("epoch"):
        epoch = int(epoch)
        grp = grp.sort_values("seq")
        ref = grp.dropna(subset=["t_hat"]).iloc[0] if grp.t_hat.notna().any() else None
        known = dict(zip(grp.seq, grp.itertuples()))
        s_max = int(grp.seq.max())
        gone = lost.get(epoch, set())
        for s in range(int(grp.seq.min()), s_max + 1):
            t_hat = (ref.t_hat + pd.Timedelta(seconds=(s - ref.seq) * exp.delta_s)) if ref is not None else pd.NaT
            if s in known:
                r = known[s]
                late = published_pending is not None and (epoch, s) in published_pending
                rows.append(dict(epoch=epoch, seq=s, state="late" if late else "final", value=int(r.value),
                                 arrival=r.arrival, issued=r.published, fcnt=r.fcnt,
                                 t_hat=r.t_hat if pd.notna(r.t_hat) else t_hat))
                continue
            # hueco: ¿puede llegar todavía?
            state = gap_state(strategy, s_max - s, k, s in gone)
            rows.append(dict(epoch=epoch, seq=s, state=state, value=None, arrival=pd.NaT, issued=pd.NaT, fcnt=None,
                             t_hat=t_hat))
    return pd.DataFrame(rows)


def observation(exp, row, patient_ref: str, device_ref: str) -> dict:
    start = row.t_hat
    end = start + pd.Timedelta(seconds=exp.delta_s) if pd.notna(start) else pd.NaT
    obs = {
        "resourceType": "Observation",
        "id": _uid(exp.plan.get("id", exp.path.name), row.epoch, row.seq),
        "identifier": [{"system": "urn:hij:bin", "value": f"{exp.plan.get('id', exp.path.name)}/{row.epoch}/{row.seq}"}],
        "status": STATUS[row.state],
        "category": [{"coding": [dict(CATEGORY_ACTIVITY)]}],  # copias: que tocar un recurso no cambie los demás
        "code": {"coding": [dict(LOINC_STEPS)]},
        "subject": {"reference": patient_ref},
        "device": {"reference": device_ref},
    }
    if pd.notna(start):
        obs["effectivePeriod"] = {"start": _iso(start), "end": _iso(end)}
    if row.state in ("final", "late"):
        # int: con huecos en el Bundle la columna value es float y saldría 62.0 (en FHIR, una cifra decimal de más)
        obs["valueQuantity"] = {"value": int(row.value), "unit": "steps", "system": UCUM, "code": "{steps}"}
        issued = getattr(row, "issued", None)
        obs["issued"] = _iso(issued if issued is not None and pd.notna(issued) else row.arrival)  # publicación
    else:
        code = "temp-unknown" if row.state == "pending" else "unknown"
        obs["dataAbsentReason"] = {"coding": [{"system": DAR, "code": code, "display": DAR_DISPLAY[code]}]}
    return obs


def bundle(exp, T: pd.Timestamp, published_pending: set | None = None, *, published_at: dict | None = None,
           buffer_bins: int | None = None) -> tuple[dict, pd.DataFrame]:
    """Bundle de tipo collection con Patient, Device, las Observation y un Provenance por uplink con valores nuevos.

    published_at y buffer_bins: ver interval_states. El Provenance de cada uplink enlaza las Observation cuya
    primera copia publicada trajo; recorded = publicación y la recepción queda en entity.what.display."""
    states = interval_states(exp, T, published_pending, published_at=published_at, buffer_bins=buffer_bins)
    pid = exp.plan.get("id", exp.path.name)
    patient = {"resourceType": "Patient", "id": _uid(pid, "patient"),
               "identifier": [{"system": "urn:hij:pseudonym", "value": "P-001"}]}
    device = {"resourceType": "Device", "id": _uid(pid, "device"),
              "deviceName": [{"name": "hij_node (Heltec WiFi LoRa 32 V3 + BNO085)", "type": "model-name"}],
              "version": [{"value": str(exp.config.get("version", "0.2.0"))}]}
    p_ref, d_ref = f"urn:uuid:{patient['id']}", f"urn:uuid:{device['id']}"  # resolubles dentro del Bundle
    entries = [patient, device]
    obs_by_uplink: dict = {}  # (FCnt, recepción en ns) -> Observation que trajo por primera vez
    for r in states.itertuples():
        o = observation(exp, r, p_ref, d_ref)
        entries.append(o)
        if r.state in ("final", "late"):
            obs_by_uplink.setdefault((_fcnt(r.fcnt), pd.Timestamp(r.arrival).value), []).append(f"urn:uuid:{o['id']}")
    ups = known_uplinks(exp, T, published_at)
    for u in ups.itertuples() if not ups.empty else []:
        fc = _fcnt(u.fcnt)
        targets = obs_by_uplink.get((fc, pd.Timestamp(u.received_at).value))
        if not targets:
            continue
        desc = (f"LoRaWAN uplink FCnt {fc}, FPort {u.fport}, received {_iso(u.received_at)}"
                + (f", RSSI {u.rssi} dBm, SNR {u.snr} dB" if pd.notna(u.rssi) else "")
                + (", retransmission" if u.flags & FLAG_RETX else ""))
        entries.append({
            "resourceType": "Provenance", "id": _uid(pid, "prov", fc, _iso(u.received_at)),
            "target": [{"reference": t} for t in targets],
            "recorded": _iso(u.published),  # publicación de las versiones (= issued)
            "activity": {"coding": [dict(ACTIVITY_CREATE)]},
            "agent": [{"type": {"coding": [dict(AGENT_PERFORMER)]}, "who": {"reference": d_ref}}],
            "entity": [{"role": "source", "what": {"identifier": {"system": "urn:lorawan:uplink",
                                                                  "value": f"{pid}/{fc}"}, "display": desc}}],
        })
    b = {"resourceType": "Bundle", "id": _uid(pid, "bundle", _iso(T)), "type": "collection", "timestamp": _iso(T),
         "entry": [{"fullUrl": f"urn:uuid:{e['id']}", "resource": e} for e in entries]}
    return b, states
