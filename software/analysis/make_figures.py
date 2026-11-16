"""Figuras del artículo a partir de las pruebas con hardware y de la simulación de 30 días.

Uso:
    python software/analysis/make_figures.py                     todas las figuras
    python software/analysis/make_figures.py sim30 lab_timeline  solo las indicadas

Cada figura se guarda en HIJ_2026_extension/figures/ como PDF vectorial y PNG a 300 dpi con el mismo nombre,
a su tamaño final (ancho de texto del manuscrito, 124 mm). Solo lee las carpetas de software/data/raw que
tienen fichero DONE y no escribe nada en ellas. Al final lista las entradas que faltan: se puede repetir a medida
que avanza la campaña y las figuras recogen las pruebas terminadas en ese momento.

Figuras:
    sim30             simulación de 30 días: DC a tres horizontes en orden temporal (a 24 h, día 30 = final del
                      seguimiento, y tras la devolución = 3 días más de cobertura continua), con la mediana y el
                      rango intercuartílico de los pacientes virtuales, por intervalo entre visitas, pérdida y
                      estrategia (S1 a S3)
    lab_availability  laboratorio: fracción de los bins evaluables recibida en las h horas siguientes al fin del
                      bin, un panel por escenario (C2, C3 al 10 %, C3 al 30 %, C4 de 24 h), estrategias S1 a S3;
                      cada curva acaba en el mayor retraso observable de su prueba y su valor final es DC_end
    lab_timeline      laboratorio, corte de 24 h (C4-24): verdad terreno frente a lo que muestran la S0, la S1
                      (datos ausentes como 0 y como "sin datos") y la S3 al final de la prueba

Estilo común: Times, 7 a 9 pt, líneas de al menos 0,5 pt, sin títulos (los pies van en LaTeX). Paleta de las
estrategias: "high contrast" de Paul Tol para S1 a S3 (se distinguen con daltonismo y en escala de grises, porque
su luminosidad es muy distinta) y gris para la S0, la línea base; cada estrategia lleva además su marcador y su
tipo de línea. Los tiempos se dan a escala de despliegue (tiempo del laboratorio por la compresión, 15x).
"""
import pathlib
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from PIL import Image  # noqa: E402

SOFTWARE = pathlib.Path(__file__).resolve().parents[1]
ROOT = SOFTWARE.parent
RAW = SOFTWARE / "data" / "raw"
PROCESSED = SOFTWARE / "data" / "processed"
FIGURES = ROOT / "HIJ_2026_extension" / "figures"
SIM_RUNS = PROCESSED / "sim30_runs.csv"

sys.path.insert(0, str(SOFTWARE / "backend"))
from hij.experiment import load_experiment  # noqa: E402
from hij.quality import bin_table, metrics, s0_intervals  # noqa: E402

MM = 1 / 25.4
TEXT_W = 124 * MM  # ancho de texto del manuscrito (una columna)

# ------------------------------------------------------------------------------------------------ estilo
STRATEGIES = {
    0: dict(label="S0 latest state", color="#999999", marker="D", ls=(0, (5, 1.5, 1, 1.5))),
    1: dict(label="S1 store-and-forward", color="#DDAA33", marker="o", ls=(0, (1, 1.3))),
    2: dict(label="S2 with redundancy", color="#BB5566", marker="s", ls=(0, (4, 1.6))),
    3: dict(label="S3 acknowledged", color="#004488", marker="^", ls="-"),
}
INK = "#000000"
GT_COLOR = "#333333"     # verdad terreno
S3_LATE = "#6699CC"      # bins de la S3 recuperados tras el corte (tono claro del azul de la S3)
OUTAGE = "#E6E6E6"       # sombreado del corte
NODATA = "#808080"       # rayado de "sin datos"
GRID = "#E0E0E0"

# fallos del primer envío atribuibles a la falta de cobertura (hij.quality.frame_fate)
OUTAGE_FATES = {"emulated_outage", "gateway_filtered", "gateway_down_or_filtered"}


def setup_style():
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Times", "STIXGeneral"],
        "mathtext.fontset": "stix",
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.titlesize": 8,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 7,
        "axes.linewidth": 0.5,
        "axes.edgecolor": INK,
        "axes.labelcolor": INK,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.unicode_minus": False,
        "lines.linewidth": 1.0,
        "xtick.color": INK,
        "ytick.color": INK,
        "xtick.major.width": 0.5,
        "ytick.major.width": 0.5,
        "xtick.minor.width": 0.5,
        "ytick.minor.width": 0.5,
        "xtick.major.size": 2.5,
        "ytick.major.size": 2.5,
        "xtick.minor.size": 1.5,
        "ytick.minor.size": 1.5,
        "xtick.major.pad": 2,
        "ytick.major.pad": 2,
        "hatch.linewidth": 0.5,
        "legend.frameon": False,
        "legend.handlelength": 2.2,
        "legend.columnspacing": 1.4,
        "legend.handletextpad": 0.5,
        "legend.borderaxespad": 0.2,
        "savefig.dpi": 300,
        "figure.dpi": 100,
    })


def save(fig, name: str) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    # sin fecha de creación en el PDF: al regenerar con los mismos datos el fichero sale idéntico
    fig.savefig(FIGURES / f"{name}.pdf", facecolor="white", metadata={"CreationDate": None})
    png = FIGURES / f"{name}.png"
    fig.savefig(png, dpi=300, facecolor="white")
    plt.close(fig)
    # PNG en RGB sin canal alfa (algunos sistemas de producción lo rechazan): se compone sobre blanco
    with Image.open(png) as im:
        im.load()
        rgb = Image.new("RGB", im.size, "white")
        rgb.paste(im, mask=im.getchannel("A") if im.mode == "RGBA" else None)
    rgb.save(png, dpi=(300, 300))
    print(f"  guardada {FIGURES / name}.pdf y .png")


def strategy_handle(s: int, with_marker: bool = True, lw: float = 1.0) -> Line2D:
    st = STRATEGIES[s]
    return Line2D([], [], color=st["color"], ls=st["ls"], lw=lw,
                  marker=st["marker"] if with_marker else None, ms=3.5, label=st["label"])


# ------------------------------------------------------------------------------------------------ datos
def done(pid: str) -> bool:
    return (RAW / pid / "DONE").exists()


def load(pid: str, missing: list):
    """Carga una prueba terminada; si falla, la apunta como entrada que falta y devuelve None."""
    try:
        return load_experiment(RAW / pid)
    except Exception as err:  # noqa: BLE001  (una prueba defectuosa no debe impedir las demás figuras)
        print(f"    aviso: no se pudo cargar {pid}: {err}")
        missing.append(f"{pid} (error al cargar)")
        return None


def eligible_bins(exp) -> pd.DataFrame:
    """bin_table restringida a los bins evaluables, con el mismo criterio que hij.quality.metrics."""
    bins = bin_table(exp)
    period = exp.config.get("period", 240)
    eval_s = exp.plan.get("eval_s") if exp.plan.get("eval_s") != exp.plan.get("duration_s") else None
    if eval_s:
        cutoff = exp.exp_start + pd.Timedelta(seconds=eval_s)
    else:
        cutoff = exp.end - pd.Timedelta(seconds=period + 30)
    return bins[bins.t_end <= cutoff].reset_index(drop=True)


def bin_hours(exp) -> float:
    """Duración de un bin a escala de despliegue, en horas (0,25 h)."""
    return exp.delta_s * exp.compression / 3600


# ------------------------------------------------------------------------------------------------ sim30
PATTERNS = [("daily", 1.0, "1 d"), ("every 2 days", 2.0, "2 d"), ("twice weekly", 3.5, "3.5 d"),
            ("weekly", 7.0, "7 d")]  # (patrón del simulador, días entre visitas, etiqueta del eje)
LOSSES = [0, 10, 30]


def fig_sim30(missing: list) -> None:
    """DC de la simulación de 30 días a tres horizontes, en filas por orden temporal (S0 se omite: no envía bins,
    su DC es 0). DC_24h: bins recibidos como mucho 24 h después de su fin; DC_followup: recibidos al final del
    seguimiento (día 30); DC_end: recibidos tras la devolución del dispositivo (3 días más de cobertura continua).
    Las tres se refieren a los bins generados en los 30 días."""
    if not SIM_RUNS.exists():
        missing.append(str(SIM_RUNS))
        return
    runs = pd.read_csv(SIM_RUNS)
    rows = [("DC_24h", r"$DC$(24 h)"), ("DC_followup", r"$DC$, day 30"), ("DC_end", r"$DC$, after return")]
    absent = [col for col, _ in rows if col not in runs.columns]
    if absent:
        missing.append(f"columnas {', '.join(absent)} de {SIM_RUNS.name}")
        return
    dodge = {1: -0.15, 2: 0.0, 3: 0.15}
    fig, axes = plt.subplots(len(rows), 3, figsize=(TEXT_W, 108 * MM), sharex=True, sharey=True,
                             layout="constrained")
    for i, (col, ylabel) in enumerate(rows):
        for j, loss in enumerate(LOSSES):
            ax = axes[i, j]
            for s in (1, 2, 3):
                st = STRATEGIES[s]
                x, med, q1, q3 = [], [], [], []
                for pattern, days, _ in PATTERNS:
                    v = runs[(runs.pattern == pattern) & (runs.loss == loss) & (runs.strategy == s)][col].dropna()
                    if v.empty:
                        missing.append(f"{pattern}, {loss} %, S{s}")
                        continue
                    x.append(np.log2(days) + dodge[s])
                    med.append(v.median())
                    q1.append(v.quantile(0.25))
                    q3.append(v.quantile(0.75))
                med, q1, q3 = map(np.asarray, (med, q1, q3))
                ax.errorbar(x, med, yerr=[med - q1, q3 - med], color=st["color"], ls=st["ls"], lw=0.8,
                            marker=st["marker"], ms=3.0, mew=0.6, elinewidth=0.6, capsize=1.8, capthick=0.6,
                            clip_on=False, zorder=3)
            ax.set_ylim(-0.03, 1.03)
            ax.set_yticks([0, 0.25, 0.5, 0.75, 1])
            ax.set_yticklabels(["0", "", "0.5", "", "1"])
            ax.yaxis.grid(True, color=GRID, lw=0.5)
            ax.set_axisbelow(True)
            ax.set_xlim(-0.45, np.log2(7) + 0.45)
            ax.set_xticks([np.log2(d) for _, d, _ in PATTERNS])
            ax.set_xticklabels([t for _, _, t in PATTERNS])
            if i == 0:
                ax.set_title(f"{loss}% loss", pad=3)
            if j == 0:
                ax.set_ylabel(ylabel)
    fig.supxlabel("Interval between visits", fontsize=8)
    fig.legend(handles=[strategy_handle(s) for s in (1, 2, 3)], loc="outside upper center", ncol=3)
    save(fig, "sim30")


# ------------------------------------------------------------------------------------------------ lab_availability
FAMILIES = [("C2", "C2 daily visit"), ("C3-10", "C3 burst loss 10%"), ("C3-30", "C3 burst loss 30%"),
            ("C4-24", "C4 24-h outage")]
H_MIN, H_MAX = 0.1, 100.0
# grosor de cada curva: la S1 (amarillo claro y punteada) va más gruesa porque es la más tenue impresa en gris
CURVE_LW = {1: 1.4, 2: 1.1, 3: 1.1}


def availability_curve(exp):
    """Escalera F(h) = fracción de los bins evaluables recibida como mucho h horas después del fin del bin.

    El denominador son todos los bins evaluables (como DC_end en metrics), así que la curva acaba en DC_end. No
    es DC(h) de metrics para h > 1 h: DC(24 h) solo cuenta los bins que acabaron al menos 24 h antes del final.
    La curva se corta en el mayor retraso observable en la prueba (último registro menos el fin del primer bin
    evaluable, a escala de despliegue): más allá no hay observación."""
    elig = eligible_bins(exp)
    n = len(elig)
    if n == 0:
        return None
    h_end = min(H_MAX, (exp.end - elig.t_end.min()).total_seconds() * exp.compression / 3600)
    d = np.sort(elig.loc[elig.received, "delay_s"].to_numpy() * exp.compression / 3600)
    if (d > h_end).any():
        print(f"    aviso: {int((d > h_end).sum())} bins con retraso > {h_end:.1f} h quedan fuera de la curva")
    y0 = (d <= H_MIN).sum() / n
    later = d[(d > H_MIN) & (d <= h_end)]
    x = np.concatenate([[H_MIN], later, [h_end]])
    y = np.concatenate([[y0], y0 + np.arange(1, len(later) + 1) / n, [y0 + len(later) / n]])
    return x, y, n, d, h_end


def fig_lab_availability(missing: list) -> None:
    panels = []
    for fam, title in FAMILIES:
        runs = []
        for s in (1, 2, 3):
            pid = f"{fam}_S{s}"
            if done(pid):
                runs.append((s, pid))
            else:
                missing.append(pid)
        if runs:
            panels.append((fam, title, runs))
    if not panels:
        return
    n = len(panels)
    nrows, ncols = (2, 2) if n == 4 else (1, n)
    height = (52 if nrows == 1 else 92) * MM
    fig, axes = plt.subplots(nrows, ncols, figsize=(TEXT_W, height), sharex=True, sharey=True,
                             layout="constrained", squeeze=False)
    used = set()
    for ax, (fam, title, runs) in zip(axes.flat, panels):
        for s, pid in runs:
            exp = load(pid, missing)
            if exp is None:
                continue
            curve = availability_curve(exp)
            if curve is None:
                print(f"    aviso: {pid} no tiene bins evaluables; se omite")
                continue
            x, y, nbins, d, h_end = curve
            m = metrics(exp)
            print(f"    {pid}: {nbins} bins evaluables, curva hasta {h_end:.2f} h, F(1 h) = "
                  f"{(d <= 1).sum() / nbins:.3f} (DC_1h de metrics = {m['DC_1h']:.3f}), final = {y[-1]:.3f} "
                  f"(DC_end de metrics = {m['DC_end']:.3f}); F(24 h) = {(d <= 24).sum() / nbins:.3f} no es "
                  f"DC_24h ({m['DC_24h']:.3f}) y no se marca")
            st = STRATEGIES[s]
            ax.step(x, y, where="post", color=st["color"], ls=st["ls"], lw=CURVE_LW[s], zorder=3)
            used.add(s)
        ax.set_title(title, pad=3)
        ax.set_xscale("log")
        ax.set_xlim(H_MIN, H_MAX)
        ax.set_ylim(0, 1.02)
        ax.set_xticks([0.1, 1, 10, 100])
        ax.set_xticklabels(["0.1", "1", "10", "100"])
        ax.set_yticks([0, 0.25, 0.5, 0.75, 1])
        ax.set_yticklabels(["0", "", "0.5", "", "1"])
        ax.yaxis.grid(True, color=GRID, lw=0.5)
        ax.axvline(1, color=GRID, lw=0.5, zorder=0)  # referencia de DC(1 h), que coincide con la curva
        ax.set_axisbelow(True)
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    fig.supxlabel("Time after the end of the bin (h)", fontsize=8)
    fig.supylabel("Fraction of bins received", fontsize=8)
    fig.legend(handles=[strategy_handle(s, with_marker=False, lw=CURVE_LW[s]) for s in sorted(used)],
               loc="outside upper center", ncol=len(used))
    save(fig, "lab_availability")


# ------------------------------------------------------------------------------------------------ lab_timeline
TIMELINE_RUNS = {0: "C4-24_S0", 1: "C4-24_S1", 3: "C4-24_S3"}


def step_fill(ax, x0: np.ndarray, width: float, values: np.ndarray, color: str, zorder: float = 2) -> None:
    """Barras contiguas de un bin de ancho como un único polígono escalonado (sin juntas entre barras)."""
    v = np.nan_to_num(np.asarray(values, dtype=float))
    edges = np.append(x0, x0[-1] + width)
    xs = np.repeat(edges, 2)[1:-1]
    ys = np.repeat(v, 2)
    ax.fill_between(xs, 0, ys, color=color, lw=0, zorder=zorder)


def runs_of(mask: np.ndarray):
    """Tramos consecutivos (inicio, fin incluido) donde mask es True."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    cuts = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[cuts + 1]]
    ends = np.r_[idx[cuts], idx[-1]]
    return list(zip(starts, ends))


def fig_lab_timeline(missing: list) -> None:
    absent = [pid for pid in TIMELINE_RUNS.values() if not done(pid)]
    if absent:
        missing.extend(absent)
        return
    exps = {s: load(pid, missing) for s, pid in TIMELINE_RUNS.items()}
    if any(e is None for e in exps.values()):
        return
    mets = {s: metrics(e) for s, e in exps.items()}
    ref = exps[1]
    bh = bin_hours(ref)
    tables = {s: eligible_bins(e).set_index("k") for s, e in exps.items()}
    ks = sorted(set.intersection(*(set(t.index) for t in tables.values())))
    gt = tables[1].loc[ks, "steps"].to_numpy()
    for s, t in tables.items():
        if not np.array_equal(t.loc[ks, "steps"].to_numpy(), gt):
            print(f"    aviso: la verdad terreno de {TIMELINE_RUNS[s]} no coincide con la de {TIMELINE_RUNS[1]}")
    k = np.asarray(ks)
    x0 = k * bh

    # S0: el incremento de cada trama se pinta en el último bin evaluable que cubre (vista tipo CBMS; se contrasta
    # con FZR_naive_lastbin, porque FZR_naive de la S0 muestra cada trama sobre todos los bins que cubre)
    t0 = tables[0].loc[ks]
    pos = {(ep, sq): kk for ep, sq, kk in zip(t0.epoch, t0.seq, t0.index)}
    s0_shown = pd.Series(0.0, index=ks)
    for r in s0_intervals(exps[0]).itertuples():
        covered = [pos[(r.epoch, sq)] for sq in range(r.s_first, r.s_last + 1) if (r.epoch, sq) in pos]
        if covered:
            s0_shown[covered[-1]] = r.increment
    s0_shown = s0_shown.to_numpy()

    t1 = tables[1].loc[ks]
    s1_value = t1["value"].where(t1["received"]).to_numpy()  # NaN = no recibido
    t3 = tables[3].loc[ks]
    s3_value = t3["value"].where(t3["received"]).to_numpy()
    s3_late = (t3["received"] & t3["first_fate"].isin(OUTAGE_FATES)).to_numpy()

    cfg = ref.config
    out_a = cfg.get("outstart", 0) * ref.compression / 3600
    out_b = out_a + cfg.get("outlen", 0) * ref.compression / 3600

    # Filas: lo que se muestra (NaN = "sin datos", rayado), capas de color, métrica de metrics() con la que se
    # contrastan los falsos ceros (bins mostrados como 0 con pasos en la verdad terreno) y escala propia (solo la
    # S0, que pinta en un bin el incremento de varios).
    rows = [
        dict(label="(a) Ground truth", shown=gt.astype(float), layers=[(gt, GT_COLOR)], check=None),
        dict(label="(b) S0, steps of each frame in its last bin", shown=s0_shown,
             layers=[(s0_shown, STRATEGIES[0]["color"])], check=(0, "FZR_naive_lastbin"), own_scale=True),
        dict(label="(c) S1, missing bins shown as zero", shown=np.nan_to_num(s1_value),
             layers=[(np.nan_to_num(s1_value), STRATEGIES[1]["color"])], check=(1, "FZR_naive")),
        dict(label="(d) S1, missing bins shown as no data", shown=s1_value,
             layers=[(s1_value, STRATEGIES[1]["color"])], check=(1, "FZR_aware")),
        dict(label="(e) S3, end of the run", shown=s3_value,
             layers=[(np.where(s3_late, np.nan, s3_value), STRATEGIES[3]["color"]),
                     (np.where(s3_late, s3_value, np.nan), S3_LATE)], check=(3, "FZR_aware")),
    ]
    for r in rows:
        r["fz"] = (r["shown"] == 0) & (gt > 0) if r["check"] else np.zeros(len(gt), dtype=bool)
        r["nodata"] = np.isnan(r["shown"])
        if r["check"]:
            s, key = r["check"]
            zeros = int((r["shown"] == 0).sum())
            fzr = r["fz"].sum() / zeros if zeros else float("nan")
            print(f"    {r['label']}: {int(r['fz'].sum())} falsos ceros de {zeros} ceros mostrados = {fzr:.3f} "
                  f"({key} de metrics en {TIMELINE_RUNS[s]} = {mets[s][key]:.3f}); "
                  f"{int(r['nodata'].sum())} bins sin datos")
    print(f"    (e): {int(s3_late.sum())} bins de la S3 recuperados tras el corte")

    # Las filas con falsos ceros llevan debajo de la línea de base una franja para las marcas y su altura crece
    # en esa proporción, de modo que todas las filas tienen la misma altura útil.
    def nice_tick(vmax):
        step = 10 ** np.floor(np.log10(vmax))
        return np.floor(vmax / step) * step

    vmax = max(np.nanmax(r["shown"]) for r in rows if not r.get("own_scale"))
    for r in rows:
        v = np.nanmax(r["shown"]) if r.get("own_scale") else vmax
        r["top"], r["tick"] = 1.15 * v, nice_tick(v)
    rug = 0.30
    ratios = [1 + rug if r["fz"].any() else 1 for r in rows]
    fig, axes = plt.subplots(len(rows), 1, figsize=(TEXT_W, 92 * MM), sharex=True, layout="constrained",
                             gridspec_kw=dict(height_ratios=ratios))
    for ax, r in zip(axes, rows):
        top = r["top"]
        ax.axvspan(out_a, out_b, color=OUTAGE, lw=0, zorder=0)
        for k0, k1 in runs_of(r["nodata"]):
            ax.axvspan(x0[k0], x0[k1] + bh, ymin=rug / (1 + rug) if r["fz"].any() else 0, facecolor="none",
                       edgecolor=NODATA, hatch="//////", lw=0, zorder=1)
        for values, color in r["layers"]:
            step_fill(ax, x0, bh, values, color)
        ax.set_title(r["label"], loc="left", pad=2, fontsize=8)
        ax.set_yticks([0, r["tick"]])
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}")
        ax.spines["left"].set_bounds(0, r["tick"])
        ax.tick_params(axis="x", length=0)
        if r["fz"].any():
            ax.set_ylim(-rug * top, top)
            ax.spines["bottom"].set_visible(False)
            ax.axhline(0, color=INK, lw=0.5, zorder=4)
            ax.vlines(x0[r["fz"]] + bh / 2, -0.85 * rug * top, -0.2 * rug * top, color=INK, lw=0.5, zorder=4)
        else:
            ax.set_ylim(0, top)
    last = axes[-1]
    last.spines["bottom"].set_visible(True)
    last.tick_params(axis="x", length=2.5)
    last.set_xlim(0, x0[-1] + bh)
    last.set_xticks(np.arange(0, x0[-1] + bh + 0.01, 6))
    last.set_xlabel("Time since start (h)")
    fig.supylabel("Steps per bin", fontsize=8)
    handles = [
        Patch(facecolor=OUTAGE, edgecolor="none", label="Outage"),
        Patch(facecolor="white", edgecolor=NODATA, hatch="//////", lw=0, label="No data"),
        Line2D([], [], color=INK, marker="|", ls="none", ms=5, mew=0.5, label="False zero"),
        Patch(facecolor=S3_LATE, edgecolor="none", label="S3, recovered after the outage"),
    ]
    fig.legend(handles=handles, loc="outside upper center", ncol=4)
    save(fig, "lab_timeline")


# ------------------------------------------------------------------------------------------------ main
FIGS = {"sim30": fig_sim30, "lab_availability": fig_lab_availability, "lab_timeline": fig_lab_timeline}


def main(argv: list[str]) -> None:
    names = argv or list(FIGS)
    unknown = [n for n in names if n not in FIGS]
    if unknown:
        raise SystemExit(f"figuras desconocidas: {', '.join(unknown)} (hay: {', '.join(FIGS)})")
    setup_style()
    missing_all = {}
    for name in names:
        print(f"{name}:")
        missing = []
        FIGS[name](missing)
        if missing:
            missing_all[name] = missing
    if missing_all:
        print("\nEntradas que faltan (se omiten; repetir cuando estén):")
        for name, items in missing_all.items():
            print(f"  {name}: {', '.join(items)}")
    else:
        print("\nNo falta ninguna entrada.")


if __name__ == "__main__":
    main(sys.argv[1:])
