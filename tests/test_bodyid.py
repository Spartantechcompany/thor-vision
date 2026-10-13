"""Fase 1: color de ropa, compatibilidad y preparacion del recorte para el modelo de cuerpo."""
import numpy as np

from app.vision import bodyid as b


def _solid(bgr, h=200, w=100):
    return np.full((h, w, 3), bgr, np.uint8)


def test_color_names():
    assert b.color_name(_solid((0, 0, 255))) == "rojo"
    assert b.color_name(_solid((255, 0, 0))) == "azul"
    assert b.color_name(_solid((10, 10, 10))) == "negro"
    assert b.color_name(_solid((240, 240, 240))) == "blanco"
    assert b.color_name(_solid((128, 128, 128))) == "gris"


def test_clothing_splits_upper_and_lower():
    img = np.zeros((300, 150, 3), np.uint8)
    img[:150] = (0, 0, 255)           # arriba rojo
    img[150:] = (255, 0, 0)           # abajo azul
    c = b.clothing(img)
    assert c == {"upper": "rojo", "lower": "azul"}


def test_compatible_rules():
    assert b.compatible({"upper": "rojo", "lower": "azul"}, {"upper": "rojo", "lower": "azul"})
    assert not b.compatible({"upper": "rojo", "lower": "azul"}, {"upper": "verde", "lower": "azul"})     # una prenda claramente distinta
    assert b.compatible({"upper": "gris", "lower": "negro"}, {"upper": "negro", "lower": "gris"})        # infrarrojo: todo parece gris
    assert not b.compatible({"upper": None, "lower": None}, {"upper": "rojo", "lower": "azul"})          # sin datos no se sugiere


def test_prep_shape_and_padding():
    t = b.prep(np.zeros((288, 183, 3), np.uint8))
    assert t.shape == (3, 256, 128) and t.dtype == np.float32
