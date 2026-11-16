"""Tabla comparativa de métricas de calidad de varios experimentos.

Uso:
    python backend/report.py piloto_s0_c4 piloto_s2_c4 piloto_s3_c4      (ids en software/data/raw/)
    python backend/report.py --all                                       (todos los experimentos con DONE)

Guarda software/data/processed/metrics.csv y, por experimento, <id>_bins.csv y <id>_frames.csv.
"""
import argparse
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from hij.experiment import load_experiment  # noqa: E402
from hij.quality import bin_table, frame_fate, metrics  # noqa: E402

SOFTWARE = pathlib.Path(__file__).resolve().parents[1]
RAW, PROCESSED = SOFTWARE / "data" / "raw", SOFTWARE / "data" / "processed"

SHOW = ["id", "strategy", "scenario", "bins_generated", "DC_1h", "DC_24h", "DC_end", "RR", "SR_bins",
        "SR_cumulative", "TI", "DR", "OOR", "delay_med_h_real", "delay_p95_h_real", "FZR_naive", "FZR_aware",
        "airtime_s_per_day", "frames"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    ids = args.ids or sorted(p.parent.name for p in RAW.glob("*/DONE"))
    PROCESSED.mkdir(parents=True, exist_ok=True)
    rows = []
    for pid in ids:
        exp = load_experiment(RAW / pid)
        m = metrics(exp)
        rows.append(m)
        bin_table(exp).to_csv(PROCESSED / f"{pid}_bins.csv", index=False)
        frame_fate(exp).to_csv(PROCESSED / f"{pid}_frames.csv", index=False)
    df = pd.DataFrame(rows)
    df.to_csv(PROCESSED / "metrics.csv", index=False)
    cols = [c for c in SHOW if c in df.columns]
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print(df[cols].round(3).to_string(index=False))
    fates = [c for c in df.columns if c.startswith("frames_")]
    if fates:
        print("\nDestino de las tramas:")
        print(df[["id", *fates]].fillna(0).to_string(index=False))


if __name__ == "__main__":
    main()
