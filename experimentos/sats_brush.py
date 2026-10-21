import math
import random
import sys

from PIL import Image, ImageDraw, ImageFilter

ANGLE = float(sys.argv[1]) if len(sys.argv) > 1 else 21  # graus, anti-horário
random.seed(int(sys.argv[2]) if len(sys.argv) > 2 else 3)

S = 4                 # supersampling
N = 1200 * S          # canvas quadrado do símbolo
CX = CY = N / 2


def noise(n, amp, smooth=6):
    """Ruído 1D suave (para bordas irregulares de pincel)."""
    raw = [random.gauss(0, 1) for _ in range(n + smooth)]
    out = [sum(raw[i:i + smooth]) / smooth for i in range(n)]
    return [v * amp for v in out]


def bar(mask, y0, length, thick, sag, tail_streaks=True):
    """Pincelada horizontal: ponta esquerda arredondada e grossa, cauda afinando à direita."""
    d = ImageDraw.Draw(mask)
    x0 = CX - length * 0.52
    steps = 300
    top_n, bot_n = noise(steps + 1, thick * 0.07, 30), noise(steps + 1, thick * 0.07, 30)
    top, bot = [], []
    for i in range(steps + 1):
        t = i / steps
        w = thick * (1 - 0.88 * t ** 2.2)            # afina até quase ponta
        w *= math.sqrt(max(0.0, 1 - (1 - min(t / 0.05, 1)) ** 2))  # cabeça arredondada
        cy = y0 - sag * math.sin(math.pi * t) + thick * 0.25 * t ** 3  # arco suave, cauda cai
        x = x0 + length * t
        top.append((x, cy - w / 2 + top_n[i]))
        bot.append((x, cy + w / 2 + bot_n[i]))
    d.polygon(top + bot[::-1], fill=255)
    if tail_streaks:  # efeito de pincel seco: riscos vazios na cauda
        for _ in range(6):
            yy = y0 + random.uniform(-0.38, 0.38) * thick
            xs = x0 + length * random.uniform(0.6, 0.8)
            xe = xs + length * random.uniform(0.1, 0.25)
            d.line((xs, yy, xe, yy + thick * 0.25 * ((xe - x0) / length) ** 3),
                   fill=0, width=random.randint(1, 2) * S)


def drop(mask, cx, cy, h, w):
    """Gota de tinta: ponta em cima, base arredondada."""
    d = ImageDraw.Draw(mask)
    pts = []
    for i in range(121):
        a = 2 * math.pi * i / 120
        # gota: raio varia, afinando em cima
        x = math.sin(a) * w / 2 * (0.5 - 0.5 * math.cos(a)) ** 0.35
        y = -math.cos(a) * h / 2
        pts.append((cx + x, cy + y))
    d.polygon(pts, fill=255)


mask = Image.new("L", (N, N), 0)
L, T = 640 * S, 78 * S
bar(mask, CY - 170 * S, L * 1.0, T * 1.05, 14 * S)
bar(mask, CY, L * 0.96, T * 0.85, 10 * S)
bar(mask, CY + 165 * S, L * 1.04, T * 1.0, 16 * S)
drop(mask, CX - 10 * S, CY - 365 * S, 120 * S, 62 * S)
drop(mask, CX - 10 * S, CY + 355 * S, 110 * S, 60 * S)

mask = mask.filter(ImageFilter.GaussianBlur(S * 0.8))
mask = mask.rotate(ANGLE, resample=Image.BICUBIC, center=(CX, CY))
mask = mask.resize((N // S, N // S), Image.LANCZOS)


def render(size, bg, fg, scale, name):
    m = mask.resize((int(mask.width * scale),) * 2, Image.LANCZOS)
    img = Image.new("RGBA", size, bg)
    layer = Image.new("RGBA", m.size, fg)
    layer.putalpha(m)
    img.alpha_composite(layer, ((size[0] - m.width) // 2, (size[1] - m.height) // 2))
    img.save(name)


render((1080, 1080), (255, 255, 255, 255), (0, 0, 0, 255), 0.9, "sats_brush.png")
render((1080, 1080), (0, 0, 0, 0), (0, 0, 0, 255), 0.9, "sats_brush_transparente.png")
render((1920, 1080), (33, 33, 33, 255), (255, 102, 0, 255), 0.85, "sats_brush_laranja.png")
