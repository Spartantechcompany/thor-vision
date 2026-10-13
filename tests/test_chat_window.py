"""La ventana de historia que el chat infiere de la pregunta (antes decia 6 h y cubria 1 h)."""
import pytest

from app.api.routes_chat import _human_window, _infer_history_hours


@pytest.mark.parametrize("q,hours", [
    ("que paso en las ultimas 3 horas", 3.0),
    ("últimos 2 días", 48.0),
    ("hace 90 minutos", None),
    ("ayer", 48.0),
    ("esta semana", 168.0),
    ("este mes", 720.0),
    ("hoy", 24.0),
])
def test_infer_hours(q, hours):
    h = _infer_history_hours(q)
    if hours is None:
        assert 0.25 <= h <= 24
    else:
        assert h == hours


def test_numeric_pattern_beats_generic():
    assert _infer_history_hours("las últimas 3 horas") == 3.0


def test_window_is_capped_and_has_label():
    assert _infer_history_hours("últimas 99 horas") <= 24 * 31
    assert _human_window(0.5) == "última hora"
    assert _human_window(168) == "última semana"
    assert _human_window(720) == "último mes"
