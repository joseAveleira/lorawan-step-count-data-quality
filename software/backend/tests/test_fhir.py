"""Pruebas de la exportación FHIR (hij.fhir) y del resumen clínico (hij.clinical) con experimentos sintéticos.

Las fronteras de la regla de capacidad se comparan con AckTracker en lugar de fijarlas aquí: la exportación debe
usar la misma regla que el ACK de la S3, sea cual sea su frontera exacta.
"""
import json
import pathlib
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hij import fhir  # noqa: E402
from hij.ack import AckTracker  # noqa: E402
from hij.clinical import summarize, weekly_text  # noqa: E402
from hij.experiment import Experiment  # noqa: E402
from hij.payload import FLAG_RETX, Frame  # noqa: E402

T0 = pd.Timestamp("2026-10-18 12:00:00", tz="UTC")
DELTA = 60
EPOCH = 5

# códigos y displays de las definiciones oficiales de FHIR R4 (4.0.1) y de LOINC (tx.fhir.org, versión 2.82)
ALLOWED = {
    ("http://loinc.org", "55423-8"): "Number of steps in unspecified time Pedometer",
    ("http://terminology.hl7.org/CodeSystem/observation-category", "activity"): "Activity",
    ("http://terminology.hl7.org/CodeSystem/data-absent-reason", "temp-unknown"): "Temporarily Unknown",
    ("http://terminology.hl7.org/CodeSystem/data-absent-reason", "unknown"): "Unknown",
    ("http://terminology.hl7.org/CodeSystem/v3-DataOperation", "CREATE"): "create",
    ("http://terminology.hl7.org/CodeSystem/provenance-participant-type", "performer"): "Performer",
}
OBS_STATUS_R4 = {"registered", "preliminary", "final", "amended", "corrected", "cancelled", "entered-in-error",
                 "unknown"}


def received(opened: int) -> pd.Timestamp:
    """La trama sale al abrirse el bin 'opened' y llega 1 s después."""
    return T0 + pd.Timedelta(seconds=opened * DELTA + 1)


def make_exp(frames, strategy=3, setup=None, config_buf=None, k=4, plan=True, fcnts=None):
    """frames: (s1, n, abierto, flags) en orden de llegada, todas de la época EPOCH. La edad del bin s1 es
    abierto - s1 bins, así que t̂(s) = T0 + s·Δ + 1 s. Pasos del bin s: s % 7 (incluye ceros)."""
    rows = []
    for i, (s1, n, opened, flags) in enumerate(frames):
        rows.append(dict(received_at=received(opened), fcnt=(fcnts[i] if fcnts else 100 + i), fport=10 + strategy,
                         strategy=strategy, flags=flags, epoch=EPOCH, s1=s1, n=n, age_units=(opened - s1) * 15,
                         cumulative=0, bins=[s % 7 for s in range(s1, s1 + n)], rssi=-50, snr=10.0, airtime_s=0.1))
    ups = pd.DataFrame(rows)
    ups["received_at"] = pd.to_datetime(ups["received_at"], utc=True)
    config = {"strategy": strategy, "delta": DELTA, "k": k, "period": 240}
    if config_buf is not None:
        config["buf"] = config_buf
    p = {"id": "TEST", "setup": list(setup or [])} if plan else {}
    end = ups.received_at.max() + pd.Timedelta(seconds=30)
    return Experiment(pathlib.Path("TEST"), p, config, DELTA, T0, end, 0.0, pd.DataFrame(), pd.DataFrame(),
                      pd.DataFrame(), ups, pd.DataFrame())


def tracker_lost(frames, buffer_bins, strategy=3):
    t = AckTracker(buffer_bins=buffer_bins)
    for s1, n, opened, flags in frames:
        t.add(Frame(strategy, flags, EPOCH, s1, n, (opened - s1) * 15, 0, [0] * n))
    return t.lost.get(EPOCH, set())


def resources(b, rtype):
    return [e["resource"] for e in b["entry"] if e["resource"]["resourceType"] == rtype]


def obs_by_seq(b):
    return {int(o["identifier"][0]["value"].rsplit("/", 1)[1]): o for o in resources(b, "Observation")}


def codings(b):
    out = []
    for o in resources(b, "Observation"):
        out += o["code"]["coding"] + [c for cat in o["category"] for c in cat["coding"]]
        out += o.get("dataAbsentReason", {}).get("coding", [])
    for p in resources(b, "Provenance"):
        out += p["activity"]["coding"] + [c for a in p["agent"] for c in a["type"]["coding"]]
    return out


# S3 con B = 10: llegan los bins 0..3 y, tras un corte, 26..29 con el bin 30 abierto. El nodo solo conserva los
# B bins cerrados más recientes, así que los más antiguos del hueco 4..25 ya no existen.
OVERFLOW = [(0, 4, 4, 0), (26, 4, 30, 0)]
SETUP_B10 = ["set buf 1440", "set buf 10"]   # vale el último, como en el plan de C5-overflow_S3


def test_provenance_agent_is_performer():
    exp = make_exp([(0, 4, 4, 0), (4, 4, 8, 0)])
    b, _ = fhir.bundle(exp, exp.end)
    provs = resources(b, "Provenance")
    assert len(provs) == 2
    device_url = next(e["fullUrl"] for e in b["entry"] if e["resource"]["resourceType"] == "Device")
    for p in provs:
        assert p["agent"] == [{"type": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
            "code": "performer", "display": "Performer"}]}, "who": {"reference": device_url}}]
        assert all(c["code"] != "device" for c in codings(b))


def test_all_codes_exist_with_their_display():
    # Bundle con observados (también ceros), pendientes y perdidos, para que salgan todos los códigos
    exp = make_exp(OVERFLOW, setup=SETUP_B10)
    b, st = fhir.bundle(exp, exp.end)
    assert {"final", "pending", "lost"} <= set(st.state)
    for c in codings(b):
        assert (c["system"], c["code"]) in ALLOWED, c
        assert c.get("display") == ALLOWED[(c["system"], c["code"])], c
    for o in resources(b, "Observation"):
        assert o["status"] in OBS_STATUS_R4
        assert ("valueQuantity" in o) != ("dataAbsentReason" in o)        # obs-6
        if "valueQuantity" in o:
            assert o["valueQuantity"]["system"] == "http://unitsofmeasure.org"
            assert o["valueQuantity"]["code"] == "{steps}"
    assert any(o.get("valueQuantity", {}).get("value") == 0 for o in resources(b, "Observation"))
    # pasos enteros aunque el Bundle tenga huecos (sin valor): 62, no 62.0
    assert all(type(o["valueQuantity"]["value"]) is int for o in resources(b, "Observation") if "valueQuantity" in o)
    assert '"value": 3.0' not in json.dumps(b) and '"value": 3,' in json.dumps(b)
    urls = [e["fullUrl"] for e in b["entry"]]
    assert len(urls) == len(set(urls))


def test_s3_gap_pending_within_buffer_lost_beyond():
    exp = make_exp(OVERFLOW, setup=SETUP_B10)
    assert fhir.node_buffer_bins(exp) == 10
    st = fhir.interval_states(exp, exp.end)
    state = dict(zip(st.seq, st.state))
    gap = set(range(4, 26))
    assert {s for s in gap if state[s] == "lost"} == tracker_lost(OVERFLOW, 10) & gap  # misma regla que el ACK
    assert {s for s in gap if state[s] != "lost"} == {s for s in gap if state[s] == "pending"}
    assert all(state[s] == "lost" for s in range(4, 19))       # muy por detrás del bin abierto menos B
    assert all(state[s] == "pending" for s in range(21, 26))   # caben en el buffer
    b, _ = fhir.bundle(exp, exp.end)
    obs = obs_by_seq(b)
    for s in gap:
        o = obs[s]
        assert "valueQuantity" not in o and "issued" not in o
        if state[s] == "lost":
            assert o["status"] == "final" and o["dataAbsentReason"]["coding"][0]["code"] == "unknown"
        else:
            assert o["status"] == "registered" and o["dataAbsentReason"]["coding"][0]["code"] == "temp-unknown"
    # con un buffer que no se llena, todo el hueco sigue pendiente
    st = fhir.interval_states(exp, exp.end, buffer_bins=1440)
    assert set(st[st.seq.isin(gap)].state) == {"pending"}


def test_retransmission_turns_pending_into_final_and_keeps_lost():
    first = make_exp(OVERFLOW, setup=SETUP_B10)
    b1, st1 = fhir.bundle(first, first.end)
    pending = {(r.epoch, r.seq) for r in st1.itertuples() if r.state == "pending"}
    frames = OVERFLOW + [(21, 5, 31, FLAG_RETX)]          # el nodo reenvía desde su bin más antiguo
    exp = make_exp(frames, setup=SETUP_B10)
    b2, st2 = fhir.bundle(exp, exp.end, published_pending=pending)
    state = dict(zip(st2.seq, st2.state))
    assert all(state[s] == "late" for s in range(21, 26))
    lost = tracker_lost(frames, 10) & set(range(4, 21))
    assert {s for s in range(4, 21) if state[s] == "lost"} == lost
    assert {s for s in range(4, 21) if state[s] == "pending"} == set(range(4, 21)) - lost
    assert all(state[s] == "lost" for s in range(4, 20))
    obs = obs_by_seq(b2)
    for s in range(21, 26):
        assert obs[s]["status"] == "final" and obs[s]["valueQuantity"]["value"] == s % 7
        assert obs[s]["issued"] == fhir._iso(received(31))
        assert obs[s]["id"] == obs_by_seq(b1)[s]["id"]               # versión nueva del mismo recurso
    prov = next(p for p in resources(b2, "Provenance") if p["entity"][0]["what"]["identifier"]["value"] == "TEST/102")
    assert sorted(t["reference"] for t in prov["target"]) == sorted(f"urn:uuid:{obs[s]['id']}" for s in range(21, 26))
    assert "retransmission" in prov["entity"][0]["what"]["display"]


def test_buffer_size_from_plan_then_config():
    assert fhir.node_buffer_bins(make_exp(OVERFLOW, setup=["set buf 1440", "set buf 96"])) == 96
    assert fhir.node_buffer_bins(make_exp(OVERFLOW, setup=["set strategy 3"], config_buf=200)) == 200
    assert fhir.node_buffer_bins(make_exp(OVERFLOW, plan=False, config_buf=200)) == 200
    assert fhir.node_buffer_bins(make_exp(OVERFLOW, plan=False)) is None


def test_s4_uses_capacity_rule_too():
    exp = make_exp(OVERFLOW, strategy=4, setup=SETUP_B10)
    st = fhir.interval_states(exp, exp.end)
    state = dict(zip(st.seq, st.state))
    assert {s for s in range(4, 26) if state[s] == "lost"} == tracker_lost(OVERFLOW, 10, strategy=4) & set(range(4, 26))
    assert state[25] == "pending"


def test_s1_and_s2_rules_unchanged():
    frames = [(0, 4, 4, 0), (9, 2, 11, 0)]                # hueco 4..8; el último bin recibido es el 10
    st = fhir.interval_states(make_exp(frames, strategy=1, setup=SETUP_B10), received(11))
    assert set(st[st.seq.between(4, 8)].state) == {"lost"}
    st = fhir.interval_states(make_exp(frames, strategy=2, setup=SETUP_B10, k=4), received(11))
    state = dict(zip(st.seq, st.state))
    assert [state[s] for s in range(4, 9)] == ["lost", "lost", "lost", "pending", "pending"]  # k = 4 por detrás


def test_issued_is_reception_by_default():
    exp = make_exp([(0, 4, 4, 0), (4, 4, 8, 0)])
    b, _ = fhir.bundle(exp, exp.end)
    obs = obs_by_seq(b)
    for s in range(8):
        opened = 4 if s < 4 else 8
        assert obs[s]["issued"] == fhir._iso(received(opened))
        assert obs[s]["effectivePeriod"]["start"] == fhir._iso(T0 + pd.Timedelta(seconds=s * DELTA + 1))
    for p in resources(b, "Provenance"):
        fc = int(p["entity"][0]["what"]["identifier"]["value"].rsplit("/", 1)[1])
        t_rx = fhir._iso(received(4 if fc == 100 else 8))
        assert p["recorded"] == t_rx and f"received {t_rx}" in p["entity"][0]["what"]["display"]


def test_published_at_for_frames_recovered_later():
    exp = make_exp([(0, 4, 4, 0), (4, 4, 8, 0)])
    rx = received(8)
    pub = rx + pd.Timedelta(hours=2)                          # el backend la recupera 2 h después
    # antes de publicarse, el backend no la conoce: ni sus bins ni su Provenance
    b, st = fhir.bundle(exp, rx + pd.Timedelta(hours=1), published_at={101: pub})
    assert sorted(st.seq) == [0, 1, 2, 3] and len(resources(b, "Provenance")) == 1
    b, st = fhir.bundle(exp, rx + pd.Timedelta(hours=3), published_at={101: pub})
    obs = obs_by_seq(b)
    assert all(obs[s]["issued"] == fhir._iso(pub) for s in range(4, 8))
    assert all(obs[s]["issued"] == fhir._iso(received(4)) for s in range(4))
    assert obs[4]["effectivePeriod"]["start"] == fhir._iso(T0 + pd.Timedelta(seconds=4 * DELTA + 1))  # con la recepción
    prov = next(p for p in resources(b, "Provenance") if p["entity"][0]["what"]["identifier"]["value"] == "TEST/101")
    assert prov["recorded"] == fhir._iso(pub)
    assert f"received {fhir._iso(rx)}" in prov["entity"][0]["what"]["display"]
    # nunca antes de la recepción (relojes distintos), y una hora sin zona se toma como UTC
    b, _ = fhir.bundle(exp, exp.end, published_at={101: rx - pd.Timedelta(seconds=3)})
    assert obs_by_seq(b)[4]["issued"] == fhir._iso(rx)
    b, _ = fhir.bundle(exp, rx + pd.Timedelta(hours=3), published_at={101: pub.tz_localize(None)})
    assert obs_by_seq(b)[4]["issued"] == fhir._iso(pub)


def test_published_at_first_published_copy_wins():
    # la trama 100 (bins 0..3) se recupera tarde; antes llega y se publica una repetición de 2..5
    frames = [(0, 4, 4, 0), (2, 4, 6, FLAG_RETX)]
    exp = make_exp(frames)
    pub = received(6) + pd.Timedelta(minutes=30)
    b, st = fhir.bundle(exp, pub + pd.Timedelta(minutes=1), published_at={100: pub})
    obs = obs_by_seq(b)
    assert obs[2]["issued"] == fhir._iso(received(6)) and obs[0]["issued"] == fhir._iso(pub)
    targets = {p["entity"][0]["what"]["identifier"]["value"]: len(p["target"]) for p in resources(b, "Provenance")}
    assert targets == {"TEST/101": 4, "TEST/100": 2}


def test_resources_do_not_share_codings():
    exp = make_exp([(0, 4, 4, 0), (4, 4, 8, 0)])
    b, _ = fhir.bundle(exp, exp.end)
    p1, p2 = resources(b, "Provenance")
    p1["agent"][0]["type"]["coding"][0]["code"] = "x"
    o1, o2 = resources(b, "Observation")[:2]
    o1["code"]["coding"][0]["display"] = "x"
    assert p2["agent"][0]["type"]["coding"][0]["code"] == "performer"
    assert o2["code"]["coding"][0]["display"] == fhir.LOINC_STEPS["display"] == ALLOWED[("http://loinc.org", "55423-8")]
    assert fhir.AGENT_PERFORMER["code"] == "performer"


def test_missing_fcnt_is_zero():
    exp = make_exp([(0, 4, 4, 0), (4, 4, 8, 0)], fcnts=[None, 1])
    b, _ = fhir.bundle(exp, exp.end)
    values = sorted(p["entity"][0]["what"]["identifier"]["value"] for p in resources(b, "Provenance"))
    assert values == ["TEST/0", "TEST/1"]


def test_bundle_structure_is_valid():
    Bundle = pytest.importorskip("fhir.resources.bundle").Bundle  # fhir.resources 6.5 valida R4B
    frames = OVERFLOW + [(21, 5, 31, FLAG_RETX)]
    for exp in (make_exp(OVERFLOW, setup=SETUP_B10), make_exp(frames, setup=SETUP_B10)):
        b, _ = fhir.bundle(exp, exp.end, published_at={101: exp.end})
        Bundle.parse_obj(b)


def test_clinical_summary_uses_capacity_rule():
    exp = make_exp(OVERFLOW, setup=SETUP_B10)
    s = summarize(exp)
    state = dict(zip(s.slots.slot, s.slots.state))
    gap = set(range(4, 26))
    assert {i for i in gap if state[i] == "lost"} == tracker_lost(OVERFLOW, 10) & gap
    assert {i for i in gap if state[i] != "lost"} == {i for i in gap if state[i] == "pending"}
    assert all(state[i] == "lost" for i in range(4, 19)) and all(state[i] == "pending" for i in range(21, 26))
    assert all(state[i] == "observed" for i in list(range(4)) + list(range(26, 30)))
    assert s.buffer_bins == 10
    # mismos estados que la exportación FHIR
    st = fhir.interval_states(exp, exp.end)
    assert all(state[r.seq] == {"final": "observed"}.get(r.state, r.state) for r in st.itertuples())
    s = summarize(exp, buffer_bins=1440)
    assert {s.slots.state[i] for i in gap} == {"pending"}


def test_clinical_text_warns_when_outage_exceeds_buffer():
    exp = make_exp(OVERFLOW, setup=SETUP_B10)          # B = 10 bins = 2,5 h a escala real
    later = exp.end + pd.Timedelta(seconds=700)        # 700 s de laboratorio = 2,9 h sin conexión
    txt = weekly_text(summarize(exp, later))
    assert "solo guarda las últimas 2.5 h" in txt
    assert "guarda los datos de ese tiempo" not in txt
    txt = weekly_text(summarize(exp, later, buffer_bins=1440))
    assert "guarda los datos de ese tiempo y los enviará" in txt
