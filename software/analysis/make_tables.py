r"""Tabla de resultados del laboratorio y cifras para el texto del artículo.

Genera:
    HIJ_2026_extension/tables/lab_results.tex        tabla de las pruebas con hardware (\label{tab:lab})
    software/data/processed/article_numbers.json    cifras del laboratorio, de la simulación de 30 días y de la
    software/data/processed/article_numbers.md      validación del simulador (el .md es la versión para leer)

Uso:
    python software/analysis/make_tables.py

Solo lee las carpetas de software/data/raw que tienen fichero DONE y no escribe nada en ellas. Se puede repetir a
medida que avanza la campaña: la tabla recoge las pruebas terminadas en ese momento. En main.tex la tabla se
incluye con \input{tables/lab_results}; solo usa paquetes que ya cargan sagej.cls y main.tex (booktabs).

Valores de la tabla (hij.quality.metrics, con la verdad terreno en el reloj del gateway): DC_24h, DC_end, RR,
SR_bins y SR_counter (dos columnas de SR, para todas las estrategias), DR, OOR, delay_med_h_real,
delay_p95_h_real, FZR_naive, FZR_aware (en la S0, con cada trama sobre los bins que cubre) y airtime_s_per_day
(tramas transmitidas, por día de reloj de la prueba). La tabla solo lleva las estrategias de Methods (S0 a S3);
las demás pruebas (S4-valid) van solo a las cifras. Downlinks = los que recibe el nodo desde el inicio del
experimento: líneas EV,ack (ACK de la S3) más los ACK de red de los uplinks confirmados de la S4 (EV,confirm con
acked = 1). Se contrastan con TTN (down/sent en el puerto de los ACK y uplinks confirmados recibidos) y se avisa si
no coinciden. Las cifras incluyen además el error de la hora reconstruida |t̂ - t| (mediana, P95 y máximo) de cada
prueba y agregado sobre las pruebas de la tabla y sobre todas, y la vista de la S0 con el incremento en el último
bin (FZR_naive_lastbin, la del panel (b) de fig:timeline).
"""
import datetime as dt
import json
import math
import pathlib
import re
import sys
import warnings

import numpy as np
import pandas as pd

SOFTWARE = pathlib.Path(__file__).resolve().parents[1]
ROOT = SOFTWARE.parent
RAW = SOFTWARE / "data" / "raw"
PROCESSED = SOFTWARE / "data" / "processed"
TABLE_TEX = ROOT / "HIJ_2026_extension" / "tables" / "lab_results.tex"
NUMBERS_JSON = PROCESSED / "article_numbers.json"
NUMBERS_MD = PROCESSED / "article_numbers.md"
SIM_RUNS = PROCESSED / "sim30_runs.csv"
SIM_RUNS_B2880 = PROCESSED / "sim30_runs_B2880.csv"   # S3 con B = 2880 (opcional)
SIM_VALIDATION = PROCESSED / "sim_validation.csv"

sys.path.insert(0, str(SOFTWARE / "backend"))
from hij.experiment import load_experiment  # noqa: E402
from hij.payload import FPORT_ACK  # noqa: E402
from hij.quality import frame_fate, metrics, time_errors  # noqa: E402

# Grupos de la tabla, en el orden del artículo: id del escenario en la campaña -> etiqueta en la tabla.
# El corte real va justo después del corte emulado de 24 h, con el que se compara.
SCENARIOS = {
    "C1": "C1",
    "C2": "C2",
    "C3-10": r"C3 (10\%)",
    "C3-30": r"C3 (30\%)",
    "C4-24": "C4 (24~h)",
    "REAL-C4-24": "C4 (24~h, real)",
    "C4-48": "C4 (48~h)",
    "C4-72": "C4 (72~h)",
    "C5-reboot": "C5 (reboot)",
    "C5-overflow": "C5 (overflow)",
    "S4-valid": "C4 (3~h)",   # corte de 3 tramas (3 h reales) con uplinks confirmados (S4): fuera de la tabla
}
# Estrategias definidas en Methods (tab:strategies). Las demás pruebas solo van a article_numbers.
TABLE_STRATEGIES = {0, 1, 2, 3}

# Columnas numéricas de la tabla: (clave, decimales, cabecera de grupo, cabecera de columna).
# Retrasos con 2 decimales: con 1, valores como 0,7498 h y 0,7507 h saldrían 0,7 y 0,8 y parecerían distintos.
# SR en dos columnas para todas las estrategias: por bins (definición de tab:metrics) y por el contador acumulado.
COLUMNS = [
    ("DC_24h", 2, "DC", "24~h"),
    ("DC_end", 2, "DC", "End"),
    ("RR", 2, None, "RR"),
    ("SR_bins", 2, "SR", "Bins"),
    ("SR_counter", 2, "SR", "Counter"),
    ("DR", 2, None, "DUP"),
    ("OOR", 2, None, "OOR"),
    ("delay_med_h_real", 2, "Delay (h)", "Median"),
    ("delay_p95_h_real", 2, "Delay (h)", "P95"),
    ("FZR_naive", 2, "FZR", "Naive"),
    ("FZR_aware", 2, "FZR", "Aware"),
    ("airtime_s_per_day", 1, "Airtime", "(s/day)"),
    ("downlinks", 0, None, "DL"),
]
NA = "n/a"
MARK_S0 = r"\textsuperscript{a}"   # en la etiqueta S0; la nota explica sus DC, RR, SR y FZR

# Simulación de 30 días: métricas que se resumen (mediana y cuartiles), en el orden de las tablas del .md.
# SR_counter: en sim30_runs.csv puede venir con su nombre anterior, SR_cumulative (alias en hij.quality).
# airtime_tx_*: airtime emitido por el nodo, también sin cobertura (opcional: solo si sim30.py lo guarda).
SIM_COMPLETENESS = ["DC_1h", "DC_24h", "DC_followup", "DC_end", "RR", "SR_bins", "SR_counter"]
SIM_DELAY_COST = ["delay_med_h_real", "delay_p95_h_real", "delay_max_h_real", "FZR_naive", "FZR_aware",
                  "airtime_s_per_day", "airtime_tx_s_per_day", "airtime_tx_max_day_s", "downlinks_per_day"]
SIM_OPTIONAL = {"airtime_tx_s_per_day", "airtime_tx_max_day_s"}
SIM_METRICS = SIM_COMPLETENESS + SIM_DELAY_COST
SIM_HEADS = {
    "DC_1h": "DC 1 h", "DC_24h": "DC 24 h", "DC_followup": "DC fin del seguimiento (día 30)",
    "DC_end": "DC tras la devolución (día 33)", "RR": "RR", "SR_bins": "SR (bins)", "SR_counter": "SR (contador)",
    "delay_med_h_real": "Retraso mediana (h)", "delay_p95_h_real": "Retraso P95 (h)",
    "delay_max_h_real": "Retraso máximo (h)", "FZR_naive": "FZR ingenua", "FZR_aware": "FZR con calidad",
    "airtime_s_per_day": "Airtime recibido (s/día)", "airtime_tx_s_per_day": "Airtime transmitido (s/día)",
    "airtime_tx_max_day_s": "Airtime transmitido, máximo en un día (s)", "downlinks_per_day": "Downlinks/día",
}
SIM_DAYS = 33   # 30 días de seguimiento y 3 de cobertura continua (devolución del dispositivo), como sim30.py
SIM_NOTES = (
    "DC_followup: share of the bins of the 30 days received by day 30 (end of follow-up). DC_end: share received "
    "by day 33, after 3 further days of continuous coverage that represent the return of the device. DC_1h and "
    "DC_24h: share received within 1 h and 24 h of the end of the bin. All metrics refer to the bins generated "
    "during the 30 days; delays in hours at deployment scale. SR_counter (SR_cumulative in older files): share of "
    "the steps included in the cumulative count of the last received frame of each boot. FZR of S0: each frame "
    "shown over the bins it covers. airtime_s_per_day (frames received by the network), airtime_tx_s_per_day "
    "(frames transmitted by the node, also without coverage, when available) and downlinks_per_day are means "
    f"over the {SIM_DAYS} simulated days (30 of follow-up plus 3 of return), not the values of a single day.")
LAB_NOTES = (
    "Times (delays, DC horizon, outage lengths) are laboratory times multiplied by the time compression (15). "
    "airtime_s_per_day: airtime of the transmitted frames per wall-clock day of the run, about 15 times the "
    "deployment value, to compare with the 30-s daily limit (frames emulated without coverage or lost were not "
    "transmitted). SR_bins: steps of the received bins over the steps of the evaluable bins (G), the definition of "
    "tab:metrics. SR_counter: share of the steps of G included in the cumulative count of the last received frame "
    "of each boot (each frame carries the counter of the boot in which it was sent, which restarts at zero); "
    "SR_cumulative is an alias with the former name. counter_gap_max_h_real: longest interval between two "
    "consecutive received frames of one boot, at deployment scale (the counter gives totals over such intervals, "
    "not per bin). FZR is counted per bin. S0 sends no bins, so its DC, RR and SR_bins are zero by construction; "
    "its FZR_naive and FZR_aware show each frame over the bins it covers, FZR_naive_lastbin shows the steps of "
    "each frame in its last bin (presentation of the original system, panel (b) of fig:timeline), SR_increments "
    "is the share of the steps of G covered by a received S0 frame and S0_interval_coverage the share of G "
    "covered. time_err_med_s, time_err_p95_s and time_err_max_s: median, 95th percentile and maximum of the "
    "absolute difference between the reconstructed and the true start of each received bin of G, in wall-clock "
    "seconds; the ground truth is on the gateway clock (clock_ref = gateway: node clock fitted to the gateway "
    "timestamps of the received frames) or, as a fallback, on the PC clock (clock_ref = pc). time_error: the same "
    "statistics pooled over the bins of the table runs and of all runs. downlinks: received by the node after the "
    "start of the run (EV,ack lines plus acknowledged confirmed uplinks of S4); downlinks_ttn: cross-check in TTN "
    "(down/sent on the acknowledgement port plus confirmed uplinks received), null when it cannot be determined. "
    "The table includes strategies S0 to S3 only (Methods); other runs are in excluded_runs.")


# ----------------------------------------------------------------------------------------------- utilidades
def clean(v):
    """Valor apto para JSON: tipos de Python y None en lugar de NaN."""
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    return v


def num(v, decimals: int) -> str | None:
    """Número con los decimales pedidos, o None si no está definido."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    s = f"{v:.{decimals}f}"
    return s[1:] if s.startswith("-") and float(s) == 0 else s


def plan_setting(plan: dict, key: str) -> int | None:
    """Último valor de 'set <key> N' en el setup del plan."""
    val = None
    for cmd in plan.get("setup", []):
        parts = cmd.split()
        if len(parts) == 3 and parts[0] == "set" and parts[1] == key and parts[2].lstrip("-").isdigit():
            val = int(parts[2])
    return val


def parse_run_id(run_id: str) -> tuple[str | None, int | None]:
    """(escenario, estrategia) a partir del id de la prueba; S4-valid lleva la estrategia en la configuración."""
    if run_id in SCENARIOS:
        return run_id, None
    m = re.fullmatch(r"(.+)_S(\d)", run_id)
    if m and m.group(1) in SCENARIOS:
        return m.group(1), int(m.group(2))
    return None, None


def host_start(exp) -> pd.Timestamp:
    """Inicio de la prueba en el reloj del PC, para compararlo con host_time (exp_start va en el del gateway)."""
    return exp.host_start if exp.host_start is not None else exp.exp_start


def count_downlinks(exp) -> int:
    """Downlinks recibidos por el nodo desde el inicio del experimento (no cuenta los de la preparación)."""
    ev = exp.ev[exp.ev.host_time >= host_start(exp)]
    acks = int((ev.type == "ack").sum())                                   # ACK de aplicación (S3, FPort 20)
    confirmed = ev.loc[ev.type == "confirm", "detail"].str.split(",").str[-1]
    return acks + int((confirmed == "1").sum())                            # ACK de red de la S4


def count_downlinks_ttn(exp) -> int | None:
    """Contraste en TTN desde el inicio: downlinks enviados en el puerto de los ACK (S3) más uplinks confirmados
    recibidos (S4: TTN contesta cada uno con un ACK de red). None si ttn.jsonl no permite saberlo."""
    path = exp.path / "ttn.jsonl"
    if not path.exists():
        return None
    sent = confirmed = 0
    flag_seen = False
    start = host_start(exp)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if pd.Timestamp(rec.get("host_time")) < start:
            continue
        topic, msg = rec.get("topic", ""), rec.get("msg", {})
        if topic.endswith("/down/sent") and msg.get("downlink_sent", {}).get("f_port") == FPORT_ACK:
            sent += 1
        elif topic.endswith("/up"):
            up = msg.get("uplink_message", {})
            flag_seen |= "confirmed" in up                                 # TTN omite el campo si es falso
            confirmed += bool(up.get("confirmed"))
    if exp.config.get("strategy") == 4 and not flag_seen:
        return None                                                        # sin el campo no se puede contrastar
    return sent + confirmed


def outage_h(r: dict) -> float | None:
    """Duración del corte emulado a escala de despliegue (h), según el plan."""
    return r["outage_len_s_lab"] * r["compression"] / 3600 if r.get("outage_len_s_lab") else None


# ------------------------------------------------------------------------------------------------ laboratorio
def lab_runs() -> tuple[list[dict], list[dict]]:
    """Pruebas con DONE: (las de la tabla, las demás). En la tabla solo entran las estrategias de Methods."""
    runs = []
    for done in sorted(RAW.glob("*/DONE")):
        run_id = done.parent.name
        scenario, strategy = parse_run_id(run_id)
        if scenario is None:
            print(f"  aviso: {run_id} no es una prueba de la campaña; no entra en la tabla")
            continue
        try:
            with warnings.catch_warnings():   # el aviso del reloj se escribe abajo, con el formato del resto
                warnings.simplefilter("ignore")
                exp = load_experiment(done.parent)
            m = metrics(exp)
            errs = time_errors(exp)
            dl_node, dl_ttn = count_downlinks(exp), count_downlinks_ttn(exp)
            fates = frame_fate(exp)["fate"].value_counts().to_dict() if not exp.tx.empty else {}
            ups = exp.uplinks
        except Exception as e:  # una prueba ilegible no debe impedir la tabla del resto
            print(f"  aviso: {run_id} no se pudo procesar ({type(e).__name__}: {e}); no entra en la tabla")
            continue
        if exp.clock_ref != "gateway":
            print(f"  aviso: {run_id}: verdad terreno con el reloj del PC ({exp.clock_note or 'sin ajuste'})")
        elif exp.clock_note:
            print(f"  aviso: {run_id}: reloj del gateway: {exp.clock_note}")
        cfg_strategy = m.get("strategy")
        if strategy is None:
            if cfg_strategy is None:
                print(f"  aviso: {run_id}: sin estrategia en el id ni en la configuración; no entra en la tabla")
                continue
            strategy = int(cfg_strategy)
        elif cfg_strategy is not None and int(cfg_strategy) != strategy:
            print(f"  aviso: {run_id}: la configuración del nodo dice S{cfg_strategy}; se usa S{strategy}")
        if dl_ttn is not None and dl_ttn != dl_node:
            print(f"  aviso: {run_id}: el nodo recibió {dl_node} downlinks y TTN registra {dl_ttn}")
        m.update(id=run_id, scenario_id=scenario, strategy=strategy, downlinks=dl_node, downlinks_ttn=dl_ttn,
                 buffer_bins=plan_setting(exp.plan, "buf"), outage_len_s_lab=plan_setting(exp.plan, "outlen"))
        m["_time_err_s"] = errs   # |t̂ - t| de cada bin recibido de G, para agregar (no va al JSON)
        m["_fates"] = {str(k): int(v) for k, v in fates.items()}   # destino de cada trama (quality.frame_fate)
        m["_rssi"] = ups["rssi"].dropna().tolist() if not ups.empty and "rssi" in ups else []
        m["_snr"] = ups["snr"].dropna().tolist() if not ups.empty and "snr" in ups else []
        runs.append(m)
    order = list(SCENARIOS)
    runs.sort(key=lambda r: (order.index(r["scenario_id"]), r["strategy"]))
    table = [r for r in runs if r["strategy"] in TABLE_STRATEGIES]
    other = [r for r in runs if r["strategy"] not in TABLE_STRATEGIES]
    for r in other:
        print(f"  {r['id']}: S{r['strategy']} no es una estrategia de Methods (S0 a S3); va solo a las cifras")
    return table, other


def column_cells(values: list, decimals: int) -> list[str]:
    """Celdas de una columna centrada con la coma decimal alineada (relleno invisible de dígitos)."""
    texts = [num(v, decimals) for v in values]
    width = max((len(t.split(".")[0]) for t in texts if t is not None), default=1)
    cells = []
    for t in texts:
        if t is None:
            cells.append(NA)
            continue
        pad = width - len(t.split(".")[0])
        cells.append((r"\phantom{" + "0" * pad + "}" if pad else "") + t)
    return cells


def table_note(runs: list[dict]) -> str:
    """Nota breve: solo lo que hace falta para leer la tabla. Las métricas se definen en tab:metrics (DC a 24 h y al
    final, SR de bins y del contador, FZR de S0 sobre los bins que cubre cada trama) y los escenarios en
    tab:scenarios; la vista del último bin de la S0 la da el texto con fig:timeline."""
    present = {r["scenario_id"] for r in runs}
    strategies = {r["strategy"] for r in runs}
    comps = {round(r["compression"], 6) for r in runs}
    # "~" tras los dos puntos: la abreviatura no se separa de su definición al final de una línea
    parts = [r"Metrics as in Table~\ref{tab:metrics} (FZR of the naive and quality-aware views)",
             "P95:~95th percentile", "DL:~downlinks"]
    if "REAL-C4-24" in present:
        parts.append("real:~frames discarded at the gateway")
    parts.append(f"{NA}:~not defined")
    note = "; ".join(parts) + ". "
    # DC(24 h) se calcula sobre un subconjunto de bins (tab:metrics): sin esta frase parece un error que supere a DC final
    note += "DC(24~h) excludes bins that ended in the last 24~h, so it can exceed DC at the end. "
    if len(comps) == 1:
        c = f"{next(iter(comps)):g}"
        note += (f"Times at deployment scale (laboratory times multiplied by {c}); airtime per wall-clock day of "
                 f"the run, about {c} times the deployment value. ")
    else:
        note += ("Times at deployment scale (laboratory times multiplied by the time compression of each run); "
                 "airtime per wall-clock day of the run. ")
    if 3 in strategies and max(comps) > 1:
        # comprobado con el simulador a escala de despliegue (24/10): cambian las medianas de C3 30 %, C4 y C5 y
        # DC(24 h) de C4 48 y 72 h; DC final, P95 y máximo no cambian
        note += ("In S3, frames deferred by the airtime limit, an effect of the time compression, lengthen median "
                 "delays and lower DC(24~h) after long outages. ")
    if 0 in strategies:
        note += MARK_S0 + "S0 sends no bins: DC, RR and SR from bins are zero by construction."
    return note.strip()


def write_table(runs: list[dict]) -> str:
    cols = {key: column_cells([r.get(key) for r in runs], dec) for key, dec, _, _ in COLUMNS}
    body, prev = [], None
    for i, r in enumerate(runs):
        cells = [cols[key][i] for key, *_ in COLUMNS]
        if prev is not None and r["scenario_id"] != prev:
            body.append(r"\addlinespace")
        label = SCENARIOS[r["scenario_id"]] if r["scenario_id"] != prev else ""
        strategy = f"S{r['strategy']}" + (MARK_S0 if r["strategy"] == 0 else "")
        body.append(" & ".join([label, strategy, *cells]) + r" \\")
        prev = r["scenario_id"]

    # cabecera de dos filas: grupos con \cmidrule y, debajo, el nombre de cada columna
    top, rules, j = ["", ""], [], 0
    while j < len(COLUMNS):
        group = COLUMNS[j][2]
        span = 1
        while group and j + span < len(COLUMNS) and COLUMNS[j + span][2] == group:
            span += 1
        if group and span > 1:
            top.append(rf"\multicolumn{{{span}}}{{c}}{{{group}}}")
            rules.append(rf"\cmidrule(lr){{{j + 3}-{j + span + 2}}}")
        else:
            top.append(group or "")
        j += span
    sub = ["Scenario", "Strategy", *[c[3] for c in COLUMNS]]
    spec = "@{\\extracolsep{\\fill}}ll" + "c" * len(COLUMNS) + "@{}"
    ids = ", ".join(r["id"] for r in runs)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    caption = "Data-quality metrics of the hardware runs by connectivity scenario and strategy."
    tex = "\n".join([
        "% Generated by software/analysis/make_tables.py; do not edit by hand.",
        f"% {stamp}; {len(runs)} runs with a DONE file: {ids}.",
        r"\begin{table}[t]",
        r"\footnotesize",
        rf"\caption{{{caption}}}",
        r"\label{tab:lab}",
        r"\setlength{\tabcolsep}{2pt}",   # con \extracolsep{\fill} el aspecto no cambia; solo da más margen
        rf"\begin{{tabular*}}{{\columnwidth}}{{{spec}}}",
        r"\toprule",
        " & ".join(top) + r" \\",
        "".join(rules),
        " & ".join(sub) + r" \\",
        r"\midrule",
        *body,
        r"\bottomrule",
        r"\end{tabular*}",
        r"\par\smallskip",
        r"\noindent{\footnotesize\raggedright " + table_note(runs) + r"\par}",
        r"\end{table}",
        "",
    ])
    bad = [ch for ch in set(tex) if ord(ch) < 32 and ch != "\n"]
    if bad or chr(0x2013) in tex or chr(0x2014) in tex or " - " in tex:   # tabuladores, semirraya, raya, guion
        raise ValueError("la tabla contiene tabuladores, caracteres de control, rayas o guiones sueltos")
    TABLE_TEX.parent.mkdir(parents=True, exist_ok=True)
    TABLE_TEX.write_text(tex, encoding="utf-8", newline="\n")
    return caption


# ------------------------------------------------------------------------------------- simulación de 30 días
def sim30_numbers() -> dict | None:
    if not SIM_RUNS.exists():
        print(f"  aviso: falta {SIM_RUNS.name}; se omite la simulación de 30 días")
        return None
    df = pd.read_csv(SIM_RUNS)
    if "SR_counter" not in df.columns and "SR_cumulative" in df.columns:
        df["SR_counter"] = df["SR_cumulative"]   # nombre anterior del mismo valor (alias en hij.quality)
    missing = [m for m in SIM_METRICS if m not in df.columns and m not in SIM_OPTIONAL]
    if missing:
        print(f"  aviso: {SIM_RUNS.name} no tiene {', '.join(missing)}; se omiten")
    metrics_here = [m for m in SIM_METRICS if m in df.columns]
    patterns = list(dict.fromkeys(df.pattern))
    summary = {}
    for (pattern, loss, strategy), g in df.groupby(["pattern", "loss", "strategy"], sort=False):
        cell = {"n_runs": len(g)}
        for m in metrics_here:
            x = g[m]
            cell[m] = {"median": x.median(), "q25": x.quantile(0.25), "q75": x.quantile(0.75)}
        summary.setdefault(pattern, {}).setdefault(str(loss), {})[f"S{strategy}"] = cell

    def where_max(col: str, data: pd.DataFrame = df) -> dict:
        """Máximo sobre todas las simulaciones, con las que empatan (a 1e-9) agrupadas por configuración."""
        top = data[col].max()
        tied = data[np.isclose(data[col], top, rtol=0, atol=1e-9)]
        confs = tied.groupby(["pattern", "loss", "strategy"], sort=False).size()
        return {"value": top, "n_runs": len(tied),
                "configurations": [{"pattern": p, "loss": int(lo), "strategy": f"S{int(s)}", "n_runs": int(n)}
                                   for (p, lo, s), n in confs.items()]}

    extra = {}
    if "airtime_tx_s_per_day" in df.columns:   # airtime emitido (también sin cobertura), si sim30.py lo guarda
        extra["max_airtime_tx_s_per_day"] = where_max("airtime_tx_s_per_day")
        if SIM_RUNS_B2880.exists():
            b2880 = pd.read_csv(SIM_RUNS_B2880)
            if "airtime_tx_s_per_day" in b2880.columns:
                extra["max_airtime_tx_s_per_day_B2880"] = where_max("airtime_tx_s_per_day", b2880)
    if "airtime_tx_max_day_s" in df.columns:
        extra["max_airtime_tx_single_day_s"] = where_max("airtime_tx_max_day_s")

    return {
        "source": "software/data/processed/sim30_runs.csv",
        "n_runs": len(df),
        "patients_per_configuration": int(df.groupby(["pattern", "loss", "strategy"]).size().max()),
        "patterns": patterns,
        "losses_percent": sorted(int(x) for x in df.loss.unique()),
        "strategies": [f"S{int(s)}" for s in sorted(df.strategy.unique())],
        "simulated_days": SIM_DAYS,
        "notes": SIM_NOTES,
        "quantiles": "median, q25 and q75 with pandas linear interpolation, as in sim30_summary.csv",
        "metrics": metrics_here,
        "summary": summary,
        "max_airtime_s_per_day": where_max("airtime_s_per_day"),
        "max_downlinks_per_day": where_max("downlinks_per_day"),
    } | extra


# ---------------------------------------------------------------------------------- validación del simulador
def validation_numbers(done_ids: list[str], table_ids: list[str] | None = None) -> dict | None:
    if not SIM_VALIDATION.exists():
        print(f"  aviso: falta {SIM_VALIDATION.name}; se omite la validación del simulador")
        return None
    v = pd.read_csv(SIM_VALIDATION)
    pairs = [c[:-5] for c in v.columns if c.endswith("_real") and f"{c[:-5]}_sim" in v.columns]
    max_abs, max_rel, defined = {}, {}, {}
    for p in pairs:
        a, b = v[f"{p}_real"].astype(float), v[f"{p}_sim"].astype(float)
        ok = a.notna() & b.notna()
        defined[p] = int(ok.sum())
        diff = (a - b).abs()
        max_abs[p] = diff[ok].max() if ok.any() else None
        has_rel = ok & (a.abs() > 0)            # diferencia relativa respecto al valor real (sin divisiones por 0)
        max_rel[p] = (diff[has_rel] / a.abs()[has_rel]).max() if has_rel.any() else None
    validated = list(v["id"])
    not_validated = [i for i in done_ids if i not in validated]
    if not_validated:
        print(f"  aviso: {len(not_validated)} pruebas terminadas sin validar con el simulador "
              f"({', '.join(not_validated)}): repetir simulator/validate_lab.py")
    out = {
        "source": "software/data/processed/sim_validation.csv",
        "n_runs": len(v),
        "runs": validated,
        "runs_not_validated": not_validated,
        "validated_runs_without_done": [i for i in validated if i not in done_ids],
        "frames_same": int(v.frames_same.sum()), "frames_real": int(v.frames_real.sum()),
        "bins_same_steps": int(v.bins_same_steps.sum()), "bins_real": int(v.bins_real.sum()),
        "downlinks_real": int(v.downlinks_real.sum()), "downlinks_sim": int(v.downlinks_sim.sum()),
        "runs_with_frame_differences": int(v.first_diff.notna().sum()) if "first_diff" in v else None,
        "max_abs_diff": max_abs,
        "max_rel_diff": max_rel,
        "runs_with_both_values": defined,
    }
    if "tx_time_diff_s_med" in v:
        out["tx_time_diff_s_med_max"] = v.tx_time_diff_s_med.max()
    out["frames_same_ratio"] = out["frames_same"] / out["frames_real"] if out["frames_real"] else None
    out["bins_same_ratio"] = out["bins_same_steps"] / out["bins_real"] if out["bins_real"] else None
    if table_ids:
        # lo mismo solo con las pruebas de la tabla (las estrategias de Methods), que son las que cita el texto
        t = v[v["id"].isin(table_ids)]
        rel, differ = {}, []
        for p in pairs:
            a, b = t[f"{p}_real"].astype(float), t[f"{p}_sim"].astype(float)
            ok = a.notna() & b.notna()
            if ((a - b).abs()[ok] > 0).any():
                differ.append(p)
            nz = ok & (a.abs() > 0)
            rel[p] = ((a - b).abs()[nz] / a.abs()[nz]).max() if nz.any() else None
        out["table_runs"] = {
            "n_runs": len(t), "runs": list(t["id"]),
            "frames_same": int(t.frames_same.sum()), "frames_real": int(t.frames_real.sum()),
            "frames_sim": int(t.frames_sim.sum()),
            "bins_same_steps": int(t.bins_same_steps.sum()), "bins_real": int(t.bins_real.sum()),
            "bins_sim": int(t.bins_sim.sum()),
            "downlinks_real": int(t.downlinks_real.sum()), "downlinks_sim": int(t.downlinks_sim.sum()),
            "values_that_differ": differ, "max_rel_diff": rel}
    return out


# ----------------------------------------------------------------------------------------------------- salida
LAB_KEYS = ["id", "scenario_id", "strategy", "clock_ref", "bins_generated", "bins_received", "bins_exposed",
            "DC_1h", "DC_24h", "DC_end", "RR", "SR_bins", "SR_counter", "SR_cumulative", "counter_gap_max_h_real",
            "TI", "time_err_med_s", "time_err_p95_s", "time_err_max_s", "DR", "OOR",
            "delay_med_h_real", "delay_p95_h_real", "delay_max_h_real", "FZR_naive", "FZR_aware", "FZR_naive_lastbin",
            "SR_increments", "S0_interval_coverage", "airtime_s_per_day", "downlinks", "downlinks_ttn", "frames",
            "compression", "duration_h_lab"]


def f2(v, d=2):
    s = num(v, d)
    return s if s is not None else NA


def md_lab_table(runs: list[dict]) -> list[str]:
    """Las columnas de tab:lab, con TI y los bins de G."""
    rows = ["| Prueba | DC 24 h | DC fin de la prueba | RR | SR bins | SR contador | DUP | OOR | Retraso mediana (h) "
            "| Retraso P95 (h) | Retraso máximo (h) | FZR ingenua | FZR con calidad | Airtime (s/día) | DL | TI "
            "| Bins gen./rec. |",
            "|---|" + "---:|" * 16]
    for r in runs:
        rows.append(f"| {r['id']} | {f2(r['DC_24h'])} | {f2(r['DC_end'])} | {f2(r['RR'])} | {f2(r['SR_bins'])} "
                    f"| {f2(r.get('SR_counter'))} | {f2(r['DR'])} "
                    f"| {f2(r['OOR'])} | {f2(r.get('delay_med_h_real'))} | {f2(r.get('delay_p95_h_real'))} "
                    f"| {f2(r.get('delay_max_h_real'))} | {f2(r['FZR_naive'])} | {f2(r['FZR_aware'])} "
                    f"| {f2(r['airtime_s_per_day'], 1)} | {r['downlinks']} | {f2(r['TI'])} "
                    f"| {r['bins_generated']}/{r['bins_received']} |")
    return rows


def md_extra_table(runs: list[dict]) -> list[str]:
    """Cifras que no van a tab:lab: error de hora, contador, vistas de la S0 y reloj de referencia."""
    rows = ["| Prueba | Error de hora mediana (s) | P95 (s) | Máximo (s) | Separación máx. del contador (h) "
            "| S0: SR por intervalo | S0: cobertura | S0: FZR ingenua, último bin | Reloj | Tramas del ajuste "
            "| Residuo máx. (ms) |",
            "|---|" + "---:|" * 7 + "---|" + "---:|" * 2]
    for r in runs:
        rows.append(f"| {r['id']} | {f2(r.get('time_err_med_s'), 3)} | {f2(r.get('time_err_p95_s'), 3)} "
                    f"| {f2(r.get('time_err_max_s'), 3)} | {f2(r.get('counter_gap_max_h_real'), 1)} "
                    f"| {f2(r.get('SR_increments'))} | {f2(r.get('S0_interval_coverage'))} "
                    f"| {f2(r.get('FZR_naive_lastbin'))} | {r.get('clock_ref')} | {r.get('clock_frames', NA)} "
                    f"| {f2(r.get('clock_resid_max_ms'), 1)} |")
    return rows


def time_error_numbers(runs: list[dict], other: list[dict]) -> dict:
    """|t̂ - t| agregado sobre los bins recibidos de G de las pruebas de la tabla y de todas (segundos de reloj)."""
    def stats(group: list[dict]) -> dict:
        with_bins = [r for r in group if len(r.get("_time_err_s", ()))]
        if not with_bins:
            return {"runs": [], "n_bins": 0}
        v = pd.concat([r["_time_err_s"] for r in with_bins], ignore_index=True)
        return {"runs": [r["id"] for r in with_bins], "n_bins": len(v), "median_s": v.median(),
                "p95_s": v.quantile(0.95), "max_s": v.max(), "min_s": v.min(),
                "max_run": max(with_bins, key=lambda r: r["_time_err_s"].max())["id"]}
    return {"definition": "absolute difference between the reconstructed and the true start of each received bin "
                          "of G, in wall-clock seconds; quantiles with pandas linear interpolation",
            "runs_not_on_gateway_clock": [r["id"] for r in runs + other if r.get("clock_ref") != "gateway"],
            "table_runs": stats(runs), "all_runs": stats(runs + other)}


def radio_numbers(runs: list[dict], other: list[dict]) -> dict:
    """Tramas transmitidas con cobertura y su recepción, con el RSSI y el SNR de las recibidas en TTN.

    Con cobertura = transmitidas fuera de los cortes reales (quality.frame_fate: delivered, network_lost o
    radio_lost); no cuentan las suprimidas por la emulación, las retenidas por el límite de airtime ni las que
    descartó el filtro del gateway en los cortes reales."""
    def stats(group: list[dict]) -> dict:
        fates = {}
        for r in group:
            for k, v in r.get("_fates", {}).items():
                fates[k] = fates.get(k, 0) + v
        rssi = [x for r in group for x in r.get("_rssi", [])]
        snr = [x for r in group for x in r.get("_snr", [])]
        in_cov = sum(fates.get(k, 0) for k in ("delivered", "network_lost", "radio_lost"))
        return {"frames_in_coverage": in_cov, "frames_delivered": fates.get("delivered", 0), "fates": fates,
                "rssi_dbm_min": min(rssi) if rssi else None, "rssi_dbm_max": max(rssi) if rssi else None,
                "snr_db_min": min(snr) if snr else None, "snr_db_max": max(snr) if snr else None}
    return {"table_runs": stats(runs), "all_runs": stats(runs + other)}


def md_radio(radio: dict) -> list[str]:
    L = []
    for key, name in (("table_runs", "pruebas de la tabla"), ("all_runs", "todas las pruebas")):
        x = radio[key]
        L.append(f"- Tramas transmitidas con cobertura ({name}): {x['frames_in_coverage']}; recibidas en TTN: "
                 f"{x['frames_delivered']}; RSSI de {x['rssi_dbm_min']} a {x['rssi_dbm_max']} dBm; SNR de "
                 f"{x['snr_db_min']} a {x['snr_db_max']} dB. Destino de todas las tramas: {x['fates']}.")
    return L


def md_time_error(te: dict) -> list[str]:
    L = []
    for key, name in (("table_runs", "pruebas de la tabla"), ("all_runs", "todas las pruebas")):
        x = te[key]
        if not x["n_bins"]:
            continue
        L.append(f"- Error de hora |t̂ − t|, {name} con bins ({len(x['runs'])}; {x['n_bins']} bins): mediana "
                 f"{f2(x['median_s'], 3)} s, P95 {f2(x['p95_s'], 3)} s, máximo {f2(x['max_s'], 3)} s "
                 f"({x['max_run']}), mínimo {f2(x['min_s'], 3)} s.")
    if te["runs_not_on_gateway_clock"]:
        L.append("- Aviso: con el reloj del PC (sin tramas emparejadas con la hora del gateway): "
                 + ", ".join(te["runs_not_on_gateway_clock"]) + ".")
    return L


def md_downlink_check(runs: list[dict]) -> str:
    diff = [f"{r['id']} (nodo {r['downlinks']}, TTN {r['downlinks_ttn']})" for r in runs
            if r.get("downlinks_ttn") is not None and r["downlinks_ttn"] != r["downlinks"]]
    unknown = [r["id"] for r in runs if r.get("downlinks_ttn") is None]
    txt = ("Downlinks (DL): los recibe el nodo desde el inicio de la prueba. Contraste con TTN: "
           + ("coinciden en todas las pruebas" if not diff else "no coinciden en " + ", ".join(diff)))
    if unknown:
        txt += "; sin contraste posible en " + ", ".join(unknown)
    return txt + "."


def md_max_line(name: str, unit: str, x: dict) -> str:
    confs = "; ".join(f"{c['pattern']}, pérdida {c['loss']} %, {c['strategy']}: {c['n_runs']}"
                      for c in x["configurations"])
    return (f"Máximo de {name} (media de los {SIM_DAYS} días simulados): {f2(x['value'], 3)} {unit}, en "
            f"{x['n_runs']} simulaciones empatadas ({confs}).")


def write_md(runs: list[dict], other: list[dict], sim: dict | None, val: dict | None, caption: str,
             te: dict | None = None, radio: dict | None = None) -> None:
    L = ["# Cifras para el artículo", "",
         f"Generado por `software/analysis/make_tables.py` el {dt.datetime.now():%Y-%m-%d %H:%M}. "
         "Los valores completos están en `article_numbers.json`.", "",
         "## 1. Pruebas con hardware (tabla `tab:lab`)", "",
         f"{len(runs)} pruebas con fichero DONE en la tabla. Tiempos del laboratorio multiplicados por 15 (la "
         "compresión); airtime de las tramas transmitidas por día de reloj de la prueba, unas 15 veces el de un "
         "despliegue, comparable con el límite de 30 s al día (las tramas sin cobertura emulada o perdidas no se "
         "transmiten). La verdad terreno va en el reloj del gateway (columna Reloj de la segunda tabla). SR bins: "
         "pasos de los bins recibidos (definición de tab:metrics); SR contador: pasos incluidos en el acumulado de "
         "la última trama recibida de cada arranque, que da totales entre tramas recibidas, sin detalle por bins. "
         "FZR por bin. La S0 no envía bins: su DC, su RR y su SR bins son 0 por construcción y su FZR se calcula "
         "con cada trama sobre los bins que cubre; la vista con los pasos de cada trama en su último bin (panel (b) "
         "de fig:timeline) va en la segunda tabla. Pie propuesto: " + caption, ""]
    L += md_lab_table(runs) + ["", md_downlink_check(runs + other), ""]
    L += ["### Error de hora, contador acumulado y vistas de la S0", "",
          "Error de hora |t̂ − t| en segundos de reloj (sin escalar), en los bins recibidos de G. Separación máxima "
          "del contador: mayor intervalo entre dos tramas recibidas seguidas del mismo arranque, a escala de "
          "despliegue. S0: SR por intervalo = pasos de G cubiertos por una trama recibida; cobertura = parte de G "
          "cubierta; FZR ingenua, último bin = los pasos de cada trama en su último bin y 0 en los demás.", ""]
    L += md_extra_table(runs + other) + [""]
    if te:
        L += md_time_error(te) + [""]
    if radio:
        L += md_radio(radio) + [""]
    sec = 2
    if other:
        L += [f"## {sec}. Pruebas fuera de la tabla", "",
              "La tabla solo lleva las estrategias de Methods (S0 a S3). Para citar estas pruebas en el texto hace "
              "falta describirlas antes en Methods (la S4 usa uplinks confirmados).", ""]
        L += md_lab_table(other) + [""]
        sec += 1

    if sim:
        L += [f"## {sec}. Simulación de 30 días", "",
              f"{sim['n_runs']} simulaciones, {sim['patients_per_configuration']} pacientes virtuales por "
              "configuración. Cada celda: mediana [q25, q75]. DC fin del seguimiento = lo recibido al acabar los "
              "30 días; DC tras la devolución = lo recibido tras 3 días más con cobertura continua (devolución del "
              f"dispositivo). Airtime y downlinks: media por día de los {SIM_DAYS} días simulados (30 de "
              "seguimiento y 3 de devolución). Retrasos a escala de despliegue.", ""]
        for loss in sim["losses_percent"]:
            for title, keys in (("completitud", SIM_COMPLETENESS), ("retraso, falsos ceros y coste", SIM_DELAY_COST)):
                keys = [k for k in keys if k in sim["metrics"]]
                L += [f"### Pérdida media en las visitas: {loss} %; {title}", "",
                      "| Visitas | Estrategia | " + " | ".join(SIM_HEADS[k] for k in keys) + " |",
                      "|---|---|" + "---:|" * len(keys)]
                for pattern in sim["patterns"]:
                    for s, cell in sim["summary"].get(pattern, {}).get(str(loss), {}).items():
                        vals = []
                        for k in keys:
                            q = cell[k]
                            if all(num(q[x], 2) is None for x in ("median", "q25", "q75")):
                                vals.append(NA)
                            else:
                                vals.append(f"{f2(q['median'])} [{f2(q['q25'])}, {f2(q['q75'])}]")
                        L.append(f"| {pattern} | {s} | " + " | ".join(vals) + " |")
                L.append("")
        L += [md_max_line("airtime recibido", "s/día", sim["max_airtime_s_per_day"])]
        if "max_airtime_tx_s_per_day" in sim:
            L.append(md_max_line("airtime transmitido", "s/día", sim["max_airtime_tx_s_per_day"]))
        if "max_airtime_tx_s_per_day_B2880" in sim:
            L.append(md_max_line("airtime transmitido con B = 2880", "s/día", sim["max_airtime_tx_s_per_day_B2880"]))
        if "max_airtime_tx_single_day_s" in sim:
            x = sim["max_airtime_tx_single_day_s"]
            L.append(f"Máximo de airtime transmitido en un solo día: {f2(x['value'], 3)} s.")
        L += [md_max_line("downlinks", "al día", sim["max_downlinks_per_day"]), ""]
        sec += 1

    if val:
        L += [f"## {sec}. Validación del simulador frente al hardware", "",
              f"{val['n_runs']} pruebas ({', '.join(val['runs'])}).", ""]
        if val["runs_not_validated"]:
            L += [f"Aviso: {len(val['runs_not_validated'])} pruebas terminadas aún sin validar "
                  f"({', '.join(val['runs_not_validated'])}); repetir `simulator/validate_lab.py` y este script.", ""]
        L += [f"- Tramas iguales: {val['frames_same']}/{val['frames_real']}.",
              f"- Bins con los mismos pasos: {val['bins_same_steps']}/{val['bins_real']}.",
              f"- Downlinks: {val['downlinks_real']} reales y {val['downlinks_sim']} simulados.",
              f"- Pruebas con alguna trama distinta: {val['runs_with_frame_differences']}.",
              f"- Diferencia máxima de la mediana del instante de transmisión: "
              f"{f2(val.get('tx_time_diff_s_med_max'), 3)} s.", "",
              "| Métrica | Máx. diferencia absoluta | Máx. diferencia relativa | Pruebas con ambos valores |",
              "|---|---:|---:|---:|"]
        for p, x in val["max_abs_diff"].items():
            rel = val["max_rel_diff"][p]
            L.append(f"| {p} | {f2(x, 4)} | {f2(rel * 100, 2) + ' %' if rel is not None else NA} "
                     f"| {val['runs_with_both_values'][p]} |")
        L.append("")
        t = val.get("table_runs")
        if t:
            worst = {p: r for p, r in t["max_rel_diff"].items() if p in t["values_that_differ"] and r is not None}
            L += [f"Solo las {t['n_runs']} pruebas de la tabla (las que cita el texto): tramas iguales "
                  f"{t['frames_same']}/{t['frames_real']} (simuladas {t['frames_sim']}), bins con los mismos pasos "
                  f"{t['bins_same_steps']}/{t['bins_real']} (simulados {t['bins_sim']}), downlinks "
                  f"{t['downlinks_real']} reales y {t['downlinks_sim']} simulados. Valores que no coinciden "
                  "exactamente (diferencia relativa máxima): "
                  + (", ".join(f"{p} {f2(r * 100, 2)} %" for p, r in worst.items()) or "ninguno") + ".", ""]
    NUMBERS_MD.write_text("\n".join(L), encoding="utf-8", newline="\n")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("Pruebas con hardware (carpetas con DONE):")
    runs, other = lab_runs()
    if not runs:
        print("  no hay pruebas terminadas: no se genera la tabla")
        caption = ""
    else:
        caption = write_table(runs)
    sim = sim30_numbers()
    val = validation_numbers([r["id"] for r in runs + other], [r["id"] for r in runs])
    te = time_error_numbers(runs, other)
    radio = radio_numbers(runs, other)

    def lab_record(r: dict) -> dict:
        return {k: r.get(k) for k in LAB_KEYS} | {k: x for k, x in r.items()
                                                  if k not in LAB_KEYS and not k.startswith("_")}

    data = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "lab": {"n_runs": len(runs), "table": "HIJ_2026_extension/tables/lab_results.tex", "notes": LAB_NOTES,
                "runs": [lab_record(r) for r in runs],
                "excluded_runs": [lab_record(r) | {"reason": "strategy not defined in Methods (S0 to S3)"}
                                  for r in other],
                "time_error": te,
                "radio": radio,
                "downlink_mismatches": [r["id"] for r in runs + other
                                        if r.get("downlinks_ttn") is not None and r["downlinks_ttn"] != r["downlinks"]]},
        "sim30": sim,
        "validation": val,
    }
    NUMBERS_JSON.write_text(json.dumps(clean(data), indent=1, ensure_ascii=False, allow_nan=False),
                            encoding="utf-8")
    write_md(runs, other, sim, val, caption, te, radio)

    print(f"  {len(runs)} pruebas en la tabla: {', '.join(r['id'] for r in runs)}")
    if runs:
        print(f"  tabla: {TABLE_TEX}")
    for key, name in (("table_runs", "pruebas de la tabla"), ("all_runs", "todas las pruebas")):
        x = te[key]
        if x["n_bins"]:
            print(f"  error de hora ({name}, {x['n_bins']} bins): mediana {x['median_s']:.3f} s, P95 "
                  f"{x['p95_s']:.3f} s, máximo {x['max_s']:.3f} s")
    if sim:
        print(f"Simulación de 30 días: {sim['n_runs']} simulaciones; máximo {sim['max_airtime_s_per_day']['value']:.2f} "
              f"s de airtime y {sim['max_downlinks_per_day']['value']:.2f} downlinks al día (media de {SIM_DAYS} días)")
    if val:
        print(f"Validación: {val['n_runs']} pruebas, tramas {val['frames_same']}/{val['frames_real']}, bins "
              f"{val['bins_same_steps']}/{val['bins_real']}, downlinks {val['downlinks_real']} reales y "
              f"{val['downlinks_sim']} simulados")
    print(f"Cifras: {NUMBERS_JSON}\n        {NUMBERS_MD}")


if __name__ == "__main__":
    main()
