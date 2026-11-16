"""Lectura del registro del nodo (node.log): una línea por mensaje, "<instante UTC del PC>\t<línea del nodo>".

    GT,<e>,<s>,<t_inicio_ms>,<pasos>,<acumulado>,<k>
    TX,<e>,<fcnt>,<s1>,<n>,<edad>,<dr>,<bytes>,<estado>,<toa_ms>,<t_ms>
    EV,<tipo>,...
(k = intervalo desde el inicio del experimento; los registros anteriores a v0.1.1 no lo traen.)
"""
import pathlib

import pandas as pd

GT_COLS = ["epoch", "seq", "t_start_ms", "steps", "cumulative", "k"]
TX_COLS = ["epoch", "fcnt", "s1", "n", "age_units", "dr", "bytes", "status", "toa_ms", "t_ms"]


def read_node_log(path: pathlib.Path) -> dict[str, pd.DataFrame]:
    gt, tx, ev = [], [], []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            host, _, line = raw.rstrip("\n").partition("\t")
            if line.startswith("GT,"):
                parts = line.split(",")[1:]
                if len(parts) == len(GT_COLS) - 1:
                    parts.append("-1")  # formato anterior, sin k
                if len(parts) == len(GT_COLS):
                    gt.append([host, *map(int, parts)])
            elif line.startswith("TX,"):
                parts = line.split(",")[1:]
                if len(parts) == len(TX_COLS):
                    vals = [int(p) if i != 7 else p for i, p in enumerate(parts)]
                    tx.append([host, *vals])
            elif line.startswith("EV,"):
                kind, _, rest = line[3:].partition(",")
                ev.append([host, kind, rest])
    out = {
        "gt": pd.DataFrame(gt, columns=["host_time", *GT_COLS]),
        "tx": pd.DataFrame(tx, columns=["host_time", *TX_COLS]),
        "ev": pd.DataFrame(ev, columns=["host_time", "type", "detail"]),
    }
    for df in out.values():
        df["host_time"] = pd.to_datetime(df["host_time"], utc=True, format="ISO8601")
    return out
