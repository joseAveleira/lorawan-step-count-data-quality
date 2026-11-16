"""Genera la página de resumen para el profesional clínico (HTML autocontenido).

Uso:
    python backend/clinician_page.py <id_experimento> [--out pagina.html]       datos reales (software/data/raw/<id>)
    python backend/clinician_page.py --demo C2 [--out pagina.html]                  despliegue simulado de 7 días
"""
import argparse
import datetime as dt
import html
import pathlib
import sys

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "simulator"))
from hij.clinical import SLOTS_PER_DAY, summarize, weekly_text  # noqa: E402

RAW = HERE.parent / "data" / "raw"
STATE_LABEL = {"observed": "Dato recibido", "zero": "0 pasos (inactividad confirmada)", "pending": "Pendiente de llegar",
               "lost": "Perdido", "no_contact": "Sin conexión", "aggregate": "Total sin detalle horario"}
DAY_LABEL = {"complete": "Completo", "inactive": "Inactivo", "partial": "Parcial", "no_data": "Sin datos", "ongoing": "En curso",
             "aggregate": "Total sin detalle"}
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

CSS = """
:root { --bg:#f7f8fa; --card:#fff; --ink:#1d2430; --muted:#5d6878; --line:#e3e7ee;
  --obs:#2f9e62; --zero:#6aa7d8; --pend:#e9a23b; --lost:#d1495b; --none:#cfd5de; --agg:#8e7cc3; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#12161c; --card:#1b2129; --ink:#e7ebf1;
  --muted:#9aa5b4; --line:#2b333e; --none:#3a4350; } }
:root[data-theme="dark"] { --bg:#12161c; --card:#1b2129; --ink:#e7ebf1; --muted:#9aa5b4; --line:#2b333e; --none:#3a4350; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width:980px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:0 0 12px; }
.sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin-bottom:16px; }
.summary { font-size:16px; } .legend { display:flex; flex-wrap:wrap; gap:10px 18px; font-size:13px; color:var(--muted); }
.sw { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px; vertical-align:-1px; }
table { width:100%; border-collapse:collapse; font-size:14px; } th, td { text-align:left; padding:6px 8px;
  border-bottom:1px solid var(--line); } th { color:var(--muted); font-weight:600; font-size:12px; }
.chip { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; font-weight:600; color:#fff; }
svg text { fill:var(--muted); font-size:11px; } .scroll { overflow-x:auto; }
"""

COLORS = {"complete": "var(--obs)", "inactive": "var(--zero)", "partial": "var(--pend)", "no_data": "var(--none)", "ongoing": "var(--muted)",
          "aggregate": "var(--agg)", "observed": "var(--obs)", "zero": "var(--zero)", "pending": "var(--pend)",
          "lost": "var(--lost)", "no_contact": "var(--none)"}


def fmt_int(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


def day_name(d: pd.Timestamp) -> str:
    return f"{DIAS[d.weekday()]} {d.day}/{d.month}"


def bars_svg(days: pd.DataFrame) -> str:
    w, h, pad, bw = 900, 200, 30, None
    n = max(len(days), 1)
    bw = min(90, (w - 2 * pad) / n * 0.7)
    step = (w - 2 * pad) / n
    top = max(days.steps.max(), 1)
    out = [f'<svg viewBox="0 0 {w} {h + 40}" width="100%" role="img" aria-label="Pasos por día">']
    for i, r in enumerate(days.itertuples()):
        x = pad + i * step + (step - bw) / 2
        bh = 0 if r.status == "no_data" else max(2, (h - 30) * r.steps / top)
        y = h - bh
        color = COLORS[r.status]
        if r.status == "no_data":
            out.append(f'<rect x="{x:.1f}" y="{h - 26}" width="{bw:.1f}" height="26" rx="4" fill="none" '
                       f'stroke="var(--none)" stroke-dasharray="4 3"/>')
            out.append(f'<text x="{x + bw / 2:.1f}" y="{h - 9}" text-anchor="middle">sin datos</text>')
        else:
            dash = ' stroke="var(--ink)" stroke-dasharray="3 3" stroke-width="1"' if r.status == "partial" else ""
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="4" fill="{color}"{dash}>'
                       f'<title>{html.escape(day_name(r.date))}: {fmt_int(r.steps)} pasos ({DAY_LABEL[r.status]})</title></rect>')
            label = ("≥ " if r.status in ("partial", "ongoing") else "") + fmt_int(r.steps)
            out.append(f'<text x="{x + bw / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle" '
                       f'style="fill:var(--ink);font-weight:600">{label}</text>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{h + 18}" text-anchor="middle">{html.escape(day_name(r.date))}</text>')
    out.append(f'<line x1="{pad}" y1="{h}" x2="{w - pad}" y2="{h}" stroke="var(--line)"/></svg>')
    return "".join(out)


def ribbon_svg(slots: pd.DataFrame, days: pd.DataFrame) -> str:
    cw, ch, gap, left = 8, 14, 4, 70
    w = left + SLOTS_PER_DAY * cw + 10
    h = len(days) * (ch + gap) + 24
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Calidad de los datos por franja de 15 min">']
    for hr in (0, 6, 12, 18, 24):
        x = left + hr * 4 * cw
        out.append(f'<text x="{x}" y="10" text-anchor="middle">{hr:02d} h</text>')
    for row, r in enumerate(days.itertuples()):
        y = 16 + row * (ch + gap)
        out.append(f'<text x="0" y="{y + 11}">{html.escape(day_name(r.date))}</text>')
        for s in slots[slots.day == r.day].itertuples():
            st = s.state
            if st == "observed" and s.value == 0:
                st = "zero"
            x = left + (s.slot % SLOTS_PER_DAY) * cw
            tip = f"{s.t:%H:%M} · {STATE_LABEL[st]}" + (f" · {fmt_int(s.value)} pasos" if s.state == "observed" else "")
            out.append(f'<rect x="{x}" y="{y}" width="{cw - 1}" height="{ch}" rx="1.5" fill="{COLORS[st]}">'
                       f'<title>{html.escape(tip)}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def render(exp, title: str, T: pd.Timestamp | None = None, note: str = "") -> str:
    s = summarize(exp, T)
    days = s.days
    rows = []
    for r in days.itertuples():
        compl = 100 * r.observed / r.slots
        extra = []
        if r.pending:
            extra.append(f"{r.pending * 15 / 60:.1f} h pendientes")
        if r.lost:
            extra.append(f"{r.lost * 15 / 60:.1f} h perdidas")
        if r.no_contact and r.status != "no_data":
            extra.append(f"{r.no_contact * 15 / 60:.1f} h sin conexión")
        steps = "—" if r.status in ("no_data", "aggregate") else (("≥ " if r.status in ("partial", "ongoing") else "") + fmt_int(r.steps))
        rows.append(f"<tr><td>{html.escape(day_name(r.date))}</td><td>{steps}</td><td>{compl:.0f} %</td>"
                    f'<td><span class="chip" style="background:{COLORS[r.status]}">{DAY_LABEL[r.status]}</span></td>'
                    f"<td>{html.escape(', '.join(extra))}</td></tr>")
    sess = []
    for r in s.sessions.itertuples() if not s.sessions.empty else []:
        dur = (r.end - r.start).total_seconds() / 3600
        rssi = "" if pd.isna(r.rssi_min) else f"{r.rssi_min:.0f} a {r.rssi_max:.0f} dBm"
        sess.append(f"<tr><td>{html.escape(day_name(r.start))} {r.start:%H:%M}</td><td>{dur:.1f} h</td>"
                    f"<td>{r.frames}</td><td>{r.bins}</td><td>{rssi}</td></tr>")
    legend = "".join(f'<span><span class="sw" style="background:{COLORS[k]}"></span>{v}</span>'
                     for k, v in STATE_LABEL.items())
    period = f"{day_name(days.date.iloc[0])} a {day_name(days.date.iloc[-1])}" if len(days) else ""
    generated = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Seguimiento de actividad</title>
<style>{CSS}</style></head><body><main>
<h1>{html.escape(title)}</h1>
<div class="sub">Periodo: {period} · Podómetro LoRaWAN · Actualizado el {generated}</div>
<div class="card summary">{html.escape(weekly_text(s))}</div>
<div class="card"><h2>Pasos por día</h2><div class="scroll">{bars_svg(days)}</div>
<div class="legend"><span><span class="sw" style="background:var(--obs)"></span>Completo</span>
<span><span class="sw" style="background:var(--pend)"></span>Parcial (≥: faltan datos)</span>
<span><span class="sw" style="background:var(--zero)"></span>Inactivo (0 pasos confirmados)</span>
<span><span class="sw" style="border:1px dashed var(--muted)"></span>Sin datos (no es 0 pasos)</span></div></div>
<div class="card"><h2>Calidad de los datos, por franjas de 15 minutos</h2><div class="scroll">{ribbon_svg(s.slots, days)}</div>
<div class="legend">{legend}</div></div>
<div class="card"><h2>Detalle por día</h2><div class="scroll"><table><tr><th>Día</th><th>Pasos</th><th>Datos</th><th>Estado</th>
<th>Observaciones</th></tr>{''.join(rows)}</table></div></div>
<div class="card"><h2>Conexiones del dispositivo</h2><div class="scroll"><table><tr><th>Inicio</th><th>Duración</th>
<th>Mensajes</th><th>Intervalos recibidos</th><th>Señal</th></tr>{''.join(sess) or '<tr><td colspan=5>Sin conexiones</td></tr>'}
</table></div></div>
<div class="sub">Calculado solo con los datos recibidos por la red. {html.escape(note)}</div>
</main></body></html>"""


def demo_experiment(scenario: str):
    from hijsim import SimConfig, simulate
    day = 86400
    base = dict(delta_s=900, period_s=3600, duration_s=7 * day, seed=3009, buf=1440)
    cfgs = {
        "C2": dict(scenario=2, cycle=day, window=7200, strategy=3, label="demo_C2_S3"),
        "C4": dict(scenario=4, out_start=int(1.4 * day), out_len=2 * day, strategy=3, label="demo_C4_S3"),
        "C2-S1": dict(scenario=2, cycle=day, window=7200, strategy=1, label="demo_C2_S1"),
    }
    return simulate(SimConfig(**base, **cfgs[scenario]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", nargs="?")
    ap.add_argument("--demo", choices=["C2", "C4", "C2-S1"])
    ap.add_argument("--out", type=pathlib.Path)
    args = ap.parse_args()
    if args.demo:
        exp = demo_experiment(args.demo)
        title, note = "Seguimiento de actividad · paciente P-001 (demostración)", "Datos simulados."
    else:
        from hij.experiment import load_experiment
        exp = load_experiment(RAW / args.experiment)
        title, note = "Seguimiento de actividad · paciente P-001", f"Experimento {args.experiment} (tiempo de laboratorio x{exp.compression:.0f})."
    out = args.out or (HERE.parent / "data" / "processed" / f"clinico_{args.demo or args.experiment}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(exp, title, note=note), encoding="utf-8")
    print(f"página escrita en {out}")


if __name__ == "__main__":
    main()
