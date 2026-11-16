"""Recoge el tráfico del nodo en TTN por MQTT y responde a la S3 con downlinks de ACK.

Guarda cada mensaje en <salida>/ttn.jsonl como {"host_time", "topic", "msg"}. Las credenciales se leen de
software/backend/.env (fuera de git):
    TTN_APP_ID=lorawan-app-iot
    TTN_DEVICE_ID=lorapasos
    TTN_API_KEY=NNSXS....
    TTN_HOST=eu1.cloud.thethings.network

Uso independiente:
    python backend/collect_ttn.py <carpeta_salida> [--ack-every 40]
"""
import argparse
import base64
import datetime as dt
import json
import pathlib
import sys
import threading

import paho.mqtt.client as mqtt

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from hij.ack import AckTracker  # noqa: E402
from hij.payload import FPORT_ACK, FPORT_BASE, decode, encode_ack  # noqa: E402

ENV_FILE = pathlib.Path(__file__).resolve().parent / ".env"
BUDGET_FILE = pathlib.Path(__file__).resolve().parents[1] / "data" / "raw" / "_downlinks.json"


class DownlinkBudget:
    """Límite de downlinks de la fair use policy de TTN (10 al día por dispositivo), compartido entre
    experimentos: se guardan en disco los instantes de los downlinks de las últimas 24 h."""

    def __init__(self, per_day: int = 10, path: pathlib.Path = BUDGET_FILE):
        self.per_day, self.path = per_day, path
        self.times = json.loads(path.read_text()) if path.exists() else []

    def _recent(self, now: float) -> list[float]:
        return [t for t in self.times if now - t < 86400]

    def available(self) -> int:
        return self.per_day - len(self._recent(dt.datetime.now().timestamp()))

    def take(self) -> bool:
        now = dt.datetime.now().timestamp()
        self.times = self._recent(now)
        if len(self.times) >= self.per_day:
            return False
        self.times.append(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.times))
        return True


def load_env() -> dict:
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class TtnCollector:
    def __init__(self, out_dir: pathlib.Path, ack_every: int = 0, log=print, buffer_bins: int | None = None):
        env = load_env()
        self.app = env["TTN_APP_ID"]
        self.dev = env.get("TTN_DEVICE_ID", "+")
        self.host = env.get("TTN_HOST", "eu1.cloud.thethings.network")
        self.ack_every = ack_every       # 0 = sin ACK
        self.log = log
        self.tracker = AckTracker(buffer_bins)  # con B del nodo: bins perdidos solo si ya no caben en su buffer
        self.budget = DownlinkBudget()
        self.s3_since_ack = 0
        self.last_ack = None
        self.downlinks = 0
        self.dev_addr = None  # DevAddr actual del nodo (cambia si hace un join nuevo)
        self.lock = threading.Lock()
        self.out = open(out_dir / "ttn.jsonl", "a", encoding="utf-8")
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"hij-collector-{self.dev}")
        self.client.username_pw_set(f"{self.app}@ttn", env["TTN_API_KEY"])
        self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = lambda *a: self.log("# TTN: desconectado")

    def start(self):
        self.client.connect(self.host, 8883, keepalive=60)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()
        self.out.close()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        topic = f"v3/{self.app}@ttn/devices/{self.dev}/#"
        client.subscribe(topic)
        self.log(f"# TTN: conectado ({reason_code}), suscrito a {topic}")

    def _on_message(self, client, userdata, message):
        host_time = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            msg = json.loads(message.payload)
        except ValueError:
            msg = {"raw": message.payload.decode("utf-8", "replace")}
        with self.lock:
            self.out.write(json.dumps({"host_time": host_time, "topic": message.topic, "msg": msg}) + "\n")
            self.out.flush()
        kind = message.topic.rsplit("/", 1)[-1]
        if message.topic.endswith("/up"):
            self._on_uplink(msg)
        elif kind in ("queued", "sent", "ack", "nack", "failed", "join"):
            self.log(f"# TTN: {message.topic.split('/devices/')[-1]}")

    def _on_uplink(self, msg):
        self.dev_addr = msg.get("end_device_ids", {}).get("dev_addr", self.dev_addr)
        up = msg.get("uplink_message", {})
        fport = up.get("f_port")
        meta = (up.get("rx_metadata") or [{}])[0]
        self.log(f"# TTN: uplink fcnt={up.get('f_cnt')} fport={fport} rssi={meta.get('rssi')} snr={meta.get('snr')}")
        if fport is None or not FPORT_BASE <= fport <= FPORT_BASE + 4:
            return
        try:
            frame = decode(base64.b64decode(up.get("frm_payload", "")), fport)
        except ValueError as e:
            self.log(f"# TTN: trama no válida: {e}")
            return
        self.tracker.add(frame)
        if frame.strategy == 3 and self.ack_every:
            self.s3_since_ack += 1
            if self.tracker.beyond_point(frame):
                # falta algo delante de esta trama: el nodo reenvía desde su bin no confirmado más antiguo. Se avisa
                # en cada trama así, porque el nodo pierde el reenvío si sale de cobertura antes de completarlo
                self.send_ack("hueco")
            elif self.s3_since_ack >= self.ack_every:
                self.send_ack("periódico")  # para que el nodo libere el buffer

    def send_ack(self, reason: str) -> bool:
        point = self.tracker.ack_target()
        if point is None:
            return False
        if not self.budget.take():
            self.log(f"# TTN: ACK ({reason}) omitido: ya hay 10 downlinks en las últimas 24 h")
            return False
        self.s3_since_ack = 0
        payload = base64.b64encode(encode_ack(*point)).decode()
        body = {"downlinks": [{"f_port": FPORT_ACK, "frm_payload": payload, "priority": "NORMAL"}]}
        # replace: si había un ACK sin entregar, se sustituye por el nuevo
        self.client.publish(f"v3/{self.app}@ttn/devices/{self.dev}/down/replace", json.dumps(body))
        self.downlinks += 1
        self.last_ack = point
        self.log(f"# TTN: ACK ({reason}) en cola para la próxima ventana RX: época {point[0]}, secuencia {point[1]} "
                 f"({self.budget.available()} downlinks disponibles en 24 h)")
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=pathlib.Path)
    ap.add_argument("--ack-every", type=int, default=0)
    ap.add_argument("--buffer-bins", type=int, default=None, help="capacidad B del buffer del nodo")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    c = TtnCollector(args.out_dir, args.ack_every, buffer_bins=args.buffer_bins)
    c.start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        c.stop()


if __name__ == "__main__":
    main()
