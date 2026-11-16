"""Ejecuta un experimento: configura el nodo, registra las tres fuentes y aplica las acciones del plan.

Fuentes que se guardan en software/data/raw/<id>/:
    node.log        líneas del nodo por serie (GT, TX, EV) con la marca de tiempo del PC
    ttn.jsonl       mensajes de TTN por MQTT (si existe backend/.env)
    gateway.jsonl   paquetes registrados por el gateway (si hay acceso SSH)
    plan.json       copia del plan

El script es el único dueño del puerto serie durante todo el experimento (en este PC, reabrir el puerto
después de un rato cerrado reinicia la placa). Para mandar comandos al nodo durante el experimento basta con
añadir líneas a software/data/raw/<id>/inbox.txt.

Plan (JSON):
    {
      "id": "c1_s1_r1",
      "duration_s": 7200,
      "setup": ["set strategy 1", "set scenario 1", "set period 240"],
      "actions": [{"at": 3600, "node": "reboot"}],
      "teardown": ["set period 240"],
      "ack_every": 0
    }

Uso:
    python experiments/run.py experiments/plans/c1_s1.json [--port COM5] [--no-ttn] [--no-gateway]
"""
import argparse
import datetime as dt
import json
import pathlib
import shutil
import sys
import time

import serial

SOFTWARE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOFTWARE / "backend"))
RAW = SOFTWARE / "data" / "raw"
GW_MARK = RAW / "_gw_block.txt"  # DevAddr con el corte real puesto; si run.py muere, lo quita campaign.py


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def buffer_bins(plan: dict) -> int | None:
    """Capacidad B del buffer que fija el plan (set buf N): el backend la usa para saber qué bins ya no existen."""
    values = [int(c.split()[2]) for c in plan.get("setup", []) if c.split()[:2] == ["set", "buf"]]
    return values[-1] if values else None


class Node:
    """Puerto serie del nodo: registra cada línea y permite esperar a una línea concreta."""

    def __init__(self, port: str, log_path: pathlib.Path, echo):
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = port, 115200, 0.1
        if sys.platform == "win32":
            self.ser.dtr = False  # con DTR/RTS en reposo no se pulsa el reset ni el botón PRG
            self.ser.rts = False
            self.ser.open()
        else:
            # En Linux el núcleo activa DTR y RTS al abrir. Soltar DTR antes que RTS pulsaría EN (reinicio), así que
            # se abre con los dos activos y se suelta primero RTS (solo baja IO0 un instante, sin efecto con el
            # programa en marcha) y después DTR.
            self.ser.dtr = True
            self.ser.rts = True
            self.ser.open()
            self.ser.rts = False
            self.ser.dtr = False
        self.log = open(log_path, "a", encoding="utf-8")
        self.echo = echo
        self.buf = b""
        self.last_line = time.time()

    def hard_reset(self):
        """Reinicio por la línea RTS (EN): último recurso si el nodo deja de responder."""
        self.write_log("# hard_reset RTS")
        self.ser.rts = True
        time.sleep(0.2)
        self.ser.rts = False

    def write_log(self, line: str):
        self.log.write(f"{now_iso()}\t{line}\n")
        self.log.flush()

    def send(self, cmd: str):
        self.ser.write((cmd + "\n").encode())
        self.write_log(f"# cmd {cmd}")
        self.echo(f">> {cmd}")
        time.sleep(0.15)

    def pump(self) -> list[str]:
        self.buf += self.ser.read(2048)
        lines = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if line:
                self.write_log(line)
                lines.append(line)
                self.last_line = time.time()
                if line.startswith(("EV,", "TX,")):
                    self.echo(line)
        return lines

    def wait_for(self, prefix: str, timeout: float) -> str | None:
        t_end = time.time() + timeout
        while time.time() < t_end:
            for line in self.pump():
                if line.startswith(prefix):
                    return line
        return None

    def close(self):
        self.ser.close()
        self.log.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=pathlib.Path)
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--no-ttn", action="store_true")
    ap.add_argument("--no-gateway", action="store_true")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    out = RAW / plan["id"]
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(args.plan, out / "plan.json")
    runner_log = open(out / "runner.log", "a", encoding="utf-8")

    def echo(msg: str):
        line = f"{now_iso()[11:19]}  {msg}"
        print(line, flush=True)
        runner_log.write(line + "\n")
        runner_log.flush()

    node = Node(args.port, out / "node.log", echo)
    collectors = []
    ttn = None
    gateway = None
    blocked = None  # DevAddr bloqueado en el gateway, para quitar el filtro pase lo que pase
    try:
        # Configuración y reinicio (abre una época nueva y aplica delta, seed, source y buf). El buffer se
        # vacía después del reinicio: así no se cuela ningún bin cerrado entre el vaciado y el reinicio.
        # Si el nodo no contesta (apagado con el botón o colgado), se reinicia por RTS, que siempre lo arranca,
        # y se repite la configuración, que no le habrá llegado.
        for attempt in range(2):
            if attempt:
                echo("! el nodo no contesta: reinicio por RTS y se repite la configuración")
                node.hard_reset()
                node.wait_for("EV,boot", 15)
                node.wait_for("EV,join", 30)
            for cmd in plan.get("setup", []):
                node.send(cmd)
            node.send("reboot")
            if node.wait_for("EV,boot", 15):
                break
        else:
            echo("! el nodo no ha arrancado tras el reinicio")
            return 1
        node.wait_for("EV,join", 30)
        node.send("clear")
        node.send("exp")
        if not node.wait_for("EV,exp,start", 5):
            echo("! el nodo no ha confirmado el inicio del experimento")
            return 1
        t0 = time.time()

        if not args.no_ttn and (SOFTWARE / "backend" / ".env").exists():
            from collect_ttn import TtnCollector
            ttn = TtnCollector(out, plan.get("ack_every", 0), log=echo, buffer_bins=buffer_bins(plan))
            ttn.start()
            collectors.append(ttn)
        if not args.no_gateway:
            from collect_gateway import GatewayCollector
            gateway = GatewayCollector(out, 60, log=echo)
            gateway.start()
            collectors.append(gateway)

        actions = sorted(plan.get("actions", []), key=lambda a: a["at"])
        inbox = out / "inbox.txt"
        inbox.touch()
        inbox_pos = inbox.stat().st_size
        duration = plan["duration_s"]
        echo(f"experimento {plan['id']} en marcha: {duration} s, {len(actions)} acciones")
        silence_limit = max(3 * 240, 600)  # el nodo imprime al menos una línea GT por bin (60 s)
        while time.time() - t0 < duration:
            node.pump()
            if time.time() - node.last_line > silence_limit:
                echo("! el nodo lleva más de 10 min sin dar señales: reinicio por RTS")
                node.hard_reset()
                node.last_line = time.time()
            elapsed = time.time() - t0
            while actions and actions[0]["at"] <= elapsed:
                a = actions.pop(0)
                if "node" in a:
                    node.send(a["node"])
                if a.get("gw") in ("block", "unblock") and gateway:
                    # el DevAddr que ve TTN ahora mismo (si el nodo ha hecho un join nuevo, ha cambiado)
                    devaddr = blocked if a["gw"] == "unblock" and blocked else ((ttn and ttn.dev_addr) or plan["devaddr"])
                    node.write_log(f"# gw {a['gw']} {devaddr}")
                    if a["gw"] == "block":
                        # se apunta antes de bloquear: si falla a medias, el finally lo quita igualmente
                        blocked = devaddr
                        GW_MARK.write_text(devaddr, encoding="utf-8")
                        gateway.set_devaddr_block(devaddr, True)
                    else:
                        gateway.set_devaddr_block(devaddr, False)
                        blocked = None
                        GW_MARK.unlink(missing_ok=True)
                if "note" in a:
                    node.write_log(f"# note {a['note']}")
                    echo(f"nota: {a['note']}")
            if inbox.stat().st_size > inbox_pos:
                with open(inbox, encoding="utf-8") as f:
                    f.seek(inbox_pos)
                    for cmd in f.read().splitlines():
                        if cmd.strip():
                            node.send(cmd.strip())
                    inbox_pos = f.tell()
        node.send("dump")
        node.wait_for("EV,status", 5)
        for cmd in plan.get("teardown", []):
            node.send(cmd)
            node.pump()
        echo(f"experimento {plan['id']} terminado")
        return 0
    except KeyboardInterrupt:
        echo("interrumpido")
        return 130
    finally:
        if blocked and gateway:
            try:
                gateway.set_devaddr_block(blocked, False)
                GW_MARK.unlink(missing_ok=True)
                node.write_log(f"# gw unblock {blocked} (al terminar)")
            except Exception as e:  # noqa: BLE001
                echo(f"! no se ha podido quitar el filtro del gateway: {e}")
        for c in collectors:
            c.stop()
        node.close()
        runner_log.close()


if __name__ == "__main__":
    sys.exit(main())
