"""Viajes por puerta: solo cuentan los recorridos que pasan por la puerta o que empiezan en la camara de entrada justo despues del disparo."""
from app.vision import trips as T


def _t():
    t = T.Trips.__new__(T.Trips)
    t.street, t.entry, t.cam_names = {"cam-189", "cam-191", "cam-vto"}, {"cam-228"}, {}
    return t


def _v(vid, cam, a, b, journey):
    return (vid, cam, a, b, None, None, None, None, journey)


def test_quien_ya_estaba_adentro_no_entra_al_viaje_y_quien_llega_por_la_entrada_si():
    t = _t()
    s = {"start": 1000.0, "inner": ["cam-228", "cam-113", "cam-120"], "entry": {"cam-228"}}
    vs = [_v(1, "cam-120", 900, 1100, "a"), _v(2, "cam-113", 1110, 1140, "a"),       # ya estaba adentro antes del timbre
          _v(3, "cam-228", 1040, 1060, "b"), _v(4, "cam-113", 1070, 1090, "b"),      # llega por Garage frontal 40 s despues
          _v(5, "cam-113", 1050, 1080, "c"), _v(6, "cam-120", 1085, 1100, "c")]      # se mueve adentro pero no empieza en la entrada
    paths = t._paths(s, vs)
    assert [[v[0] for v in p] for p in paths] == [[3, 4]]
    assert t._direction(paths[0], s) == "entrada"


def test_recorrido_que_pasa_por_la_calle_siempre_cuenta():
    t = _t()
    s = {"start": 1000.0, "inner": ["cam-228", "cam-113"], "entry": {"cam-228"}}
    vs = [_v(1, "cam-191", 1500, 1520, "a"), _v(2, "cam-113", 1400, 1450, "a")]     # sale a la calle mucho despues del disparo
    assert len(t._paths(s, vs)) == 1
