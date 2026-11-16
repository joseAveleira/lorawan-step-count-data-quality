"""Copia periódicamente los paquetes que registra el gateway Dragino (tabla livepkts de /tmp/lgwdb.sqlite).

El gateway solo conserva los últimos 128 registros, así que hay que leerlos con frecuencia. Cada paquete se
guarda en <salida>/gateway.jsonl como {"host_time", "row"}; la columna content trae RSSI, SNR, DevAddr,
FCnt, FPort y MIC. Cada uplink aparece dos veces en la tabla (una por servidor de reenvío); aquí se guardan
todas las filas y la deduplicación se hace en el análisis.

Acceso por SSH al gateway con clave; se configura en software/backend/.env:
    GW_SSH_TARGET=root@gateway.local
    GW_SSH_KEY=C:/Users/<usuario>/.ssh/id_ed25519_dragino
o, si no hay clave instalada, con un programa askpass:
    GW_SSH_ASKPASS=C:/ruta/askpass.sh

Uso independiente:
    python backend/collect_gateway.py <carpeta_salida> [--interval 60]
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import threading

ENV_FILE = pathlib.Path(__file__).resolve().parent / ".env"
QUERY = ("SELECT id,time,servname,pdtype,freq,dr,cnt,devaddr,content,payload "
         "FROM livepkts WHERE id > {last} ORDER BY id;")


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


class GatewayCollector:
    def __init__(self, out_dir: pathlib.Path, interval: float = 60, log=print):
        cfg = load_env()
        self.target = cfg.get("GW_SSH_TARGET", "root@gateway.local")
        self.ssh_opts = ["-o", "ConnectTimeout=10"]
        if cfg.get("GW_SSH_KEY"):
            self.ssh_opts += ["-i", cfg["GW_SSH_KEY"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
        self.ssh_env = dict(os.environ)
        if cfg.get("GW_SSH_ASKPASS"):
            self.ssh_env.update(SSH_ASKPASS=cfg["GW_SSH_ASKPASS"], SSH_ASKPASS_REQUIRE="force", DISPLAY="none")
        self.interval = interval
        self.log = log
        self.last_id = 0
        self.stop_event = threading.Event()
        # sin out_dir solo sirve para tocar el filtro (p. ej., quitar un corte real que se quedó puesto)
        self.out = open(out_dir / "gateway.jsonl", "a", encoding="utf-8") if out_dir else None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def ssh(self, command: str, timeout: float = 30) -> str:
        r = subprocess.run(["ssh", *self.ssh_opts, self.target, command],
                           capture_output=True, text=True, timeout=timeout, env=self.ssh_env)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:200])
        return r.stdout

    # ------------------------------------------------------------------
    # Corte real y selectivo: el gateway descarta los uplinks de un DevAddr (filtro de nivel 1, lista
    # negra) y sigue conectado a TTN. Se guarda igual que en la web del gateway (uci y tabla filter), se
    # regenera /etc/lora/local_conf.json y se reinicia el forwarder, que lee el nivel al arrancar. El
    # reinicio corta unos segundos la recepción: hay que hacerlo entre dos envíos del nodo.
    # (No usar reload_iot_server.sh: solo para el forwarder y nadie lo vuelve a arrancar.)
    # ------------------------------------------------------------------
    FILTER_DB = "/etc/lora/devskey"
    LEVEL_LOG = "primary_server] packets received with a devaddr filter, level"

    def set_devaddr_block(self, devaddr: str, block: bool):
        devaddr = devaddr.upper()
        assert len(devaddr) == 8 and all(c in "0123456789ABCDEF" for c in devaddr)
        where = f"name='primary_server' AND type='devaddr' AND value='{devaddr}'"
        if block:
            row = (f"sqlite3 {self.FILTER_DB} \"INSERT INTO filter (name, type, value) "
                   f"SELECT 'primary_server','devaddr','{devaddr}' "
                   f"WHERE NOT EXISTS (SELECT 1 FROM filter WHERE {where})\"")
            level = 1
        else:
            row = f"sqlite3 {self.FILTER_DB} \"DELETE FROM filter WHERE {where}\""
            level = 0
        try:
            out = self.ssh(
                f"{row} && uci set gateway.server1.devaddr_filter_level={level} && uci commit gateway "
                f"&& /usr/local/dragino/generate-config.sh start >/dev/null 2>&1; "
                f"systemctl restart draginofwd; sleep 8; systemctl is-active draginofwd; "
                f"journalctl -u draginofwd --since '-12 sec' --no-pager | grep -o '{self.LEVEL_LOG}([0-9])' | tail -1",
                timeout=60)
        finally:
            # pase lo que pase, el forwarder no se queda parado
            self.ssh("systemctl is-active --quiet draginofwd || systemctl start draginofwd")
        active = "active" in out.split()
        applied = f"level({level})" in out
        self.log(f"# gateway: filtro DevAddr {devaddr} {'activado (corte real)' if block else 'quitado'}: "
                 f"forwarder {'activo' if active else 'PARADO'}, nivel {'aplicado' if applied else 'NO CONFIRMADO'}")
        if not (active and applied):
            raise RuntimeError(f"el gateway no ha confirmado el filtro: {out.strip()[-200:]}")

    def devaddr_blocked(self, devaddr: str) -> bool:
        out = self.ssh(f"sqlite3 {self.FILTER_DB} \"SELECT count(*) FROM filter WHERE name='primary_server' "
                       f"AND type='devaddr' AND value='{devaddr.upper()}'\"; "
                       f"uci -q get gateway.server1.devaddr_filter_level")
        count, _, level = out.strip().partition("\n")
        return count.strip() != "0" and level.strip() == "1"

    def poll(self) -> int:
        out = self.ssh(f'sqlite3 -json /tmp/lgwdb.sqlite "{QUERY.format(last=self.last_id)}"')
        rows = json.loads(out) if out.strip() else []
        if rows and rows[0]["id"] < self.last_id:
            self.last_id = 0  # el gateway se ha reiniciado y la tabla ha vuelto a empezar
        host_time = dt.datetime.now(dt.timezone.utc).isoformat()
        for row in rows:
            self.out.write(json.dumps({"host_time": host_time, "row": row}) + "\n")
            self.last_id = max(self.last_id, row["id"])
        self.out.flush()
        return len(rows)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                n = self.poll()
                if n:
                    self.log(f"# gateway: {n} registros nuevos (último id {self.last_id})")
            except Exception as e:  # noqa: BLE001 - se registra y se reintenta en la siguiente vuelta
                self.log(f"# gateway: error al leer: {e}")
            self.stop_event.wait(self.interval)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        self.out.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=pathlib.Path)
    ap.add_argument("--interval", type=float, default=60)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    c = GatewayCollector(args.out_dir, args.interval)
    c.start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        c.stop()


if __name__ == "__main__":
    main()
