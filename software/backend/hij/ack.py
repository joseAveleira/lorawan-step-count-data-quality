"""Seguimiento de los bins recibidos para los ACK de la S3.

El ACK es la última (época, secuencia) recibida sin huecos desde el principio del experimento. Para pasar de
una época a la siguiente hay que saber dónde terminaba la anterior: lo indica la bandera FLAG_EPOCH_END de la
trama que lleva su último bin. Sin ella, el backend podría confirmar bins que nunca llegaron y el nodo los
borraría.
"""
from .payload import FLAG_EPOCH_END, FLAG_RETX, Frame

NOTHING_SEQ = 0xFFFF  # secuencia que ningún bin real alcanza (65 536 bins: 682 días con Δ = 15 min, 45 con 1 min)


class AckTracker:
    """Los bins que el nodo ya no tiene (descartados por desbordamiento del buffer) se marcan como perdidos para
    que el ACK pueda avanzar. Hay dos reglas para saber cuáles son:

    - Sin buffer_bins (la regla anterior; el receptor de la campaña la usó en C1_S3 y C4-24_S3, y desde C2_S3
      usa la de capacidad con el B del plan): tras un reenvío, la trama empieza en el bin no confirmado más
      antiguo que conserva el nodo, así que si una trama reenviada (FLAG_RETX) empieza más allá del punto de ACK,
      los intermedios ya no existen. Falla si la trama recibida continúa un reenvío cuya primera trama no llegó
      (por ejemplo, la red de seguridad tras días sin cobertura): daría por perdidos bins que el nodo aún
      conserva, el ACK los confirmaría y el nodo los borraría.
    - Con buffer_bins = B (regla de capacidad): el nodo conserva los B bins cerrados más recientes. Con newest =
      bin abierto más reciente conocido de la época (s1 + edad // 15 de una trama), un bin no recibido solo se da
      por perdido si es anterior a newest - B; si la época llena el buffer por sí sola (newest >= B), de las
      épocas anteriores no queda nada. No depende de las banderas y, si el nodo cierra bins sin pausas, nunca da
      por perdido un bin que el nodo pueda volver a enviar (detalles en _forget_beyond_capacity)."""

    def __init__(self, buffer_bins: int | None = None):
        self.received: dict[int, set[int]] = {}
        self.lost: dict[int, set[int]] = {}  # bins que el nodo ya no tiene
        self.last_seq: dict[int, int] = {}   # época cerrada -> secuencia de su último bin
        self.first_epoch: int | None = None
        self.buffer_bins = buffer_bins
        self.newest: dict[int, int] = {}     # época -> bin abierto más reciente conocido (regla de capacidad)

    def add(self, frame: Frame) -> None:
        if frame.strategy == 0:
            return  # en la S0 el valor no es un bin
        if self.first_epoch is None:
            self.first_epoch = frame.epoch
        if self.buffer_bins is None and frame.strategy == 3 and frame.has(FLAG_RETX):
            self._skip_missing_before(frame.epoch, frame.s1)
        seqs = self.received.setdefault(frame.epoch, set())
        seqs.update(range(frame.s1, frame.s1 + frame.n))
        if frame.has(FLAG_EPOCH_END):
            self.last_seq[frame.epoch] = frame.s1 + frame.n - 1
        if self.buffer_bins is not None and frame.age_known:
            self._forget_beyond_capacity(frame.epoch, frame.s1 + frame.age_units // 15)

    def _forget_beyond_capacity(self, epoch: int, open_seq: int) -> None:
        """Regla de capacidad exacta. bufPush descarta el bin más antiguo si el buffer está lleno, así que con el
        bin abierto newest el nodo conserva de newest - B a newest - 1: los anteriores a oldest_kept = newest - B
        ya no existen (B = 96 con el 101 abierto: el 4 se ha perdido y el 5 no). Si newest >= B, el buffer solo
        tiene bins de esta época y de las anteriores no queda nada.

        open_seq = s1 + edad // 15 puede pasarse en un bin del abierto real (el cierre del bin y el envío caen en
        milisegundos distintos de la misma frontera, o la espera por el límite de airtime cruza una frontera). Es
        seguro: el bin dado de más por perdido es el más antiguo de un buffer lleno, que el nodo descarta igual al
        cerrar el bin abierto, antes de su siguiente trama; un ACK que lo salte solo adelanta ese borrado.
        La regla supone que el nodo cierra bins sin pausas: tras un apagado con el botón (el reloj sigue y no se
        cierran bins), s1 + edad sobrestima la posición en las tramas de una época anterior y podría dar por
        perdidos bins que el nodo conserva. No ocurre en el laboratorio ni en la simulación."""
        self.newest[epoch] = max(self.newest.get(epoch, 0), open_seq)
        oldest_kept = self.newest[epoch] - self.buffer_bins
        if oldest_kept < 0:
            return
        self.lost.setdefault(epoch, set()).update(set(range(oldest_kept)) - self.received.get(epoch, set()))
        # el buffer entero es de esta época: de las anteriores no queda nada, se cierran donde estén
        for e in list(self.received):
            if self._order(e) < self._order(epoch) and e not in self.last_seq:
                have = self.received[e] | self.lost.get(e, set())
                if not have:
                    continue  # nada conocido de esa época (field.seed_tracker con s0 = 0): se deja abierta
                top = max(have)
                self.lost.setdefault(e, set()).update(set(range(top + 1)) - have)
                self.last_seq[e] = top

    def _skip_missing_before(self, epoch: int, s1: int) -> None:
        # sin punto de ACK (no ha llegado el primer bin) se cuenta desde antes del primer bin de la primera época
        e_ack, s_ack = self.ack_point() or (self.first_epoch, -1)
        if self._order(epoch) == self._order(e_ack):
            if s1 > s_ack + 1:
                self.lost.setdefault(e_ack, set()).update(range(s_ack + 1, s1))
        elif self._order(epoch) > self._order(e_ack):
            # el nodo ya no conserva nada de las épocas anteriores: se cierran donde estén
            for e in list(self.received):
                if self._order(e) < self._order(epoch) and e not in self.last_seq:
                    have = self.received[e] | self.lost.get(e, set())
                    top = max(have)
                    self.lost.setdefault(e, set()).update(set(range(top + 1)) - have)
                    self.last_seq[e] = top
            if s1 > 0:
                self.lost.setdefault(epoch, set()).update(set(range(s1)) - self.received.get(epoch, set()))

    def _order(self, epoch: int) -> int:
        return (epoch - self.first_epoch) % 256  # las épocas son de 8 bits

    def has_gap(self) -> bool:
        """Hay bins recibidos más allá del punto de ACK, es decir, falta alguno intermedio."""
        if not self.received:
            return False
        point = self.ack_point()
        if point is None:
            return True  # faltan los primeros bins
        e_ack, s_ack = point
        for epoch, seqs in self.received.items():
            if self._order(epoch) > self._order(e_ack) and seqs:
                return True
            if epoch == e_ack and max(seqs | self.lost.get(epoch, set())) > s_ack:
                return True
        return False

    def beyond_point(self, frame: Frame) -> bool:
        """La trama no prolonga lo confirmado: su último bin queda más allá del punto de ACK (o aún no hay punto),
        así que delante de ella falta algo. Es lo que dispara el ACK de hueco. Una trama que continúa lo recibido
        (por ejemplo, un reenvío que va rellenando el hueco) no lo dispara, aunque queden bins sueltos más allá.
        Llamar después de add(frame)."""
        point = self.ack_point()
        if point is None:
            return True
        e_ack, s_ack = point
        return (self._order(frame.epoch), frame.s1 + frame.n - 1) > (self._order(e_ack), s_ack)

    def ack_target(self) -> tuple[int, int] | None:
        """Lo que se manda en el ACK: el punto de ACK o, si aún no ha llegado el primer bin, (primera época,
        NOTHING_SEQ). El nodo no tiene ese bin, así que no confirma nada y, como el ACK se queda por detrás de su
        trama anterior, reenvía desde su bin no confirmado más antiguo (handleDownlink del firmware, sin cambios).
        Sin esto, si se pierde la primera trama la S3 no puede confirmar nada en todo el seguimiento."""
        point = self.ack_point()
        if point is None and self.first_epoch is not None:
            return (self.first_epoch, NOTHING_SEQ)
        return point

    def ack_point(self) -> tuple[int, int] | None:
        point = None
        for epoch in sorted(self.received, key=self._order):
            seqs = self.received[epoch] | self.lost.get(epoch, set())
            s = -1
            while s + 1 in seqs:
                s += 1
            if s < 0:
                break                       # falta el primer bin de la época
            point = (epoch, s)
            if self.last_seq.get(epoch) != s:
                break                       # época con huecos o sin cerrar: no se puede pasar a la siguiente
        return point
