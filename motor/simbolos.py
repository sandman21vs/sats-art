"""Símbolos que podem ser recortados do disco em rede_nos.py.

Cada função recebe o centro (cx, cy) e o ângulo em graus (negativo = horário)
e devolve a geometria shapely dos "buracos" — o símbolo, em unidades de desenho
(o disco tem 300 unidades de raio).
"""
import math

from shapely.geometry import Polygon
from shapely.ops import unary_union


def _place(poly, cx, cy, k, angle):
    a = math.radians(angle)
    return [(cx + k * (x * math.cos(a) + y * math.sin(a)),
             cy + k * (-x * math.sin(a) + y * math.cos(a))) for x, y in poly]


# ---------- sat: três pinceladas e duas gotas ----------
def _bar(y0, thick, sag, x_start=-333, x_end=310, cut=False):
    top, bot = [], []
    for i in range(161):
        t = i / 160
        if cut:
            w = thick * (1 - 0.62 * t ** 1.6)
        else:
            w = thick * (1 - 0.88 * t ** 2.2)
            w *= math.sqrt(max(0.0, 1 - (1 - min(t / 0.05, 1)) ** 2))
        cy = y0 - sag * math.sin(math.pi * t) + thick * 0.25 * t ** 3
        x = x_start + (x_end - x_start) * t
        top.append((x, cy - w / 2))
        bot.append((x, cy + w / 2))
    return top + bot[::-1]


def _drop(cx, cy, h, w):
    pts = []
    for i in range(80):
        a = 2 * math.pi * i / 80
        x = math.sin(a) * w / 2 * (0.5 - 0.5 * math.cos(a)) ** 0.35
        pts.append((cx + x, cy - math.cos(a) * h / 2))
    return pts


def sats(cx, cy, angle=-21, k=0.54):
    T = 78
    place = lambda p: Polygon(_place(p, cx, cy, k, angle))  # noqa: E731
    return unary_union([
        place(_bar(-170, T * 1.05, 14)),
        place(_bar(0, T * 0.85, 10)),
        place(_bar(165, T * 1.0, 16)),
        place(_drop(-10, -365, 120, 62)),
        place(_drop(-10, 355, 110, 60)),
    ])


SIMBOLOS = {"sat": (sats, -21)}
