import math
import random
import sys

from PIL import Image, ImageDraw

W, H = 1920, 1080
SS = 2  # supersampling
BG = (33, 33, 33)
FG = (255, 102, 0)
CX, CY = 960, 540
R = 360

random.seed(int(sys.argv[1]) if len(sys.argv) > 1 else 21)


def resample(poly, step=6, closed=True):
    pts = poly + [poly[0]] if closed else poly
    out = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for i in range(n):
            t = i / n
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return out


def circle():
    return [(CX + R * math.cos(a), CY + R * math.sin(a))
            for a in (2 * math.pi * i / 400 for i in range(400))]


def sat_symbol():
    hw, t, gap = 190, 54, 125      # bar half-width, bar thickness, bar spacing
    v, stub = 27, 255              # vertical half-width, vertical half-length
    ys = [CY - gap, CY, CY + gap]
    right = [(CX + v, CY - stub)]
    for y in ys:
        right += [(CX + v, y - t / 2), (CX + hw, y - t / 2),
                  (CX + hw, y + t / 2), (CX + v, y + t / 2)]
    right.append((CX + v, CY + stub))
    left = [(2 * CX - x, y) for x, y in reversed(right)]
    return right + left


shapes = [resample(circle()), resample(sat_symbol())]
all_pts = [p for s in shapes for p in s]


def jitter(p):
    s = 9 if random.random() < 0.03 else 2.2
    return (p[0] + random.gauss(0, s), p[1] + random.gauss(0, s))


path = []
for _ in range(5):  # several passes for density
    for shape in shapes:
        start = random.randrange(len(shape))
        i = 0
        while i < len(shape):
            path.append(jitter(shape[(start + i) % len(shape)]))
            r = random.random()
            if r < 0.004:        # long chord to a random spot, then back
                path.append(jitter(random.choice(all_pts)))
            elif r < 0.012:      # skip ahead along the outline
                i += random.randint(5, 25)
            i += random.choice((1, 1, 1, 2))

img = Image.new("RGB", (W * SS, H * SS), BG)
d = ImageDraw.Draw(img)
d.line([(x * SS, y * SS) for x, y in path], fill=FG, width=2, joint="curve")
img = img.resize((W, H), Image.LANCZOS)
img.save("sats_scribble.png")
print(len(path), "points")
