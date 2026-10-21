"""Transforma uma imagem (print, logo, PNG transparente) na forma do símbolo.

O fundo é detectado pela borda da imagem: tudo que difere dela além do limiar
vira símbolo. PNG com transparência usa o canal alfa direto.

Como a imagem vem de qualquer pessoa, tudo aqui tem limite: pixels antes de
decodificar, tamanho depois de reduzir e complexidade da forma vetorizada.
"""
import io
import warnings

import numpy as np
from PIL import Image, ImageOps
from shapely.affinity import rotate, scale, translate
from shapely.geometry import box
from shapely.ops import unary_union

MAX_LADO = 360            # resolução da máscara usada para vetorizar
MAX_PIXELS = 25_000_000   # recusa antes de decodificar (ex.: 5000x5000)
MAX_LADO_GUARDADO = 1024  # a imagem guardada no servidor é reduzida a isso
MAX_TRECHOS = 6000        # trechos horizontais na máscara (proxy de complexidade)
MAX_PARTES = 80
MAX_VERTICES = 6000
MAX_PERIMETRO = 40        # com o maior lado = 1 (um logo comum fica entre 3 e 15)

Image.MAX_IMAGE_PIXELS = MAX_PIXELS * 2  # rede de segurança do próprio Pillow


class ImagemInvalida(ValueError):
    pass


def reduzir(dados: bytes) -> bytes:
    """Confere a imagem enviada e devolve uma versão PNG pequena (maior lado ≤ 1024)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(dados))
            w, h = im.size
            if w * h > MAX_PIXELS or max(w, h) > 10000 or min(w, h) < 1:
                raise ImagemInvalida("Imagem grande demais em pixels (máximo 25 megapixels).")
            im.draft("RGBA", (MAX_LADO_GUARDADO, MAX_LADO_GUARDADO))  # JPEG já decodifica reduzido
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MAX_LADO_GUARDADO, MAX_LADO_GUARDADO))
            im = im.convert("RGBA")
    except ImagemInvalida:
        raise
    except Exception as e:  # formato desconhecido, arquivo truncado, bomba de descompressão...
        raise ImagemInvalida("Não consegui abrir esse arquivo como imagem.") from e
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def mascara(dados: bytes, limiar=60, inverter=False) -> np.ndarray:
    im = Image.open(io.BytesIO(dados))
    if im.width * im.height > MAX_LADO_GUARDADO ** 2 * 1.05:  # só aceita o que passou por reduzir()
        raise ImagemInvalida("Imagem não reduzida.")
    rgba = np.asarray(im.convert("RGBA"), np.float32)
    alpha = rgba[..., 3]
    borda_alpha = np.concatenate([alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]])
    if borda_alpha.mean() < 250:  # PNG transparente: o que é opaco é o símbolo
        m = alpha > 128
    else:
        lum = rgba[..., 0] * 0.299 + rgba[..., 1] * 0.587 + rgba[..., 2] * 0.114
        borda = np.concatenate([lum[0], lum[-1], lum[:, 0], lum[:, -1]])
        m = np.abs(lum - np.median(borda)) > limiar
    return ~m if inverter else m


def forma_normalizada(dados: bytes, limiar=60, inverter=False):
    """Geometria do símbolo centrada em (0, 0), com o maior lado = 1."""
    m = mascara(dados, limiar, inverter)
    ys, xs = np.nonzero(m)
    if len(xs) == 0:
        raise ImagemInvalida("Não encontrei nenhum símbolo na imagem. Tente mudar o limiar ou inverter.")
    m = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = m.shape
    f = MAX_LADO / max(h, w)
    if f < 1:  # reduz suavizando, depois volta a binarizar
        small = Image.fromarray(m.astype(np.uint8) * 255).resize(
            (max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS)
        m = np.asarray(small) > 127
        h, w = m.shape

    complexa = ImagemInvalida("Essa imagem tem detalhes demais. Use um logo mais simples "
                              "ou ajuste a sensibilidade.")
    trechos = []
    for y in range(h):
        row = np.concatenate([[False], m[y], [False]]).astype(np.int8)
        d = np.diff(row)
        trechos += [(x0, y, x1) for x0, x1 in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0])]
        if len(trechos) > MAX_TRECHOS:
            raise complexa
    if not trechos:
        raise ImagemInvalida("Não encontrei nenhum símbolo na imagem. Tente mudar o limiar ou inverter.")
    g = unary_union([box(x0, y, x1, y + 1) for x0, y, x1 in trechos])
    g = g.buffer(0.45, join_style=2).buffer(-0.45, join_style=2).simplify(0.35)

    # descarta sujeirinhas
    partes = [p for p in (g.geoms if hasattr(g, "geoms") else [g]) if not p.is_empty]
    total = sum(p.area for p in partes)
    partes = [p for p in partes if p.area > total * 0.002]
    if not partes:
        raise ImagemInvalida("Não encontrei um símbolo nítido na imagem. Tente mudar o limiar ou inverter.")
    if len(partes) > MAX_PARTES:
        raise complexa
    g = unary_union(partes)

    minx, miny, maxx, maxy = g.bounds
    lado = max(maxx - minx, maxy - miny)
    if not lado > 0:
        raise ImagemInvalida("Não encontrei um símbolo nítido na imagem.")
    g = scale(translate(g, -(minx + maxx) / 2, -(miny + maxy) / 2), 1 / lado, 1 / lado, origin=(0, 0))
    vertices = sum(len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors)
                   for p in (g.geoms if hasattr(g, "geoms") else [g]))
    if vertices > MAX_VERTICES or g.length > MAX_PERIMETRO:
        raise complexa
    return g


def holes_de_forma(forma, altura=440):
    """make_holes(cx, cy, angulo) para o motor, com o maior lado = `altura` unidades."""
    def make_holes(cx, cy, angle):
        g = translate(scale(forma, altura, altura, origin=(0, 0)), cx, cy)
        return rotate(g, -angle, origin=(cx, cy)) if angle else g
    return make_holes
