"""Pruebas del simulador (python -m pytest simulator): corte real, lectura del plan, edad de las tramas, reconstrucción
de validate_lab y airtime emitido de sim30. No leen data/raw ni escriben en data/processed."""
import json
import pathlib
import sys

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "backend")]
from hij.quality import metrics  # noqa: E402
from hijsim import simulate  # noqa: E402
from preview_campaign import PLANS, plan_to_config  # noqa: E402
import sim30  # noqa: E402
import validate_lab  # noqa: E402


def plan(pid: str) -> dict:
    return json.loads((PLANS / f"{pid}.json").read_text(encoding="utf-8"))


def test_real_outage_frames_transmitted_not_received():
    cfg = plan_to_config(plan("REAL-C4-24_S3"), ack_same_window=True)
    assert cfg.scenario == 1 and cfg.gw_block == ((1800, 7560),)
    real = simulate(cfg)
    emul = simulate(plan_to_config(plan("C4-24_S3"), ack_same_window=True))
    tx = real.tx
    inside = (tx.t_ms >= 1800_000) & (tx.t_ms < 7560_000)
    assert len(tx[inside]) == 24 and (tx[inside].status == "sent").all() and (tx[inside].toa_ms > 0).all()
    t0 = real.exp_start
    rx = (real.uplinks.received_at - t0).dt.total_seconds()
    assert not ((rx >= 1800) & (rx < 7560)).any()
    # mismos datos que el corte emulado (salvo el contador de tramas) y más airtime
    cols = ["received_at", "s1", "n", "age_units", "flags", "cumulative"]
    assert real.uplinks[cols].astype(str).equals(emul.uplinks[cols].astype(str))
    mr, me = metrics(real), metrics(emul)
    for k in ["DC_24h", "DC_end", "RR", "SR_bins", "DR", "OOR", "delay_med_h_real", "FZR_naive"]:
        assert mr[k] == me[k], k
    assert mr["airtime_s_per_day"] > 1.7 * me["airtime_s_per_day"]
    assert mr["frames_gateway_down_or_filtered"] == 24 and "frames_emulated_outage" not in mr


def test_plan_to_config_reads_fup_and_last_setting():
    p = plan("C1_S1")
    assert plan_to_config(p, True).fup_ms_per_hour == 1250
    p = dict(p, setup=p["setup"] + ["set fup 0", "set buf 96", "set buf 100"])
    cfg = plan_to_config(p, True)
    assert cfg.fup_ms_per_hour == 0 and cfg.buf == 100 and cfg.gw_block == ()


def test_age_in_integer_ms_like_the_firmware():
    # con este adelanto, el floor en coma flotante daba 59 unidades en una trama en vez de 60
    cfg = plan_to_config(plan("C1_S1"), True)
    cfg.tx_lead_s = 0.48158796500047174
    assert set(simulate(cfg).tx.age_units) == {60}


def _as_real(sim, tmp: pathlib.Path, dump_s: float, obs_s: float):
    """Simulación tratada como prueba real: línea EV,exp,start, fin del bucle de run.py y último registro."""
    sim.ev = pd.concat([pd.DataFrame([dict(host_time=sim.exp_start, type="exp", detail="start,0")]), sim.ev],
                       ignore_index=True)
    sim.end = sim.exp_start + pd.Timedelta(seconds=obs_s)
    tmp.mkdir()
    (tmp / "node.log").write_text(f"{(sim.exp_start + pd.Timedelta(seconds=dump_s)).isoformat()}\t# cmd dump\n",
                                  encoding="utf-8")
    sim.path = tmp
    return sim


def test_validate_lab_rebuilds_reboots_outages_and_early_end(tmp_path):
    for pid in ["C2_S2", "C5-reboot_S3", "REAL-C4-24_S1"]:
        p = plan(pid)
        cfg = plan_to_config(p, True)
        cfg.tx_lead_s = 0.2731
        end = p["duration_s"] - 2.7 if pid == "C2_S2" else p["duration_s"]   # salto del reloj del PC
        cfg.duration_s = end
        cfg.reboots_at = tuple(r + 1.234 for r in cfg.reboots_at)          # el nodo tarda en rearrancar
        real = _as_real(simulate(cfg), tmp_path / pid, end, end + 1.1)
        sched = validate_lab.measured_schedule(real, cfg.period_s)
        assert abs(sched["tx_lead_s"] - 0.2731) < 0.002
        assert sched["reboots_at"] == cfg.reboots_at and sched["gw_block"] == cfg.gw_block
        assert abs(sched["end_tx_s"] - end) < 1e-3
        again = plan_to_config(p, True)
        again.tx_lead_s, again.reboots_at, again.gw_block = sched["tx_lead_s"], sched["reboots_at"], sched["gw_block"]
        again.duration_s = min(again.duration_s, sched["end_tx_s"])
        sim = simulate(again)
        cols = ["s1", "n", "bytes", "status", "toa_ms"]
        assert real.tx[cols].reset_index(drop=True).equals(sim.tx[cols].reset_index(drop=True)), pid


def test_sim30_emitted_airtime():
    row = sim30.run(("weekly", 0, 1, sim30.patient(0), 1440))
    # S1: una trama de 4 bins (19 B, 71 ms a DR5) cada hora durante 33 días, con o sin cobertura
    assert np.isclose(row["airtime_tx_s_per_day"], 24 * 0.071) and np.isclose(row["airtime_tx_max_day_s"], 24 * 0.071)
    assert row["tx_limiter_conflicts"] == 0
    assert row["airtime_s_per_day"] < 0.2 * row["airtime_tx_s_per_day"]   # solo llega lo enviado en las visitas
    s0 = sim30.run(("daily", 0, 0, sim30.patient(0), 1440))
    assert np.isclose(s0["FZR_naive_followup"], s0["FZR_naive"]) and s0["FZR_aware"] == 0
