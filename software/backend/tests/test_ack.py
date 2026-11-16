import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hij.ack import AckTracker  # noqa: E402
from hij.payload import FLAG_EPOCH_END, FLAG_RETX, Frame, decode, encode_ack  # noqa: E402


def frame(epoch, s1, n, flags=0, strategy=3):
    return Frame(strategy, flags, epoch, s1, n, 0, 0, [0] * n)


def test_contiguous_prefix():
    t = AckTracker()
    t.add(frame(5, 0, 4))
    t.add(frame(5, 4, 4))
    assert t.ack_point() == (5, 7)


def test_gap_stops_ack():
    t = AckTracker()
    t.add(frame(5, 0, 4))
    t.add(frame(5, 8, 4))       # se ha perdido la trama con 4..7
    assert t.ack_point() == (5, 3)
    t.add(frame(5, 4, 4))       # llega la retransmisión
    assert t.ack_point() == (5, 11)


def test_new_epoch_needs_epoch_end():
    # se pierden los últimos bins de la época 5 y llegan los de la 6: no se puede confirmar la 6
    t = AckTracker()
    t.add(frame(5, 0, 4))
    t.add(frame(6, 0, 4))
    assert t.ack_point() == (5, 3)
    t.add(frame(5, 4, 2, FLAG_EPOCH_END))   # llegan los que faltaban, con la marca de fin de época
    assert t.ack_point() == (6, 3)


def test_epoch_wrap():
    t = AckTracker()
    t.add(frame(255, 0, 2, FLAG_EPOCH_END))
    t.add(frame(0, 0, 3))
    assert t.ack_point() == (0, 2)


def test_nothing_confirmed_until_first_bin():
    # se pierde la primera trama: no hay punto de ACK, pero se avisa con una posición que el nodo no tiene
    t = AckTracker()
    t.add(frame(5, 8, 4))
    assert t.ack_point() is None and t.has_gap()
    assert t.ack_target() == (5, 0xFFFF)
    t.add(frame(5, 0, 12, FLAG_RETX))      # el nodo reenvía desde su bin no confirmado más antiguo
    assert t.ack_target() == t.ack_point() == (5, 11) and not t.has_gap()


def test_nothing_confirmed_and_overflow():
    # sin punto de ACK, el reenvío empieza en el 4: el nodo ya no tiene los bins 0..3
    t = AckTracker()
    t.add(frame(5, 8, 4))
    t.add(frame(5, 4, 8, FLAG_RETX))
    assert t.ack_point() == (5, 11)


def test_target_is_point_when_known():
    t = AckTracker()
    assert t.ack_target() is None           # sin tramas no se manda nada
    t.add(frame(5, 0, 4))
    assert t.ack_target() == t.ack_point() == (5, 3)


def frame_age(epoch, s1, n, age_bins, flags=0):
    return Frame(3, flags, epoch, s1, n, age_bins * 15, 0, [0] * n)


def test_capacity_rule_ignores_retx_continuation():
    # B = 100: llega una trama reenviada que empieza en el 50 (su primera trama no llegó); el nodo aún tiene 4..49
    t = AckTracker(buffer_bins=100)
    t.add(frame_age(5, 0, 4, 4))
    t.add(frame_age(5, 50, 20, 30, FLAG_RETX))   # bin abierto: 80, el nodo conserva desde el 80 - 100
    assert t.ack_point() == (5, 3)
    old = AckTracker()
    old.add(frame(5, 0, 4))
    old.add(frame(5, 50, 20, FLAG_RETX))
    assert old.ack_point() == (5, 69)           # la regla antigua los daba por perdidos


def test_capacity_rule_forgets_overflow():
    # B = 10: con el bin abierto en el 30 el nodo conserva del 20 al 29; los anteriores ya no existen
    t = AckTracker(buffer_bins=10)
    t.add(frame_age(5, 0, 4, 4))
    t.add(frame_age(5, 22, 4, 8))                # bin abierto: 30
    assert t.ack_point() == (5, 19)
    t.add(frame_age(5, 20, 3, 10))               # el nodo reenvía desde su bin más antiguo, el 20
    assert t.ack_point() == (5, 25)


def test_capacity_rule_closes_older_epochs():
    t = AckTracker(buffer_bins=10)
    t.add(frame_age(5, 0, 4, 4))                 # época 5 sin cerrar (no llegó su fin)
    t.add(frame_age(6, 0, 4, 30))                # la época 6 ya llena el buffer: de la 5 no queda nada
    assert t.last_seq[5] == 3                    # la 5 se cierra donde está
    assert t.ack_point() == (6, 19)              # y en la 6 el nodo conserva del 20 al 29
    t.add(frame_age(6, 20, 10, 10))              # bin abierto: 30
    assert t.ack_point() == (6, 29)


def test_capacity_rule_exact_boundary():
    # B = 96 con los bins 0..100 cerrados y el 101 abierto: el nodo conserva del 5 al 100 (bufPush descarta el
    # más antiguo con el buffer lleno), así que el 4 ya no existe y el 5 sí
    t = AckTracker(buffer_bins=96)
    t.add(frame_age(5, 0, 3, 3))                 # antes del corte: 0..2
    f = frame_age(5, 97, 4, 4)                   # primera trama de la visita, con los más nuevos: 97..100
    t.add(f)
    assert t.lost[5] == {3, 4}                   # el 5 no: el nodo aún lo tiene
    assert t.beyond_point(f) and t.ack_target() == (5, 4)   # el nodo no tiene el 4: reenvía desde el 5
    # una trama después (4 bins) el nodo ha descartado del 5 al 8 y reenvía 9..104 con el 105 abierto
    f = frame_age(5, 9, 96, 96, FLAG_RETX)
    t.add(f)
    assert t.lost[5] == set(range(3, 9))
    assert t.ack_point() == (5, 104) and not t.has_gap() and not t.beyond_point(f)
    # con un bin de margen se quedaba en (5, 7): otro ACK que el nodo no encuentra y otra trama repetida


def test_capacity_rule_two_epochs():
    # B = 8. De la época 5 llegan 0..3 y no su fin; tras un reinicio llegan bins de la 6
    t = AckTracker(buffer_bins=8)
    t.add(frame_age(5, 0, 4, 4))
    t.add(frame_age(6, 0, 7, 7))                 # 0..6 con el 7 abierto (< B): el nodo aún puede tener un bin de la 5
    assert 5 not in t.last_seq and t.ack_point() == (5, 3)
    f = frame_age(6, 7, 1, 1)                    # 7 con el 8 = B abierto: la 6 llena el buffer por sí sola
    t.add(f)
    assert t.last_seq[5] == 3                    # de la 5 no queda nada: se cierra donde está
    assert not t.lost.get(6) and t.ack_point() == (6, 7) and not t.beyond_point(f)


def test_capacity_rule_nothing_confirmed():
    # no ha llegado el primer bin: mientras el nodo pueda tenerlo se manda "nada confirmado"
    t = AckTracker(buffer_bins=30)
    t.add(frame_age(5, 22, 4, 8))                # bin abierto: 30 = B, el nodo conserva del 0 al 29
    assert t.ack_point() is None and t.ack_target() == (5, 0xFFFF)
    t = AckTracker(buffer_bins=29)
    t.add(frame_age(5, 22, 4, 8))                # con B = 29 el 0 ya no existe
    assert t.lost[5] == {0} and t.ack_target() == (5, 0)


def test_capacity_rule_older_epoch_without_bins():
    # como field.seed_tracker con s0 = 0: de la época 5 no se sabe nada y la 6 llena el buffer
    t = AckTracker(buffer_bins=8)
    t.first_epoch = 5
    t.received.setdefault(5, set())
    t.add(frame_age(6, 4, 4, 12))                # no falla; la 5 se deja abierta y no se confirma nada
    assert 5 not in t.last_seq and t.ack_point() is None and t.ack_target() == (5, 0xFFFF)


def test_beyond_point_triggers_on_every_frame_after_gap():
    t = AckTracker()
    f = frame(5, 0, 4)
    t.add(f)
    assert not t.beyond_point(f)                 # prolonga lo confirmado
    f = frame(5, 8, 4)
    t.add(f)
    assert t.beyond_point(f)                     # faltan 4..7
    f = frame(5, 12, 4)
    t.add(f)
    assert t.beyond_point(f)                     # sigue faltando: se vuelve a avisar (el nodo no reenvió)


def test_beyond_point_quiet_while_retransmission_fills_gap():
    # visita tras un corte: llegan los bins nuevos y el nodo reenvía el atraso en varias tramas
    t = AckTracker()
    t.add(frame(5, 0, 4))
    f = frame(5, 300, 4)
    t.add(f)
    assert t.beyond_point(f)
    f = frame(5, 4, 105, FLAG_RETX)              # rellena parte del hueco: prolonga lo confirmado
    t.add(f)
    assert not t.beyond_point(f) and t.has_gap()
    f = frame(5, 109, 105, FLAG_RETX)
    t.add(f)
    assert not t.beyond_point(f)
    f = frame(5, 214, 86, FLAG_RETX)             # cierra el hueco y enlaza con 300..303
    t.add(f)
    assert not t.beyond_point(f) and t.ack_point() == (5, 303) and not t.has_gap()


def test_beyond_point_without_first_bin_and_across_epochs():
    t = AckTracker()
    f = frame(5, 8, 4)
    t.add(f)
    assert t.beyond_point(f)                     # no ha llegado el primer bin
    t = AckTracker()
    t.add(frame(5, 0, 4))
    f = frame(6, 0, 4)
    t.add(f)
    assert t.beyond_point(f)                     # época nueva sin saber dónde acababa la anterior
    f = frame(5, 4, 2, FLAG_EPOCH_END)
    t.add(f)
    assert not t.beyond_point(f) and t.ack_point() == (6, 3)


def test_beyond_point_duplicate_frame():
    t = AckTracker()
    t.add(frame(5, 0, 8))
    f = frame(5, 4, 4, FLAG_RETX)                # bins ya recibidos
    t.add(f)
    assert not t.beyond_point(f)


def test_gap_detection():
    t = AckTracker()
    t.add(frame(5, 0, 4))
    assert not t.has_gap()
    t.add(frame(5, 8, 4))           # faltan 4..7
    assert t.has_gap() and t.ack_point() == (5, 3)
    t.add(frame(5, 4, 4))
    assert not t.has_gap()
    t.add(frame(6, 0, 2))           # época nueva sin saber dónde acababa la 5
    assert t.has_gap()


def test_overflow_skip():
    # el nodo ha descartado los bins 4..11 por desbordamiento: su reenvío empieza en el 12
    t = AckTracker()
    t.add(frame(5, 0, 4))
    t.add(frame(5, 16, 4))
    assert t.ack_point() == (5, 3) and t.has_gap()
    t.add(frame(5, 12, 4, FLAG_RETX))
    assert t.ack_point() == (5, 19) and not t.has_gap()


def test_retx_from_ack_point_does_not_skip():
    t = AckTracker()
    t.add(frame(5, 0, 4))
    t.add(frame(5, 8, 4))
    t.add(frame(5, 4, 8, FLAG_RETX))   # reenvío normal desde el hueco
    assert t.ack_point() == (5, 11)


def test_s0_ignored():
    t = AckTracker()
    t.add(frame(5, 0, 1, strategy=0))
    assert t.ack_point() is None


def test_decode_roundtrip():
    raw = bytes.fromhex("0001000004003C0000009E0036002800000040")
    f = decode(raw, 11)
    assert (f.strategy, f.epoch, f.s1, f.n, f.age_units, f.cumulative) == (1, 1, 0, 4, 60, 158)
    assert f.bins == [54, 40, 0, 64]
    assert encode_ack(6, 300) == bytes([6, 1, 44])
