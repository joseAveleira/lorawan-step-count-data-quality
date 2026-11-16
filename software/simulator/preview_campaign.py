"""Simula cada plan de la campaña con los mismos parámetros que el nodo y muestra las métricas esperadas.

Uso:
    python simulator/preview_campaign.py [--ack-same-window]
"""
import argparse
import json
import pathlib
import sys

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "backend"))
from hijsim import SimConfig, simulate  # noqa: E402
from hij.quality import metrics  # noqa: E402

PLANS = HERE.parent / "experiments" / "plans" / "campaign"
KEYS = {"strategy": "strategy", "scenario": "scenario", "period": "period_s", "k": "k", "n": "n_ack", "buf": "buf",
        "seed": "seed", "delta": "delta_s", "dr": "dr", "cycle": "cycle", "window": "window", "geloss": "ge_loss",
        "geburst": "ge_burst", "outstart": "out_start", "outlen": "out_len"}


def plan_to_config(plan: dict, ack_same_window: bool) -> SimConfig:
    kw, fup = {}, 1
    for cmd in plan["setup"]:   # vale el último 'set' de cada parámetro, como en el nodo
        parts = cmd.split()
        if len(parts) == 3 and parts[0] == "set":
            if parts[1] in KEYS:
                kw[KEYS[parts[1]]] = int(parts[2])
            elif parts[1] == "fup":
                fup = int(parts[2])
    if fup == 0:
        kw["fup_ms_per_hour"] = 0   # sin el límite de TTN queda el duty cycle legal de la banda (link.cpp)
    reboots = tuple(a["at"] for a in plan.get("actions", []) if a.get("node") == "reboot")
    eval_s = plan.get("eval_s") if plan.get("eval_s") != plan["duration_s"] else None
    # cortes reales: entre block y unblock el gateway descarta las tramas del nodo, que el nodo sí transmite
    # (estado sent, con su airtime y su paso por el límite de airtime) y que el backend no recibe; el escenario
    # del nodo es el del plan (C1)
    blocks = sorted(a["at"] for a in plan.get("actions", []) if a.get("gw") == "block")
    unblocks = sorted(a["at"] for a in plan.get("actions", []) if a.get("gw") == "unblock")
    gw_block = tuple((a, next((b for b in unblocks if b > a), plan["duration_s"])) for a in blocks)
    return SimConfig(duration_s=plan["duration_s"], reboots_at=reboots, ack_same_window=ack_same_window,
                     label=plan["id"], eval_s=eval_s, gw_block=gw_block, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ack-same-window", action="store_true")
    args = ap.parse_args()
    order = [l.strip() for l in (PLANS / "order.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = []
    for pid in order:
        plan = json.loads((PLANS / f"{pid}.json").read_text(encoding="utf-8"))
        exp = simulate(plan_to_config(plan, args.ack_same_window))
        m = metrics(exp, end_margin_s=270)
        m["id"], m["tier"] = pid, plan.get("tier", 1)
        m["downlinks"] = int(exp.ev.type.isin(["ack", "confirm"]).sum())
        rows.append(m)
    df = pd.DataFrame(rows)
    cols = ["id", "tier", "bins_generated", "DC_1h", "DC_24h", "DC_end", "RR", "SR_bins", "SR_counter", "TI", "DR",
            "OOR", "delay_med_h_real", "delay_p95_h_real", "FZR_naive", "airtime_s_per_day", "frames_node_dropped",
            "downlinks"]
    cols = [c for c in cols if c in df.columns]
    pd.set_option("display.width", 250)
    print(df[cols].round(2).to_string(index=False))


if __name__ == "__main__":
    main()
