"""Ejecuta la campaña de experimentos en el orden de experiments/plans/campaign/order.txt.

- Cada experimento se lanza en un proceso aparte (experiments/run.py); si falla, se pasa al siguiente.
- Los terminados quedan marcados con software/data/raw/<id>/DONE, así que la campaña se puede reanudar.
  Los restos de un intento que no terminó se apartan (<id>__incompleto_<fecha>) para no mezclar datos.
- Para pararla al acabar el experimento en curso: crear el fichero experiments/STOP. Con Ctrl+C se para ya
  (el experimento en curso quita antes el corte del gateway, si lo había).
- Si un experimento murió con el corte real puesto en el gateway, se quita antes del siguiente.
- Busca el nodo por su USB (CP2102) aunque Windows le dé otro COM; si no está enchufado, avisa y no empieza.
  Si una prueba falla nada más empezar (nodo, USB o TTN), la campaña se para en vez de quemar las demás.
- Mientras corre, Windows no entra en suspensión (SetThreadExecutionState; deja de actuar al terminar).
- Mantiene una página de estado sencilla en data/raw/_estado_campana.html (se recarga sola cada minuto).

- Con --hasta HH:MM (experiments\\lanzar_campana_noche.bat) solo empieza pruebas que acaben antes de esa hora del
  siguiente día laborable, eligiendo en el orden de la campaña las que caben, y se para sola: Jose se lleva el PC.

Uso (en una ventana propia, no como tarea de Claude Code), o doble clic en experiments\\lanzar_campana.bat:
    .venv\\Scripts\\python.exe experiments\\campaign.py [--tier 1] [--only C1_S0,C1_S1] [--status-only] [--hasta 07:45]
"""
import argparse
import ctypes
import datetime as dt
import html
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
SOFTWARE = HERE.parent
PLANS = HERE / "plans" / "campaign"
RAW = SOFTWARE / "data" / "raw"
STOP = HERE / "STOP"
LOG = RAW / "_campaign.log"
STATUS = RAW / "_estado_campana.html"
GW_MARK = RAW / "_gw_block.txt"  # lo escribe run.py mientras el corte real está puesto en el gateway
LOCK = RAW / "_campaign.lock"    # bloqueado mientras hay una campaña en marcha
FIELD_ACTIVE = RAW / "_campo_activo.txt"  # prueba de campo sin recoger (lo escribe field.py salida, lo borra llegada)
OVERHEAD_S = 90                 # reinicio del nodo, join y pausa entre experimentos
NODE_USB = (0x10C4, 0xEA60)      # CP2102 del Heltec V3 (COM18 es otro Espressif y no se toca)
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
TIERS = {1: "Imprescindibles", 2: "Opcionales"}
UNTIL: dt.datetime | None = None  # con --hasta: no se empieza ninguna prueba que no acabe antes de esta hora


def when(t: dt.datetime) -> str:
    return f"{DIAS[t.weekday()]} {t:%d/%m} a las {t:%H:%M}"


def single_instance():
    """Fichero bloqueado mientras dure la campaña (el sistema lo suelta al cerrarse el proceso); None si ya lo está.
    En Windows con msvcrt y en la Raspberry (Linux) con flock."""
    f = open(LOCK, "a+")
    f.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def next_departure(hhmm: str, now: dt.datetime) -> dt.datetime:
    """La próxima vez que sean las hh:mm de un día laborable (el fin de semana el PC se queda en casa)."""
    h, m = (int(x) for x in hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    while t.weekday() >= 5:
        t += dt.timedelta(days=1)
    return t


def write_status(plans: list, state: dict, t_running: float | None = None, banner: str = ""):
    """Página de estado: pruebas hechas, la que está en marcha y cuándo acaba cada bloque."""
    now = dt.datetime.now()
    left_s, ends, running, left_tier = 0.0, {}, None, {}
    for p in plans:  # en el orden de la campaña
        st, rem = state.get(p["id"]), 0.0
        if st == "running":
            running = p
            rem = max(p["duration_s"] - (time.time() - t_running), 0)
        elif st is None:
            rem = p["duration_s"] + OVERHEAD_S
        left_s += rem
        left_tier[p.get("tier", 1)] = left_tier.get(p.get("tier", 1), 0) + rem
        ends[p.get("tier", 1)] = now + dt.timedelta(seconds=left_s)
    done = sum(state.get(p["id"]) == "done" for p in plans)
    blocks = ""
    for tier, name in TIERS.items():
        ps = [p for p in plans if p.get("tier", 1) == tier]
        if ps:
            d = sum(state.get(p["id"]) == "done" for p in ps)
            left = any(state.get(p["id"]) in (None, "running") for p in ps)
            if not left:
                fin = "terminadas"
            elif UNTIL:  # con pausas cada día la fecha final depende de cuándo vuelva el PC
                fin = f"quedan unas <b>{left_tier[tier] / 3600:.0f} h</b> de pruebas"
            else:
                fin = f"terminan hacia el <b>{when(ends[tier])}</b>"
            blocks += f"<p>{name}: <b>{d} de {len(ps)}</b> hechas · {fin}</p>\n"
    box = f"<p class='box warn'>{html.escape(banner)}</p>\n" if banner else ""
    if UNTIL and not banner:
        box += f"<p class='box'>Esta tanda se para sola antes del <b>{when(UNTIL)}</b>, para que te puedas llevar el PC.</p>\n"
    if running:
        fin = dt.datetime.fromtimestamp(t_running + running["duration_s"])
        box += (f"<p class='box'>▶ Ahora: <b>{html.escape(running['description'])}</b> "
                f"<span class='id'>{running['id']}</span> · termina el {when(fin)}</p>\n")
    icon = {"done": "✔", "running": "▶", "failed": "✗", None: "·"}
    rows = "".join(
        f"<tr class='{state.get(p['id']) or 'pending'}'><td>{icon[state.get(p['id'])]}</td>"
        f"<td>{html.escape(p['description'])} <span class='id'>{p['id']}</span></td>"
        f"<td>{p['duration_s'] / 3600:.1f} h</td>"
        f"<td>{'imprescindible' if p.get('tier', 1) == 1 else 'opcional'}</td></tr>\n"
        for p in plans)
    pct = 100 * done / max(len(plans), 1)
    STATUS.write_text(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Estado de la campaña</title><style>
body {{ font:16px/1.5 system-ui, sans-serif; margin:0; background:#f7f8fa; color:#1d2430; }}
main {{ max-width:820px; margin:0 auto; padding:24px 16px; }} h1 {{ font-size:22px; margin:0 0 12px; }}
.big {{ font-size:28px; font-weight:700; }} .muted {{ color:#5d6878; font-size:14px; }}
.bar {{ height:14px; background:#e3e7ee; border-radius:7px; overflow:hidden; margin:10px 0 18px; }}
.bar div {{ height:100%; background:#2f9e62; width:{pct:.0f}%; }}
.box {{ background:#fff; border:1px solid #e3e7ee; border-radius:10px; padding:10px 14px; }}
.warn {{ border-color:#e0a100; background:#fff8e1; }} .id {{ color:#9aa5b4; font-size:12px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; margin-top:16px; }}
td {{ padding:5px 6px; border-bottom:1px solid #e3e7ee; }}
tr.done td {{ color:#2f9e62; }} tr.running td {{ font-weight:700; }} tr.failed td {{ color:#d1495b; }}
</style></head><body><main>
<h1>Estado de la campaña de experimentos</h1>
<div class="big">{done} de {len(plans)} pruebas hechas</div>
<div class="bar"><div></div></div>
{blocks}{box}<p class="muted">Deja el PC encendido y enchufado, el nodo conectado por USB y el gateway
encendido. Actualizado el {when(now)}.</p>
<table>
{rows}</table></main></body></html>
""", encoding="utf-8")


def log(msg: str):
    line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def keep_awake(on: bool):
    if sys.platform == "win32":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def find_node_port() -> str | None:
    """Puerto del nodo, sin abrirlo. Windows le puede dar otro COM si se enchufa en otro USB."""
    from serial.tools import list_ports
    ports = [p.device for p in list_ports.comports() if (p.vid, p.pid) == NODE_USB]
    return "COM5" if "COM5" in ports else (ports[0] if ports else None)


def clear_gateway_leftover():
    """Si un experimento murió con el corte real puesto (ventana cerrada, reinicio de Windows...), lo quita."""
    if not GW_MARK.exists():
        return
    devaddr = GW_MARK.read_text(encoding="utf-8").strip()
    log(f"el gateway seguía con el corte real de {devaddr} de un experimento interrumpido: se quita")
    try:
        sys.path.insert(0, str(SOFTWARE / "backend"))
        from collect_gateway import GatewayCollector
        GatewayCollector(None, log=log).set_devaddr_block(devaddr, False)
        GW_MARK.unlink()
    except Exception as e:  # noqa: BLE001 - se avisa y se sigue; antes del siguiente se vuelve a intentar
        log(f"! no se ha podido quitar el corte del gateway: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", type=int, default=2, help="ejecutar hasta esta prioridad (1 o 2)")
    ap.add_argument("--only", default="", help="lista de ids separados por comas")
    ap.add_argument("--status-only", action="store_true", help="solo escribir la página de estado")
    ap.add_argument("--hasta", metavar="HH:MM",
                    help="no empezar pruebas que no acaben antes de esa hora del siguiente día laborable")
    args = ap.parse_args()
    lock = single_instance()  # un doble clic de más no debe tocar la página de estado ni el experimento en curso
    if lock is None:
        print("La campaña ya está en marcha en otra ventana: no hace falta lanzarla otra vez.")
        return
    global UNTIL
    UNTIL = next_departure(args.hasta, dt.datetime.now()) if args.hasta else None

    order = [l.strip() for l in (PLANS / "order.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    plans, state = [], {}
    for pid in order:
        plan = json.loads((PLANS / f"{pid}.json").read_text(encoding="utf-8"))
        if plan.get("tier", 1) > args.tier or (only and pid not in only):
            continue
        plans.append(plan)
        if (RAW / pid / "DONE").exists():
            state[pid] = "done"
    if args.status_only:
        write_status(plans, state, banner="La campaña no está en marcha.")
        print(f"página de estado: {STATUS}")
        return
    if FIELD_ACTIVE.exists():  # cada prueba empieza borrando la memoria del nodo: se perdería el día de campo
        msg = ("Hay una prueba de campo sin recoger: la campaña no se lanza, porque borraría lo que ha contado el "
               "nodo. Enchufa el nodo y haz doble clic en experiments\\llegada_campo.bat (recoge el día y después "
               "lanza la campaña sola).")
        log(msg)
        write_status(plans, state, banner=msg)
        return
    todo = [p for p in plans if p["id"] not in state]
    hours = sum(p["duration_s"] for p in todo) / 3600
    log(f"campaña: {len(todo)} experimentos pendientes, {hours:.1f} h de laboratorio")
    port = find_node_port()
    write_status(plans, state, banner="" if port else "No encuentro el nodo en el USB. Enchúfalo y vuelve a lanzar la campaña.")
    try:
        os.startfile(STATUS)  # la página se queda abierta en el navegador y se actualiza sola
    except (AttributeError, OSError):
        pass
    if port is None:
        log("no encuentro el nodo en el USB: enchúfalo y vuelve a lanzar la campaña")
        return
    if port != "COM5":
        log(f"el nodo está en {port}")
    if UNTIL:
        log(f"solo se empiezan pruebas que acaben antes del {when(UNTIL)}")
    keep_awake(True)
    proc, banner = None, "Campaña terminada."
    pending = list(todo)
    try:
        for i in range(1, len(todo) + 1):
            if STOP.exists():
                log("parada solicitada (experiments/STOP)")
                banner = "Campaña parada (fichero experiments/STOP). Para seguir: borrar STOP y volver a lanzarla."
                break
            # la primera en el orden de la campaña que acabe antes de la hora de salida (sin --hasta, la siguiente)
            plan = next((p for p in pending if UNTIL is None or dt.datetime.now()
                         + dt.timedelta(seconds=p["duration_s"] + OVERHEAD_S) <= UNTIL), None)
            if plan is None:
                log(f"no da tiempo a otra prueba antes del {when(UNTIL)}: se para la campaña")
                banner = (f"Parada sola: no daba tiempo a otra prueba antes del {when(UNTIL)}. "
                          "Al volver: enchufa el nodo y doble clic en llegada_campo.bat.")
                break
            pending.remove(plan)
            clear_gateway_leftover()
            pid = plan["id"]
            out = RAW / pid
            if out.exists():  # restos de un intento anterior que no terminó: se apartan para no mezclar datos
                out.rename(RAW / f"{pid}__incompleto_{dt.datetime.now():%Y%m%d_%H%M%S}")
            log(f"[{i}/{len(todo)}] {pid}: {plan['description']} ({plan['duration_s'] / 3600:.1f} h)")
            t0 = time.time()
            state[pid] = "running"
            write_status(plans, state, t0)
            proc = subprocess.Popen([sys.executable, str(HERE / "run.py"), str(PLANS / f"{pid}.json"), "--port", port],
                                    cwd=SOFTWARE)
            while True:
                try:
                    rc = proc.wait(timeout=60)
                    break
                except subprocess.TimeoutExpired:
                    write_status(plans, state, t0)  # la página se refresca cada minuto
            proc = None
            mins = (time.time() - t0) / 60
            if rc == 0:
                (out / "DONE").write_text(dt.datetime.now(dt.timezone.utc).isoformat(), encoding="utf-8")
                state[pid] = "done"
                log(f"[{i}/{len(todo)}] {pid}: terminado en {mins:.0f} min")
            else:
                state[pid] = "failed"
                if mins < 5:  # falla nada más empezar: es el montaje (nodo, USB, TTN), no la prueba; no se quema el resto
                    log(f"[{i}/{len(todo)}] {pid}: FALLO (código {rc}) al empezar; se para la campaña")
                    banner = ("Campaña parada: la prueba ha fallado nada más empezar. "
                              "Comprueba que el nodo está enchufado y vuelve a lanzar la campaña.")
                    break
                log(f"[{i}/{len(todo)}] {pid}: FALLO (código {rc}) tras {mins:.0f} min; se sigue con el siguiente")
            write_status(plans, state)
            time.sleep(30)
    except KeyboardInterrupt:
        log("campaña interrumpida: se espera a que el experimento en curso cierre")
        bat = "lanzar_campana_noche.bat" if UNTIL else "lanzar_campana.bat"
        banner = f"Campaña parada. Para seguir donde se quedó: doble clic en experiments\\{bat}."
        if proc is not None:
            try:
                proc.wait(timeout=120)  # run.py también recibe el Ctrl+C y quita el corte del gateway si lo había
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                proc.kill()
    finally:
        keep_awake(False)
        state = {k: v for k, v in state.items() if v != "running"}
        if banner == "Campaña terminada." and "failed" in state.values():
            banner = "Campaña terminada. Las pruebas con ✗ fallaron: se repiten volviendo a lanzar la campaña."
        write_status(plans, state, banner=banner)
    log("campaña terminada")


if __name__ == "__main__":
    main()
