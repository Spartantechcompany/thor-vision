"""El recorte de rostro es siempre cuadrado: nunca se estira, ni siquiera en el borde del cuadro."""
import ast
import textwrap

import cv2
import numpy as np


def _crop_fn():
    src = open("app/pipeline/vision_processor.py", encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "_crop_thumb":
            code = textwrap.dedent(ast.get_source_segment(src, node)).replace("@staticmethod\n", "")
            ns = {"cv2": cv2, "np": np, "Optional": __import__("typing").Optional, "logger": __import__("logging").getLogger("t")}
            exec(code, ns)
            return ns["_crop_thumb"]
    raise AssertionError("no existe _crop_thumb")


def _decode(b):
    return cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)


def test_non_square_bbox_gives_square_crop_without_stretch():
    f = np.zeros((400, 600, 3), np.uint8)
    cv2.circle(f, (300, 200), 40, (255, 255, 255), -1)           # un circulo se mantiene circulo si no hay estiramiento
    out = _decode(_crop_fn()(f, (270, 140, 330, 260), size=128, margin=0.35))        # bbox 60x120 (no cuadrado)
    assert out.shape[:2] == (128, 128)
    ys, xs = np.where(out[:, :, 0] > 128)
    assert abs((xs.max() - xs.min()) - (ys.max() - ys.min())) <= 3


def test_bbox_at_frame_edge_is_padded_not_squished():
    f = np.full((300, 300, 3), 90, np.uint8)
    out = _decode(_crop_fn()(f, (0, 0, 80, 80), size=96, margin=0.35))
    assert out is not None and out.shape[:2] == (96, 96)


def test_bbox_outside_frame_returns_none():
    f = np.zeros((100, 100, 3), np.uint8)
    assert _crop_fn()(f, (200, 200, 260, 260)) is None
