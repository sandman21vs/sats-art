"""Símbolo como vazio dentro de uma rede orgânica de nós conectados.

Herda da imagem da Monero: o disco é a forma cheia, o símbolo é recortado dele.
Aqui a "tinta" é uma malha de nós: densa nas bordas (onde a Monero tinha os
feixes de rabisco), mais solta no interior, e se espalhando em poeira até as
bordas da tela, para poder recortar/dar zoom em qualquer enquadramento.

Uso: python -m motor.rede_nos <simbolo> [angulo] [seed] [lado_px] [px_por_unidade] [--sem-circulo] [--retrato]
  simbolo: um dos definidos em motor/simbolos.py (sat)
  --sem-circulo: sem o disco; a rede se adensa em volta do próprio símbolo
  --retrato: 9:16 (tela de celular) em vez de 16:9; lado_px é sempre o lado MAIOR
  padrão: ângulo do próprio símbolo, seed 21, 7680 px (8K, 16:9), 2.7 px por unidade
  (o círculo tem 300 unidades de raio -> 810 px em 8K)

Também é usado como biblioteca pelo servidor: construir_rede() monta os nós e fios
(depende só da forma e da composição) e pintar() transforma isso em imagem
(cores, brilho, fundo), em qualquer resolução, sempre com o mesmo enquadramento.
"""
import math
import os
import random
import sys
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from shapely.affinity import scale as shp_scale
from shapely.geometry import LineString, Point, box
from shapely.prepared import prep

SS = 2           # supersampling
REF_W = 7680     # largura de referência: o enquadramento é definido em px de 8K


@dataclass
class Config:
    # composição (muda a rede)
    angulo: float = 0.0
    seed: int = 21
    circulo: bool = True
    tamanho: float = 1.0        # escala do símbolo
    raio: float = 300.0         # raio do disco, em unidades
    zoom: float = 2.7           # px de 8K por unidade: maior = desenho maior na tela
    malha: float = 14.0         # distância mínima entre nós do interior (menor = mais denso)
    poeira_longe: float = 0.22  # quanto da rede sobra longe do símbolo (0 = vazio)
    halo: float = 110.0         # largura do halo em volta do círculo/símbolo
    retrato: bool = False       # 9:16 (celular) em vez de 16:9; o desenho mantém o tamanho
    # pintura (só muda a cor)
    cor: str = "#ff6600"
    brilho: float = 1.0
    fundo: float = 1.0
    multicor: bool = False      # cada parte da logo acende os nós em volta com a sua cor
    cores_arte: tuple = ()      # cor de cada parte (None = segue `cor`); veja campo_de_cores

    def chave_rede(self):
        return (self.angulo, self.seed, self.circulo, self.tamanho, self.raio,
                self.zoom, self.malha, self.poeira_longe, self.halo, self.retrato)


class Node:
    __slots__ = ("x", "y", "kind", "size", "bright")

    def __init__(self, x, y, kind, size, bright):
        self.x, self.y, self.kind, self.size, self.bright = x, y, kind, size, bright


@dataclass
class Rede:
    W: float
    H: float
    CX: float
    CY: float
    nodes: list
    edges: list         # (i, j, alpha, bend, largura_em_unidades)
    threads: list       # (a, b, bend)
    rings: list


def construir_rede(make_holes, cfg: Config) -> Rede:
    rnd = random.Random(cfg.seed)
    W, H = REF_W / cfg.zoom, REF_W * 9 / 16 / cfg.zoom   # tela em unidades
    if cfg.retrato:  # mesma área e mesma escala, só gira a moldura
        W, H = H, W
    CX, CY = W / 2, H / 2
    R = cfg.raio
    circulo = cfg.circulo

    # ---------- geometria: disco com o símbolo recortado ----------
    holes = make_holes(CX, CY, cfg.angulo)
    if cfg.tamanho != 1.0:
        holes = shp_scale(holes, cfg.tamanho, cfg.tamanho, origin=(CX, CY))
    region = Point(CX, CY).buffer(R, 256).difference(holes)
    polys = list(region.geoms) if hasattr(region, "geoms") else [region]
    rings = []
    for p in polys:
        if circulo:
            rings.append(p.exterior)
        rings += list(p.interiors)
    if not circulo:  # partes do símbolo que saem do disco também ganham contorno
        hs = list(holes.geoms) if hasattr(holes, "geoms") else [holes]
        disc = Point(CX, CY).buffer(R - 7, 256)
        rings += [h.exterior for h in hs if not disc.contains(h)]
        # e os furos de dentro do símbolo (miolo de um anel, triângulo de um A)
        rings += [i for h in hs for i in h.interiors]
    # área onde os fios podem passar: tudo menos o símbolo (e, com círculo, só dentro dele)
    inside = prep((region if circulo else box(0, 0, W, H).difference(holes)).buffer(1.5))

    # ---------- nós ----------
    nodes = []
    # borda: nós densos ao longo de cada contorno
    for ring in rings:
        pos = 0.0
        while pos < ring.length:
            p = ring.interpolate(pos)
            big = rnd.random() < 0.08
            nodes.append(Node(p.x + rnd.gauss(0, 1.6), p.y + rnd.gauss(0, 1.6), "edge",
                              rnd.uniform(1.8, 2.6) if big else rnd.uniform(0.7, 1.6),
                              1.0 if big else rnd.uniform(0.5, 0.95)))
            pos += rnd.uniform(5, 13)

    def poisson(count, mind, accept, tries=60000):
        pts, grid, cell = [], {}, mind / math.sqrt(2)
        for _ in range(tries):
            if len(pts) >= count:
                break
            x, y = rnd.uniform(0, W), rnd.uniform(0, H)
            if not accept(x, y):
                continue
            gx, gy = int(x / cell), int(y / cell)
            ok = True
            for dx in (-2, -1, 0, 1, 2):
                for dy in (-2, -1, 0, 1, 2):
                    q = grid.get((gx + dx, gy + dy))
                    if q and math.dist(q, (x, y)) < mind:
                        ok = False
            if ok:
                grid[(gx, gy)] = (x, y)
                pts.append((x, y))
        return pts

    # interior do disco: malha mais solta, um pouco mais densa perto das bordas
    region_inner = prep(region.buffer(-7))
    n_in = int(1400 * (14 / cfg.malha) ** 2 * (R / 300) ** 2)
    for x, y in poisson(n_in, cfg.malha, lambda x, y: region_inner.contains(Point(x, y))):
        d = (region.boundary if circulo else holes.boundary).distance(Point(x, y))
        p = 0.35 + 0.65 * math.exp(-d / 45)
        if not circulo:  # sem a borda do disco: vai rareando até emendar na poeira
            p = 0.12 + 0.88 * math.exp(-d / (70 * cfg.halo / 110))
        if rnd.random() < p:
            hub = rnd.random() < 0.05
            nodes.append(Node(x, y, "hub" if hub else "in",
                              rnd.uniform(2.6, 3.8) if hub else rnd.uniform(0.8, 1.6),
                              0.9 if hub else rnd.uniform(0.25, 0.6)))

    # poeira fora do círculo: densa perto dele e rareando até as bordas da tela
    tries = round(400000 * (W * H) / (REF_W / 2.7 * REF_W * 9 / 16 / 2.7))
    for x, y in poisson(20000, 26, lambda x, y: math.dist((x, y), (CX, CY)) > R + 14,
                        tries=max(tries, 20000)):
        d = math.dist((x, y), (CX, CY)) - R
        near = 0.78 if circulo else 0.3  # sem círculo, emenda na densidade do interior
        if rnd.random() < cfg.poeira_longe + near * math.exp(-d / cfg.halo):
            star = rnd.random() < 0.03  # alguns pontos mais fortes espalhados longe
            nodes.append(Node(x, y, "dust", rnd.uniform(1.6, 2.4) if star else rnd.uniform(0.6, 1.4),
                              (0.5 if star else 0.14) + 0.45 * math.exp(-d / 150)))

    # ---------- conexões ----------
    cell = 60
    grid = {}
    for idx, n in enumerate(nodes):
        grid.setdefault((int(n.x / cell), int(n.y / cell)), []).append(idx)

    def neighbours(n, radius):
        gx, gy = int(n.x / cell), int(n.y / cell)
        span = int(radius / cell) + 1
        out = []
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    m = nodes[j]
                    d = math.hypot(m.x - n.x, m.y - n.y)
                    if 0 < d < radius:
                        out.append((d, j))
        return sorted(out)

    edges = {}
    for i, n in enumerate(nodes):
        if n.kind == "dust":
            radius, k = 75, 2
        elif n.kind == "hub":
            radius, k = 110, 7
        elif n.kind == "edge":
            radius, k = 30, 3
        else:
            radius, k = 40, 3
        made = 0
        for d, j in neighbours(n, radius):
            if made >= k:
                break
            m = nodes[j]
            if circulo and (n.kind == "dust") != (m.kind == "dust"):
                if not (n.kind == "edge" or m.kind == "edge") or rnd.random() > 0.5:
                    continue
            elif (n.kind != "dust" or not circulo) and \
                    not inside.contains(LineString([(n.x, n.y), (m.x, m.y)])):
                continue  # nunca atravessa o símbolo: ele aparece pelo vazio
            key = (min(i, j), max(i, j))
            if key not in edges:
                a = 1.0 if n.kind == m.kind == "edge" else 0.32
                if "hub" in (n.kind, m.kind):
                    a = 0.45
                if n.kind == "dust" or m.kind == "dust":
                    a = 0.9 * min(n.bright, m.bright) + 0.08
                edges[key] = a * math.exp(-d / (radius * 1.4))
            made += 1

    # fios longos à la Monero: poucas curvas ligando a borda do círculo a borda do símbolo
    rim = [n for n in nodes if n.kind == "edge" and abs(math.dist((n.x, n.y), (CX, CY)) - R) < 3]
    sym = [n for n in nodes if n.kind == "edge" and abs(math.dist((n.x, n.y), (CX, CY)) - R) > 12]
    long_threads = []
    for _ in range(400 if circulo and rim and sym else 0):
        if len(long_threads) >= 22:
            break
        a, b = rnd.choice(rim), rnd.choice(sym)
        if 90 < math.dist((a.x, a.y), (b.x, b.y)) < 420 and \
                inside.contains(LineString([(a.x, a.y), (b.x, b.y)])):
            long_threads.append((a, b))

    # curvatura de cada fio (sorteada aqui para a imagem não depender da resolução)
    edge_list = []
    for (i, j), a in sorted(edges.items(), key=lambda kv: kv[1]):
        rim_edge = nodes[i].kind == nodes[j].kind == "edge"
        edge_list.append((i, j, a, rnd.gauss(0, 0.04 if rim_edge else 0.14),
                          1.0 if rim_edge else 0.67))
    threads = [(a, b, rnd.gauss(0, 0.18)) for a, b in long_threads]
    return Rede(W, H, CX, CY, nodes, edge_list, threads, rings)


def _hex(c):
    c = c.lstrip("#")
    return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], float)


def dimensoes(lado, retrato=False):
    """(largura, altura) em px para `lado` = o lado maior da imagem."""
    curto = lado * 9 // 16
    return (curto, lado) if retrato else (lado, curto)


PASSO_CAMPO = 6     # unidades por célula do campo de cores
AMOSTRAS_CAMPO = 3  # subamostras por lado em cada célula (pega traços finos da logo)


def _caixa(a, r, eixo):
    """Média móvel de raio r (em células) ao longo de um eixo, com bordas em zero."""
    if r < 1:
        return a
    a = np.moveaxis(a, eixo, -1)
    c = np.cumsum(np.pad(a, [(0, 0)] * (a.ndim - 1) + [(r + 1, r)]), axis=-1)
    return np.moveaxis((c[..., 2 * r + 1:] - c[..., :-2 * r - 1]) / (2 * r + 1), -1, eixo)


def _borrar(a, raio):
    """Quase gaussiana: três médias móveis em cada eixo (custo não depende do raio)."""
    r = max(1, int(round(raio / 3)))
    for _ in range(3):
        a = _caixa(_caixa(a, r, 0), r, 1)
    return a


def campo_de_cores(rede: Rede, cfg: Config, rotulos_na_tela, cores):
    """Cor da rede em cada ponto da tela (baixa resolução, uint8 RGB): cada parte da logo pinta
    os nós em volta dela com a sua cor, e as cores se misturam suavemente entre as partes.

    rotulos_na_tela(xs, ys) -> índice da parte da logo em cada ponto (em unidades do motor;
    255 = fora do símbolo). cores: [(r, g, b)] por índice. Perto do símbolo vale a parte mais
    próxima; longe, cada vez mais a mistura das partes em escalas maiores."""
    gw, gh = math.ceil(rede.W / PASSO_CAMPO), math.ceil(rede.H / PASSO_CAMPO)
    k = AMOSTRAS_CAMPO
    sub = (np.arange(k) + 0.5) / k
    xs = ((np.arange(gw)[:, None] + sub[None]).reshape(-1) * PASSO_CAMPO)[None, :]
    ys = ((np.arange(gh)[:, None] + sub[None]).reshape(-1) * PASSO_CAMPO)[:, None]
    rot = rotulos_na_tela(np.broadcast_to(xs, (gh * k, gw * k)), np.broadcast_to(ys, (gh * k, gw * k)))
    n = len(cores)
    if n == 0:
        return None
    pesos = []
    for i in range(n):
        cobre = (rot == i).reshape(gh, k, gw, k).mean((1, 3))  # fração da célula nesta parte
        if not cobre.any():
            pesos.append(None)
            continue
        area = cobre.sum()
        # escalas de 30, 120 e 480 unidades: a menor manda onde alcança, as outras completam
        pesos.append(_borrar(cobre, 30 / PASSO_CAMPO) + 1e-3 * _borrar(cobre, 120 / PASSO_CAMPO)
                     + 1e-6 * _borrar(cobre, 480 / PASSO_CAMPO) + 1e-12 * area)
    usados = [i for i in range(n) if pesos[i] is not None]
    if not usados:
        return None
    total = sum(pesos[i] for i in usados)
    rgb = sum(pesos[i][..., None] * np.array(cores[i], np.float64) for i in usados) / total[..., None]
    return np.clip(np.round(rgb), 0, 255).astype(np.uint8)


def pintar(rede: Rede, cfg: Config, lado=REF_W, campo=None) -> Image.Image:
    """`lado` = lado MAIOR da imagem, em px (a largura no paisagem, a altura no retrato).
    `campo` (de campo_de_cores) troca a cor única `cfg.cor` por uma cor em cada ponto."""
    out_w, out_h = dimensoes(lado, cfg.retrato)
    U = cfg.zoom * (lado / REF_W)  # px de saída por unidade (igual nos dois formatos)
    S = U * SS                     # px de desenho por unidade

    base = _hex(cfg.cor)
    ORANGE = base
    HOT = np.round(base * 0.75 + np.array([255, 235, 160]) * 0.25)
    CORE = np.array([255, 236, 214], float)

    def curve(ax, ay, bx, by, bend):
        mx, my = (ax + bx) / 2, (ay + by) / 2
        nx, ny = -(by - ay), bx - ax
        cx, cy = mx + nx * bend, my + ny * bend
        pts = []
        for s in range(13):
            t = s / 12
            x = (1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t * t * bx
            y = (1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t * t * by
            pts.append((x * S, y * S))
        return pts

    nodes = rede.nodes
    DW, DH = out_w * SS, out_h * SS
    lw = lambda u: max(1, round(u * S))  # noqa: E731  largura em unidades -> px de desenho
    lines = Image.new("L", (DW, DH), 0)
    dl = ImageDraw.Draw(lines)
    for i, j, a, bend, width in rede.edges:
        n, m = nodes[i], nodes[j]
        dl.line(curve(n.x, n.y, m.x, m.y, bend), fill=int(255 * min(1, a)), width=lw(width))
    for a, b, bend in rede.threads:
        dl.line(curve(a.x, a.y, b.x, b.y, bend), fill=90, width=lw(0.67))
    for ring in rede.rings:
        dl.line([(x * S, y * S) for x, y in ring.coords], fill=150, width=lw(0.67))

    dots = Image.new("L", (DW, DH), 0)
    dd = ImageDraw.Draw(dots)
    for n in nodes:
        r = n.size * S
        dd.ellipse((n.x * S - r, n.y * S - r, n.x * S + r, n.y * S + r), fill=int(255 * n.bright))

    lines = lines.resize((out_w, out_h), Image.LANCZOS)
    dots = dots.resize((out_w, out_h), Image.LANCZOS)
    hubs = Image.new("L", (out_w, out_h), 0)
    dh = ImageDraw.Draw(hubs)
    for n in nodes:
        if n.kind == "hub":
            dh.ellipse(((n.x - 5) * U, (n.y - 5) * U, (n.x + 5) * U, (n.y + 5) * U), fill=255)

    def f32(im):
        return np.asarray(im, np.float32) / 255

    Lc, Dc = f32(lines), f32(dots)
    G1 = f32(dots.filter(ImageFilter.GaussianBlur(4 * U)))
    G2 = f32(lines.filter(ImageFilter.GaussianBlur(14 * U)))
    GH = f32(hubs.filter(ImageFilter.GaussianBlur(16 * U)))
    del lines, dots, hubs

    yy, xx = np.ogrid[0:out_h, 0:out_w]
    glow = np.exp(-((np.hypot(xx - rede.CX * U, yy - rede.CY * U) / U) / 520) ** 2).astype(np.float32)
    grain = np.random.default_rng(7).normal(0, 2.2, (out_h, out_w)).astype(np.float32)

    b = cfg.brilho
    out = np.empty((out_h, out_w, 3), np.uint8)
    if campo is not None:  # a cor muda de ponto a ponto: HOT = 3/4 da cor + 1/4 de branco quente
        campo = Image.fromarray(campo, "RGB").resize((out_w, out_h), Image.BILINEAR)
        tinta = Lc * 1.5 + G1 * 1.65 + G2 * 1.6 + GH * 1.8
    for c in range(3):
        if campo is not None:
            cor = np.asarray(campo.getchannel(c), np.float32)
            light = (tinta * cor + Dc * 1.1 * CORE[c] + G1 * 0.55 * (255, 235, 160)[c]) * b
            del cor
        else:
            light = (Lc * 1.5 * ORANGE[c] + Dc * 1.1 * CORE[c] + G1 * 2.2 * HOT[c]
                     + G2 * 1.6 * ORANGE[c] + GH * 1.8 * ORANGE[c]) * b
        bg = ((13, 12, 12)[c] + (11, 6, 3)[c] * glow) * cfg.fundo
        ch = bg + 255 * (1 - np.exp(-light / 255 * 1.4)) + grain
        out[..., c] = np.clip(ch, 0, 255).astype(np.uint8)
        del light, ch
    return Image.fromarray(out)


def main():
    from motor.simbolos import SIMBOLOS

    circulo = "--sem-circulo" not in sys.argv
    retrato = "--retrato" in sys.argv
    args = [a for a in sys.argv if a not in ("--sem-circulo", "--retrato")]
    simbolo = args[1] if len(args) > 1 else "sat"
    make_holes, default_angle = SIMBOLOS[simbolo]
    cfg = Config(angulo=float(args[2]) if len(args) > 2 else default_angle,
                 seed=int(args[3]) if len(args) > 3 else 21,
                 circulo=circulo, retrato=retrato,
                 zoom=float(args[5]) if len(args) > 5 else 2.7)
    lado = int(args[4]) if len(args) > 4 else REF_W

    rede = construir_rede(make_holes, cfg)
    img = pintar(rede, cfg, lado)
    os.makedirs("output", exist_ok=True)
    w, h = dimensoes(lado, retrato)
    name = f"output/{simbolo}_nodes{'' if circulo else '_sem_circulo'}_{w}x{h}.png"
    img.save(name)
    print(name, len(rede.nodes), "nós,", len(rede.edges), "conexões")


if __name__ == "__main__":
    main()
