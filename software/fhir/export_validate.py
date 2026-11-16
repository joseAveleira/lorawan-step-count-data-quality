"""Exportación FHIR R4 de las pruebas del laboratorio y validación con el validador oficial de HL7.

Para cada prueba terminada con bins (estrategias S1 a S4; la S0 no envía bins y no tiene intervalos que exportar) se
reproduce lo que publica el backend en tiempo real: tras cada trama recibida, en el orden de recepción, un Bundle de
tipo collection con las versiones nuevas o cambiadas de las Observation (intervalo nuevo, hueco que pasa a pendiente
o a perdido, dato que llega tarde), el Provenance de esa trama y el Patient y el Device a los que remiten. Cada
versión de un recurso se publica una sola vez, así que el recuento de recursos es el de versiones publicadas. Usa
hij.fhir.bundle sin cambios: el estado de cada intervalo en cada instante es el mismo que en el resto del análisis.

Después pasa todos los Bundle por el validador oficial de HL7 (validator_cli.jar) contra FHIR R4 (4.0.1), con el
servidor de terminología por defecto (tx.fhir.org) para comprobar los códigos de LOINC, UCUM y de FHIR. Solo se
envían códigos de terminología, no los recursos; los datos son de las pruebas del laboratorio (patrón sintético).

Salidas:
    software/data/processed/fhir/<prueba>/<nnnn>.json   Bundle publicado tras cada trama
    software/data/processed/fhir/validation_output.json salida del validador (OperationOutcome por fichero)
    software/data/processed/fhir_validation.json        recuentos y resumen de la validación
    software/data/processed/fhir_validation.md          lo mismo, para leer
    --examples <carpeta>: fhir_example_1.json y _2.json, un ejemplo de cada estado y un Provenance (para
                          consulta; se guardan en software/data/processed/fhir_examples)

Uso:
    python software/fhir/export_validate.py [ids...] [--no-validate] [--examples carpeta]
Por defecto, Java y el validador se buscan en lora_extension/herramientas/ (--java, --validator para otros).
"""
import argparse
import collections
import datetime as dt
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pandas as pd

SOFTWARE = pathlib.Path(__file__).resolve().parents[1]
ROOT = SOFTWARE.parent
RAW, PROCESSED = SOFTWARE / "data" / "raw", SOFTWARE / "data" / "processed"
OUT = PROCESSED / "fhir"
TOOLS = ROOT / "herramientas"

sys.path.insert(0, str(SOFTWARE / "backend"))
from hij import fhir  # noqa: E402
from hij.experiment import load_experiment  # noqa: E402
from hij.quality import received_copies  # noqa: E402


def feed(exp, pid: str):
    """Bundles que publica el backend tras cada trama recibida, con las versiones nuevas o cambiadas.

    Devuelve (bundles, versiones): versiones es una lista de (id de la Observation, estado, valor) por cada versión
    publicada, en orden, y los Provenance publicados se cuentan aparte en cada Bundle."""
    times = sorted(exp.uplinks.received_at.unique())
    last: dict[str, str] = {}       # id de la Observation -> JSON de la última versión publicada
    provs: set[str] = set()
    pending: set = set()            # (época, secuencia) publicados antes como pendientes
    bundles, versions = [], []
    for j, T in enumerate(times):
        T = pd.Timestamp(T)
        b, st = fhir.bundle(exp, T, published_pending=pending)
        state = {fhir._uid(pid, r.epoch, r.seq): (r.state, r.value) for r in st.itertuples()} if len(st) else {}
        pending |= {(r.epoch, r.seq) for r in st.itertuples() if r.state == "pending"} if len(st) else set()
        base, new = [], []
        for e in b["entry"]:
            r = e["resource"]
            kind = r["resourceType"]
            if kind in ("Patient", "Device"):
                base.append(e)
            elif kind == "Observation":
                s = json.dumps(r, sort_keys=True)
                if last.get(r["id"]) != s:
                    last[r["id"]] = s
                    new.append(e)
                    versions.append((r["id"],) + state[r["id"]])
            elif kind == "Provenance" and r["id"] not in provs:
                provs.add(r["id"])
                new.append(e)
        if new:
            bundles.append({"resourceType": "Bundle", "id": fhir._uid(pid, "feed", j), "type": "collection",
                            "timestamp": fhir._iso(T), "entry": base + new})
    return bundles, versions


def runs_with_bins(ids: list[str]) -> list[str]:
    """Pruebas de las estrategias del artículo con bins (S1 a S3; la S4 de comprobación no está en Methods)."""
    out = []
    for pid in ids:
        exp = load_experiment(RAW / pid)
        strategy = exp.config.get("strategy")
        if strategy is None and not exp.uplinks.empty:
            strategy = int(exp.uplinks.strategy.iloc[-1])
        if strategy in (1, 2, 3) and not received_copies(exp).empty:
            out.append(pid)
    return out


def find_java(arg: str | None) -> str:
    if arg:
        return arg
    found = sorted(TOOLS.glob("java/*/bin/java.exe")) + sorted(TOOLS.glob("java/*/bin/java"))
    if found:
        return str(found[0])
    return shutil.which("java") or "java"


def validate(java: str, jar: pathlib.Path, folders: list[pathlib.Path], output: pathlib.Path,
             log_name: str = "validator_console.log") -> dict:
    """Ejecuta el validador de HL7 sobre todos los ficheros de las carpetas (una sola JVM; se le pasan carpetas
    porque la lista de ficheros no cabe en una línea de órdenes de Windows) y devuelve su versión y su duración."""
    # -language en: los displays se comprueban en inglés, el idioma de los recursos (sin él, el validador toma el
    # idioma del sistema, aquí el español, y compara con el nombre de LOINC en español); -locale en: mensajes en inglés
    cmd = [java, "-Xmx3g", "-jar", str(jar), *map(str, folders), "-version", "4.0.1", "-language", "en",
           "-locale", "en", "-output", str(output), "-txCache", str(jar.parent / "txcache")]
    t0 = dt.datetime.now()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    secs = (dt.datetime.now() - t0).total_seconds()
    (OUT / log_name).write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    m = re.search(r"FHIR Validation tool Version ([^\s(]+)", proc.stdout)
    return {"returncode": proc.returncode, "seconds": round(secs, 1), "validator_version": m.group(1) if m else None}


def summarize_outcomes(output: pathlib.Path) -> dict:
    """Cuenta los mensajes del validador por gravedad y por texto (sin cifras ni identificadores)."""
    data = json.loads(output.read_text(encoding="utf-8"))
    outcomes = [e["resource"] for e in data.get("entry", [])] if data.get("resourceType") == "Bundle" else [data]
    sev = collections.Counter()
    msgs = {s: collections.Counter() for s in ("fatal", "error", "warning", "information")}
    files_with_errors = 0
    for oo in outcomes:
        has_error = False
        for i in oo.get("issue", []):
            s = i.get("severity", "information")
            text = i.get("details", {}).get("text") or i.get("diagnostics") or ""
            if s in ("fatal", "error", "warning") and "All OK" not in text:
                sev[s] += 1
                key = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", text)
                key = re.sub(r"\d+", "#", key)
                msgs[s][key] += 1
                has_error |= s in ("fatal", "error")
            elif s == "information":
                sev["information"] += 1
        files_with_errors += has_error
    return {"files": len(outcomes), "files_with_errors": files_with_errors, "issues": dict(sev),
            "messages": {s: c.most_common(20) for s, c in msgs.items() if c}}


def _dar(r: dict) -> str | None:
    return r.get("dataAbsentReason", {}).get("coding", [{}])[0].get("code")


def example_bundles(all_bundles: dict, pid: str = "C5-overflow_S3") -> tuple[list[dict], list[dict]]:
    """Ejemplos de una sola prueba (un Patient y un Device), para consulta: un intervalo observado
    con pasos, uno observado con 0 pasos, uno perdido y las dos versiones de un intervalo que se publicó como
    pendiente y llegó después (llegado tarde), con el Provenance de la trama que lo trajo. Cada Observation lleva
    meta.versionId (número de versión publicada). Van en dos Bundles, como se publican: el primero con los intervalos
    observados, el perdido y la versión pendiente, y el segundo con la versión que llega tarde y su Provenance (en
    un mismo Bundle, la referencia del Provenance sería ambigua entre las dos versiones)."""
    history: dict[str, list[dict]] = {}     # id -> versiones publicadas, en orden
    base, prov_of = {}, {}
    for b in all_bundles[pid]:
        for e in b["entry"]:
            r = e["resource"]
            if r["resourceType"] in ("Patient", "Device"):
                base.setdefault(r["resourceType"], e)
            elif r["resourceType"] == "Observation":
                h = history.setdefault(r["id"], [])
                h.append(dict(r, meta={"versionId": str(len(h) + 1)}))
            elif r["resourceType"] == "Provenance":
                for t in r["target"]:
                    prov_of.setdefault(t["reference"], r)   # Provenance de la primera versión con valor
    once = [h[0] for h in history.values() if len(h) == 1]
    positive = next(r for r in once if r.get("valueQuantity", {}).get("value", 0) > 0)
    zero = next(r for r in once if "valueQuantity" in r and r["valueQuantity"]["value"] == 0)
    lost = next(h[-1] for h in history.values() if _dar(h[-1]) == "unknown")
    late_id = next(i for i, h in history.items()
                   if any(_dar(v) == "temp-unknown" for v in h) and "valueQuantity" in h[-1])
    pending = next(v for v in history[late_id] if _dar(v) == "temp-unknown")
    late = history[late_id][-1]
    prov = dict(prov_of[f"urn:uuid:{late_id}"], target=[{"reference": f"urn:uuid:{late_id}"}])
    def wrap(rs):
        return [base["Patient"], base["Device"]] + [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in rs]
    return wrap([positive, zero, lost, pending]), wrap([late, prov])


def md_table(df: pd.DataFrame) -> list[str]:
    """Tabla en Markdown sin depender de tabulate."""
    cols = list(df.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    rows += ["| " + " | ".join(str(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--java")
    ap.add_argument("--validator", type=pathlib.Path, default=TOOLS / "validador_fhir" / "validator_cli.jar")
    ap.add_argument("--examples", type=pathlib.Path)
    args = ap.parse_args()
    if args.examples:
        args.examples = args.examples.resolve()   # el validador no acepta rutas con ".."
    ids = args.ids or sorted(p.parent.name for p in RAW.glob("*/DONE"))
    ids = runs_with_bins(ids)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    per_run, files, all_bundles = [], [], {}
    for pid in ids:
        exp = load_experiment(RAW / pid)
        bundles, versions = feed(exp, pid)
        all_bundles[pid] = bundles
        d = OUT / pid
        d.mkdir()
        for j, b in enumerate(bundles):
            f = d / f"{j:04d}.json"
            f.write_text(json.dumps(b, indent=1, ensure_ascii=False), encoding="utf-8")
            files.append(f)
        kinds = collections.Counter()
        for _, state, value in versions:
            if state in ("final", "late"):
                kinds["observed_zero" if int(value) == 0 else "observed_positive"] += 1
                if state == "late":
                    kinds["received_late"] += 1
            else:
                kinds[state] += 1
        ids_seen = {v[0] for v in versions}
        n_prov = sum(1 for b in bundles for e in b["entry"] if e["resource"]["resourceType"] == "Provenance")
        per_run.append({"id": pid, "strategy": int(exp.config.get("strategy", -1)), "bundles": len(bundles),
                        "intervals": len(ids_seen), "observation_versions": len(versions), "provenance": n_prov,
                        "patient_device": 2 * len(bundles), **kinds})
    df = pd.DataFrame(per_run).fillna(0)
    for c in df.columns:
        if c not in ("id",):
            df[c] = df[c].astype(int)
    totals = {c: int(df[c].sum()) for c in df.columns if c not in ("id", "strategy")}
    totals["resources"] = totals["observation_versions"] + totals["provenance"] + totals["patient_device"]
    summary = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "runs": ids, "per_run": per_run,
               "totals": totals}

    if not args.no_validate:
        output = OUT / "validation_output.json"
        info = validate(find_java(args.java), args.validator, [OUT / pid for pid in ids], output)
        summary["validator"] = info
        if output.exists():
            summary["validation"] = summarize_outcomes(output)
    (PROCESSED / "fhir_validation.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False),
                                                     encoding="utf-8")

    lines = ["# Exportación y validación FHIR", "",
             f"Generado por `software/fhir/export_validate.py` el {summary['generated']}.", "",
             f"{len(ids)} pruebas con bins; un Bundle tras cada trama recibida con las versiones nuevas o cambiadas.",
             "", *md_table(df), "", "Totales: " + ", ".join(f"{k} {v}" for k, v in totals.items()), ""]
    if "validator" in summary:
        v = summary["validator"]
        lines += [f"Validador de HL7 {v['validator_version']} contra FHIR R4 (4.0.1): código de salida "
                  f"{v['returncode']}, {v['seconds']} s.", ""]
        if "validation" in summary:
            val = summary["validation"]
            lines += [f"Ficheros validados: {val['files']}; con errores: {val['files_with_errors']}; mensajes: "
                      f"{val['issues']}", ""]
            for s, items in val["messages"].items():
                lines.append(f"## {s}")
                lines += [f"- {n} x {m}" for m, n in items]
                lines.append("")
    (PROCESSED / "fhir_validation.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

    if args.examples:
        args.examples.mkdir(parents=True, exist_ok=True)
        files = []
        for k, entries in enumerate(example_bundles(all_bundles), start=1):
            stamps = [e["resource"].get("recorded") or e["resource"].get("issued") for e in entries]
            ex = {"resourceType": "Bundle", "id": fhir._uid("examples", k), "type": "collection",
                  "timestamp": max(t for t in stamps if t), "entry": entries}
            f = args.examples / f"fhir_example_{k}.json"
            f.write_text(json.dumps(ex, indent=2, ensure_ascii=False), encoding="utf-8")
            files.append(f)
            print(f"Ejemplo {k}: {f} ({len(entries)} recursos)")
        if not args.no_validate:
            out_ex = OUT / "examples_validation.json"
            validate(find_java(args.java), args.validator, files, out_ex, "validator_examples.log")
            if out_ex.exists():
                summary["examples_validation"] = summarize_outcomes(out_ex)
                (PROCESSED / "fhir_validation.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False),
                                                                 encoding="utf-8")
                print(f"Validación de los ejemplos: {summary['examples_validation']}")

if __name__ == "__main__":
    main()
