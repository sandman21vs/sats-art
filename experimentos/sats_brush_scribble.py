"""Símbolo do sat em pincelada, desenhado no estilo "rabisco" do logo da Monero.

O que foi copiado da imagem da Monero:
- O círculo não é um anel separado: é a borda de um disco cheio, e o símbolo é
  espaço negativo recortado dele. Onde o símbolo encosta na borda, o círculo se
  interrompe (na Monero, os pés do M cortam o disco e separam a faixa de baixo).
- Cada contorno é um feixe de várias passadas, com traços curtos de vai-e-volta
  ao longo da borda (hachura), e só às vezes um pequeno espeto.
- Linhas retas soltas ligam pontos de contornos diferentes (círculo <-> símbolo,
  círculo <-> círculo), com comprimentos variados, sem formar teia no miolo.

Uso: python sats_brush_scribble.py [modo] [angulo] [seed]
  modo "um"   -> só o traço de baixo atravessa o disco (como os pés do M)
  modo "tres" -> os três traços atravessam o disco de borda a borda
"""
import math
import random
import sys

from PIL import Image, ImageDraw
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

MODE = sys.argv[1] if len(sys.argv) > 1 else "um"
ANGLE = float(sys.argv[2]) if len(sys.argv) > 2 else -21  # negativo = horário
random.seed(int(sys.argv[3]) if len(sys.argv) > 3 else 21)

W, H = 1920, 1080
SS = 4
CX, CY = 960, 540
R = 280
K = 0.5  # escala do símbolo (unidades do desenho -> px)


def bar(y0, thick, sag, x_start=-333, x_end=310, cut=False):
    """Pincelada: grossa à esquerda, afinando à direita.
    cut=True: atravessa o disco (sem cabeça arredondada, cauda não some)."""
    top, bot = [], []
    length = x_end - x_start
    for i in range(161):
        t = i / 160
        if cut:
            w = thick * (1 - 0.62 * t ** 1.6)
        else:
            w = thick * (1 - 0.88 * t ** 2.2)
            w *= math.sqrt(max(0.0, 1 - (1 - min(t / 0.05, 1)) ** 2))
        cy = y0 - sag * math.sin(math.pi * t) + thick * 0.25 * t ** 3
        x = x_start + length * t
        top.append((x, cy - w / 2))
        bot.append((x, cy + w / 2))
    return top + bot[::-1]


def drop(cx, cy, h, w):
    pts = []
    for i in range(80):
        a = 2 * math.pi * i / 80
        x = math.sin(a) * w / 2 * (0.5 - 0.5 * math.cos(a)) ** 0.35
        pts.append((cx + x, cy - math.cos(a) * h / 2))
    return pts


def place(poly):
    a = math.radians(ANGLE)
    return [(CX + K * (x * math.cos(a) + y * math.sin(a)),
             CY + K * (-x * math.sin(a) + y * math.cos(a))) for x, y in poly]


T = 78
far = 1.3 * R / K  # longe o bastante para passar da borda
cut_all = MODE == "tres"
bars = [
    bar(-170, T * 1.05, 14, *((-far, far, True) if cut_all else ())),
    bar(0, T * 0.85, 10, *((-far, far, True) if cut_all else ())),
    bar(165, T * 1.0, 16, -far, far, True),
]
holes = [Polygon(place(b)) for b in bars]
holes += [Polygon(place(drop(-10, -365, 120, 62))), Polygon(place(drop(-10, 355, 110, 60)))]

region = Point(CX, CY).buffer(R, 256).difference(unary_union(holes))
polys = list(region.geoms) if hasattr(region, "geoms") else [region]
rings = []
for p in polys:
    rings.append(list(p.exterior.coords)[:-1])
    rings += [list(r.coords)[:-1] for r in p.interiors]


def resample(ring, step=5):
    pts = ring + [ring[0]]
    out = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        out += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n)]
    return out


shapes = [resample(r) for r in rings]
nodes = [(si, i) for si, sh in enumerate(shapes) for i in range(len(sh))]
visits = {n: 0 for n in nodes}


def jitter(p):
    # feixe com hachura fina; espeto pequeno e raro
    s = 6 if random.random() < 0.015 else 2.2
    return (p[0] + random.gauss(0, s), p[1] + random.gauss(0, s))


def next_start(p, si):
    """Depois de um trecho, salta para outro contorno (linha reta solta)."""
    if random.random() < 0.25:  # garante cobertura uniforme
        return min(random.sample(nodes, 300), key=lambda n: visits[n])
    reach = random.uniform(80, 2 * R)
    cand = [n for n in random.sample(nodes, 500)
            if 40 < math.dist(p, shapes[n[0]][n[1]]) < reach]
    other = [n for n in cand if n[0] != si]
    pool = other if other and random.random() < 0.8 else cand
    return min(pool, key=lambda n: visits[n] + 2 * random.random()) if pool else random.choice(nodes)


path = []
si, i = 0, 0
total = len(nodes) * 7
while len(path) < total:
    shape = shapes[si]
    direction = random.choice((1, -1))
    for _ in range(random.randint(90, 320)):
        r = random.random()
        if r < 0.18:
            i -= direction * random.randint(1, 4)          # vai-e-volta (hachura)
        elif r < 0.183:
            i += direction * random.randint(5, 15)         # atalho reto curto
        else:
            i += direction * random.randint(1, 3)
        i %= len(shape)
        visits[(si, i)] += 1
        path.append(jitter(shape[i]))
    si, i = next_start(shape[i], si)

pts = [(x * SS, y * SS) for x, y in path]


def render(bg, fg, name):
    img = Image.new("RGBA", (W * SS, H * SS), bg)
    ImageDraw.Draw(img).line(pts, fill=fg, width=5, joint="curve")
    img.resize((W, H), Image.LANCZOS).save(name)


suffix = "" if MODE == "um" else "_tres"
render((33, 33, 33, 255), (255, 102, 0, 255), f"sats_brush_scribble_laranja{suffix}.png")
render((255, 255, 255, 255), (0, 0, 0, 255), f"sats_brush_scribble{suffix}.png")
render((0, 0, 0, 0), (0, 0, 0, 255), f"sats_brush_scribble_transparente{suffix}.png")
