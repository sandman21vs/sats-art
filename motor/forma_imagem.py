"""Transforma uma imagem (print, logo, PNG transparente) na forma do símbolo.

O fundo é detectado pela borda da imagem: tudo que difere dela em cor além do limiar
vira símbolo. PNG com transparência usa o canal alfa direto. As cores dominantes do
símbolo são agrupadas para a pessoa poder tirar uma cor do símbolo e, na arte, cada
parte da logo acender os nós em volta com a sua cor.

Como a imagem vem de qualquer pessoa, tudo aqui tem limite: pixels antes de
decodificar, tamanho depois de reduzir, amostra e rodadas do agrupamento de cores e
complexidade da forma vetorizada.
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

# ---------- cor ----------
MAX_CORES = 6            # cores dominantes no máximo (os "chips" da página)
AMOSTRA_CORES = 20_000   # pixels usados para achar as cores (o resto só é rotulado)
ITERACOES_KMEANS = 12
ESCALA_DE = 2.55         # ΔE (0 a 100) na escala do limiar (0 a 255, como a luminância)
LIMIAR_PADRAO = 60

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


# ---------- cor ----------
def _lab(rgb):
    """sRGB (0 a 255) -> CIELAB (D65), float32. L vai de 0 a 100."""
    c = np.asarray(rgb, np.float32) / 255
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = c @ np.array([[0.4124, 0.2126, 0.0193], [0.3576, 0.7152, 0.1192],
                        [0.1805, 0.0722, 0.9505]], np.float32)
    xyz /= np.array([0.9505, 1.0, 1.089], np.float32)
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], -1).astype(np.float32)


def _lab_para_rgb(lab):
    lab = np.asarray(lab, np.float64).reshape(-1, 3)
    fy = (lab[:, 0] + 16) / 116
    f = np.stack([fy + lab[:, 1] / 500, fy, fy - lab[:, 2] / 200], -1)
    xyz = np.where(f > 0.206893, f ** 3, (f - 16 / 116) / 7.787) * np.array([0.9505, 1.0, 1.089])
    m = np.array([[3.2406, -0.9689, 0.0557], [-1.5372, 1.8758, -0.2040], [-0.4986, 0.0415, 1.0570]])
    c = np.clip(xyz @ m, 0, 1)
    c = np.where(c > 0.0031308, 1.055 * c ** (1 / 2.4) - 0.055, 12.92 * c)
    return np.clip(np.round(c * 255), 0, 255).astype(int)


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def _kmeans(x, k, rng):
    """k-means simples e limitado: k-means++ e no máximo ITERACOES_KMEANS rodadas."""
    centros = [x[rng.integers(len(x))]]
    d2 = ((x - centros[0]) ** 2).sum(1)
    for _ in range(1, k):
        if d2.sum() <= 1e-6:  # menos cores distintas que k
            break
        centros.append(x[rng.choice(len(x), p=d2 / d2.sum())])
        d2 = np.minimum(d2, ((x - centros[-1]) ** 2).sum(1))
    c = np.array(centros, np.float32)
    for _ in range(ITERACOES_KMEANS):
        rot = ((x[:, None] - c[None]) ** 2).sum(2).argmin(1)
        novo = np.array([x[rot == i].mean(0) if (rot == i).any() else c[i] for i in range(len(c))], np.float32)
        parou = np.abs(novo - c).max() < 0.5
        c = novo
        if parou:
            break
    rot = ((x[:, None] - c[None]) ** 2).sum(2).argmin(1)
    return c, np.bincount(rot, minlength=len(c)).astype(np.float64)


def _agrupar(lab, rng):
    """Cores dominantes (em Lab), da maior para a menor: k-means com folga, depois junta as
    parecidas (ΔE < 14) até sobrarem no máximo MAX_CORES e descarta as de menos de 0,3% (um
    texto pequeno ao lado do símbolo ainda vira uma cor própria)."""
    c, n = _kmeans(lab, min(MAX_CORES + 2, len(lab)), rng)
    c, n = list(c), list(n)
    while len(c) > 1:
        dist, i, j = min((float(np.linalg.norm(c[i] - c[j])), i, j)
                         for i in range(len(c)) for j in range(i + 1, len(c)))
        if dist >= 14 and len(c) <= MAX_CORES:
            break
        c[i] = (c[i] * n[i] + c[j] * n[j]) / (n[i] + n[j])
        n[i] += n[j]
        del c[j], n[j]
    total = sum(n)
    ordem = sorted(range(len(c)), key=lambda i: -n[i])
    ficam = [i for i in ordem if n[i] >= total * 0.003] or ordem[:1]
    return np.array([c[i] for i in ficam], np.float32)


def cor_para_arte(rgb):
    """Cor de brilho sugerida para uma cor da logo: a mesma tonalidade, acesa o bastante para
    brilhar no fundo escuro. Cores neutras (preto, branco, cinza) devolvem None: seguem a cor
    escolhida no passo 2."""
    lab = _lab(np.array(rgb, np.float32).reshape(1, 3))[0]
    if float(np.hypot(lab[1], lab[2])) < 14:
        return None
    r, g, b = (v / 255 for v in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return None
    s = (mx - mn) / mx
    h = (((g - b) / (mx - mn)) % 6 if mx == r else (b - r) / (mx - mn) + 2 if mx == g
         else (r - g) / (mx - mn) + 4) / 6
    s, v = max(s, 0.55), max(mx, 0.9)
    i, f = int(h * 6) % 6, h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    return _hex(np.round(np.array([(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]) * 255))


class Analise:
    """O que se sabe de uma imagem, calculado UMA vez por imagem (depois, mudar a sensibilidade
    ou as cores ligadas é só comparar arrays).

    dist: uint8, distância de cada pixel ao fundo na escala do limiar (ou o alfa, se o fundo é
    transparente); rotulos: uint8, a cor dominante mais próxima de cada pixel;
    paleta: [(r, g, b)] da maior para a menor; partes: fração do símbolo de cada cor."""

    def __init__(self, dist, rotulos, alfa, paleta, partes, fundo_irregular=False):
        self.dist, self.rotulos, self.alfa, self.paleta, self.partes = dist, rotulos, alfa, paleta, partes
        self.fundo_irregular = fundo_irregular  # sugere "Remover fundo" (degradê, vinheta, borda mista)

    def cores(self):
        """Para a página: a cor real, a fração da logo e a cor de brilho sugerida de cada uma."""
        return [{"hex": _hex(c), "parte": round(float(p), 3), "arte": cor_para_arte(c)}
                for c, p in zip(self.paleta, self.partes)]


def _rgba_reduzida(dados):
    im = Image.open(io.BytesIO(dados))
    if im.width * im.height > MAX_LADO_GUARDADO ** 2 * 1.05:  # só aceita o que passou por reduzir()
        raise ImagemInvalida("Imagem não reduzida.")
    return np.asarray(im.convert("RGBA"), np.float32)


def _borda(a):
    return np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])


# ---------- fundo que não é liso (degradê, vinheta, sombra, logo encostando na borda) ----------
# "Remover fundo" troca a cor única da borda por uma SUPERFÍCIE suave (polinômio de grau 2 em
# x e y, por canal Lab) ajustada aos pixels que com certeza são fundo: a borda, os pontos que a
# pessoa tocou e, depois, o que está ligado à borda e cola no modelo (preenchimento a partir da
# borda numa grade reduzida). Cada pixel é comparado com o fundo previsto NAQUELE ponto. Sombras
# suaves (mais escuras que o fundo, com o mesmo tom) contam como fundo. Tudo roda numa grade de
# no máximo GRADE_FUNDO células por lado: custo pequeno e previsível.
GRADE_FUNDO = 128         # lado máximo da grade reduzida usada para ajustar o fundo
MAX_PONTOS_FUNDO = 8      # pontos "isto é fundo" tocados pela pessoa
RODADAS_FUNDO = 3         # reajustes da superfície
MAX_PASSOS_PREENCHER = 300  # limite do preenchimento a partir da borda (na grade)
TOL_FUNDO = 6.0           # ΔE até a superfície para uma célula contar como fundo certo
SOMBRA_TOM = 7.0          # ΔE de tom (já descontado o escurecimento) aceito numa sombra
SOMBRA_ESCURECE = 0.3     # sombra escurece até 30% sem virar símbolo


def _grade(lab):
    """Lab em blocos (média), com no máximo GRADE_FUNDO células por lado. Devolve (grade, s)."""
    h, w = lab.shape[:2]
    s = max(1, -(-max(h, w) // GRADE_FUNDO))
    gh, gw = max(1, h // s), max(1, w // s)
    g = lab[:gh * s, :gw * s].reshape(gh, s, gw, s, 3).mean((1, 3))
    return g.astype(np.float32), s


def _termos(x, y):
    """Termos do polinômio (1, x, y, x², xy, y²), com x e y entre -1 e 1."""
    return [np.ones_like(x * y), x + 0 * y, y + 0 * x, x * x + 0 * y, x * y, y * y + 0 * x]


def _ajustar(xs, ys, cores, pesos):
    """Mínimos quadrados ponderados (com uma leve regularização) dos 6 termos por canal."""
    a = np.stack(_termos(xs, ys), 1).astype(np.float64) * pesos[:, None]
    b = cores.astype(np.float64) * pesos[:, None]
    reg = np.diag([0, 1, 1, 1, 1, 1]) * 1e-3 * float((pesos ** 2).sum())
    return np.linalg.solve(a.T @ a + reg, a.T @ b)  # 6 x 3


def _superficie(coef, xs, ys):
    t = _termos(xs, ys)
    return np.stack([sum(c * ti for c, ti in zip(coef[:, k], t)) for k in range(3)], -1).astype(np.float32)


def _cor_mais_comum(cores):
    """A cor que mais se repete (vizinhas com ΔE < 8), não a mediana: com a logo encostando
    em parte da borda, a mediana mistura logo e fundo."""
    if len(cores) > 400:
        cores = cores[np.linspace(0, len(cores) - 1, 400).astype(int)]
    perto = ((cores[:, None] - cores[None]) ** 2).sum(-1) < 64
    i = int(perto.sum(1).argmax())
    return cores[perto[i]].mean(0)


def _cor_no_ponto(lab, u, v):
    h, w = lab.shape[:2]
    x, y = min(w - 1, int(u * w)), min(h - 1, int(v * h))
    return lab[max(0, y - 2):y + 3, max(0, x - 2):x + 3].reshape(-1, 3).mean(0)


def modelo_fundo(lab, pontos=()):
    """Ajusta a superfície do fundo. Devolve (coef 6x3, irregular): `irregular` diz que um fundo
    de cor única não serve (degradê, vinheta, ou a borda não tem uma cor dominante clara)."""
    h, w = lab.shape[:2]
    g, s = _grade(lab)
    gh, gw = g.shape[:2]
    gy = ((np.arange(gh) + 0.5) * s / h * 2 - 1).astype(np.float32)[:, None]
    gx = ((np.arange(gw) + 0.5) * s / w * 2 - 1).astype(np.float32)[None, :]
    borda = np.zeros((gh, gw), bool)
    borda[0], borda[-1], borda[:, 0], borda[:, -1] = True, True, True, True
    marcados = np.zeros((gh, gw), bool)
    for u, v in pontos:
        cy, cx = min(gh - 1, int(v * gh)), min(gw - 1, int(u * gw))
        marcados[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2] = True

    comum = _cor_mais_comum(g[borda])
    mediana = np.median(g[borda], 0)
    amostra = (borda & (((g - comum) ** 2).sum(-1) < 144)) | marcados  # ΔE < 12 da cor dominante
    if amostra.sum() < 8:
        amostra = borda | marcados
    for _ in range(RODADAS_FUNDO):
        peso = np.where(marcados, 4.0, 1.0)[amostra]
        yy, xx = np.broadcast_arrays(gy, gx)
        coef = _ajustar(xx[amostra], yy[amostra], g[amostra], peso)
        res = np.sqrt(((g - _superficie(coef, gx, gy)) ** 2).sum(-1))
        # fundo certo: liga-se à borda (ou a um ponto marcado) por células que colam no modelo
        cola = res < TOL_FUNDO
        cheio = cola & (borda | marcados)
        for _ in range(MAX_PASSOS_PREENCHER):
            viz = cheio.copy()
            viz[1:] |= cheio[:-1]; viz[:-1] |= cheio[1:]; viz[:, 1:] |= cheio[:, :-1]; viz[:, :-1] |= cheio[:, 1:]
            viz &= cola
            if (viz == cheio).all():
                break
            cheio = viz
        if cheio.sum() < 8:
            break
        amostra = cheio | marcados
    sup = _superficie(coef, gx, gy)
    variacao = float(np.sqrt(((sup.reshape(-1, 3).max(0) - sup.reshape(-1, 3).min(0)) ** 2).sum()))
    irregular = variacao > 10 or float(np.sqrt(((comum - mediana) ** 2).sum())) > 10
    return coef, irregular


def _distancia_superficie(lab, coef):
    """Distância de cada pixel ao fundo previsto naquele ponto (escala do limiar), com as
    sombras suaves descontadas."""
    h, w = lab.shape[:2]
    ys = ((np.arange(h) + 0.5) / h * 2 - 1).astype(np.float32)[:, None]
    xs = ((np.arange(w) + 0.5) / w * 2 - 1).astype(np.float32)[None, :]
    fundo = _superficie(coef, xs, ys)
    dl = lab[..., 0] - fundo[..., 0]
    de = np.sqrt(dl ** 2 + ((lab[..., 1:] - fundo[..., 1:]) ** 2).sum(-1))
    lf = np.maximum(fundo[..., 0], 1.0)
    k = np.clip(lab[..., 0] / lf, 0, 1)[..., None]
    tom = np.sqrt(((lab[..., 1:] - k * fundo[..., 1:]) ** 2).sum(-1))  # cor, descontado o escurecimento
    escurece = np.clip(-dl / lf, 0, 1)
    sombra = (dl < 0) & (tom < SOMBRA_TOM)
    d_sombra = np.maximum(tom, np.clip((escurece - SOMBRA_ESCURECE) / (1 - SOMBRA_ESCURECE), 0, 1) * 100)
    return np.where(sombra, d_sombra, de) * ESCALA_DE


def _pontos(pontos):
    """Pontos (u, v) entre 0 e 1, no máximo MAX_PONTOS_FUNDO (o servidor já valida; aqui só
    não confia)."""
    ok = []
    for p in tuple(pontos)[:MAX_PONTOS_FUNDO]:
        u, v = float(p[0]), float(p[1])
        if np.isfinite(u) and np.isfinite(v) and 0 <= u <= 1 and 0 <= v <= 1:
            ok.append((u, v))
    return tuple(ok)


def analisar(dados: bytes, remover_fundo=False, pontos=()) -> Analise:
    """Separa símbolo e fundo pela COR (distância perceptual ΔE, em Lab, até a cor do fundo
    estimada pela borda) e acha as cores dominantes do símbolo.

    A distância é max(|Δ luminância|, ΔE × 2,55): um logo preto e branco dá o mesmo resultado
    de antes (ou pega um pouco mais de borda), e partes amarelas ou azul-claras sobre branco,
    que têm luminância parecida com a do fundo, passam a contar.

    `remover_fundo` compara cada pixel com uma superfície suave (veja modelo_fundo) em vez de
    uma cor só; `pontos` [(u, v) de 0 a 1] são lugares que a pessoa disse que são fundo: a cor
    deles também vira fundo. PNG transparente ignora os dois (o alfa já diz tudo)."""
    pontos = _pontos(pontos)
    rgba = _rgba_reduzida(dados)
    rgb, alpha = rgba[..., :3], rgba[..., 3]
    lab = _lab(rgb)
    alfa = bool(_borda(alpha).mean() < 250)
    irregular = False
    if alfa:  # PNG transparente: o que é opaco é o símbolo
        dist = alpha.astype(np.uint8)
        nucleo = alpha > 250
    else:
        coef, irregular = modelo_fundo(lab, pontos)
        if remover_fundo:
            d = _distancia_superficie(lab, coef)
        else:
            lum = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
            fundo_lab = np.median(_borda(lab), axis=0)
            de = np.sqrt(((lab - fundo_lab) ** 2).sum(-1))
            d = np.maximum(np.abs(lum - np.median(_borda(lum))), de * ESCALA_DE)
            del lum, de
        for u, v in pontos:  # a cor de cada ponto tocado também é fundo
            d = np.minimum(d, np.sqrt(((lab - _cor_no_ponto(lab, u, v)) ** 2).sum(-1)) * ESCALA_DE)
        dist = np.clip(d, 0, 255).astype(np.uint8)
        nucleo = d > LIMIAR_PADRAO
        del d
    del rgba, rgb, alpha

    # as cores saem só do "miolo" de cada parte: sem o antialias das bordas, que mistura cores
    h, w = nucleo.shape
    liso = nucleo.copy()
    for dy, dx in ((0, 1), (1, 0)):
        dif = ((lab[dy:, dx:] - lab[:h - dy, :w - dx]) ** 2).sum(-1) > 144  # ΔE > 12 com o vizinho (folga para o ruído do JPEG)
        liso[dy:, dx:] &= ~dif
        liso[:h - dy, :w - dx] &= ~dif
    pontos = lab[liso] if liso.sum() >= 50 else lab[nucleo]
    if len(pontos) == 0:  # nada se destaca do fundo: uma "cor" só, a do pixel mais distante
        pontos = lab.reshape(-1, 3)[[int(np.argmax(dist))]]
    rng = np.random.default_rng(21)  # sempre as mesmas cores, na mesma ordem, para a mesma imagem
    if len(pontos) > AMOSTRA_CORES:
        pontos = pontos[rng.choice(len(pontos), AMOSTRA_CORES, replace=False)]
    centros = _agrupar(pontos, rng)

    rotulos = np.zeros((h, w), np.uint8)
    melhor = np.full((h, w), np.inf, np.float32)
    for i, c in enumerate(centros):
        d2 = ((lab - c) ** 2).sum(-1)
        menor = d2 < melhor
        rotulos[menor], melhor[menor] = i, d2[menor]
    del lab, melhor
    n = np.bincount(rotulos[nucleo], minlength=len(centros)).astype(float)
    partes = list(n / n.sum()) if n.sum() else [1 / len(centros)] * len(centros)
    paleta = [tuple(int(v) for v in c) for c in _lab_para_rgb(centros)]
    return Analise(dist, rotulos, alfa, paleta, partes, irregular)


def mascara(dados, limiar=LIMIAR_PADRAO, inverter=False, cores_off=()) -> np.ndarray:
    """Símbolo = pixels distantes do fundo cuja cor dominante não foi desligada.
    `dados` pode ser a imagem (PNG reduzido) ou uma Analise já pronta."""
    a = dados if isinstance(dados, Analise) else analisar(dados)
    m = a.dist > (128 if a.alfa else limiar)
    desligadas = [i for i in cores_off if 0 <= i < len(a.paleta)]
    if desligadas:
        m &= ~np.isin(a.rotulos, desligadas)
    return ~m if inverter else m


class Regioes:
    """Em que parte (cor) da logo cai cada ponto da forma normalizada.

    rotulos: uint8 na resolução da forma vetorizada, FORA = fora do símbolo. Guarda também o
    caminho de volta até os pixels da imagem guardada (recorte e escala), para a miniatura."""

    FORA = 255

    def __init__(self, rotulos, cx, cy, lado, x0, y0, fator, n_cores):
        self.rotulos, self.cx, self.cy, self.lado = rotulos, cx, cy, lado
        self.x0, self.y0, self.fator, self.n_cores = x0, y0, fator, n_cores

    def em(self, u, v):
        """Rótulos nos pontos (u, v) da forma normalizada (arrays numpy)."""
        h, w = self.rotulos.shape
        px = np.floor(u * self.lado + self.cx).astype(np.int64)
        py = np.floor(v * self.lado + self.cy).astype(np.int64)
        dentro = (px >= 0) & (px < w) & (py >= 0) & (py < h)
        out = np.full(np.shape(u), self.FORA, np.uint8)
        out[dentro] = self.rotulos[py[dentro], px[dentro]]
        return out


def forma_normalizada(dados, limiar=LIMIAR_PADRAO, inverter=False, cores_off=(), com_regioes=False):
    """Geometria do símbolo centrada em (0, 0), com o maior lado = 1.
    `dados` pode ser a imagem ou uma Analise. Com `com_regioes`, devolve (forma, Regioes)."""
    a = dados if isinstance(dados, Analise) else analisar(dados)
    m = mascara(a, limiar, inverter, cores_off)
    ys, xs = np.nonzero(m)
    if len(xs) == 0:
        raise ImagemInvalida("Não encontrei nenhum símbolo na imagem. Tente mudar o limiar ou inverter.")
    x0, y0 = int(xs.min()), int(ys.min())
    m = m[y0:ys.max() + 1, x0:xs.max() + 1]
    rot = a.rotulos[y0:ys.max() + 1, x0:xs.max() + 1]
    h, w = m.shape
    f = MAX_LADO / max(h, w)
    fator = 1.0
    if f < 1:  # reduz suavizando, depois volta a binarizar
        tam = (max(1, round(w * f)), max(1, round(h * f)))
        small = Image.fromarray(m.astype(np.uint8) * 255).resize(tam, Image.LANCZOS)
        m = np.asarray(small) > 127
        rot = np.asarray(Image.fromarray(rot).resize(tam, Image.NEAREST))
        fator = tam[0] / w
        h, w = m.shape

    complexa = ImagemInvalida("Essa imagem tem detalhes demais. Use um logo mais simples "
                              "ou ajuste a sensibilidade.")
    trechos = []
    for y in range(h):
        row = np.concatenate([[False], m[y], [False]]).astype(np.int8)
        d = np.diff(row)
        trechos += [(xa, y, xb) for xa, xb in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0])]
        if len(trechos) > MAX_TRECHOS:
            raise complexa
    if not trechos:
        raise ImagemInvalida("Não encontrei nenhum símbolo na imagem. Tente mudar o limiar ou inverter.")
    g = unary_union([box(xa, y, xb, y + 1) for xa, y, xb in trechos])
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
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    g = scale(translate(g, -cx, -cy), 1 / lado, 1 / lado, origin=(0, 0))
    vertices = sum(len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors)
                   for p in (g.geoms if hasattr(g, "geoms") else [g]))
    if vertices > MAX_VERTICES or g.length > MAX_PERIMETRO:
        raise complexa
    if not com_regioes:
        return g
    rot = np.where(m, rot, Regioes.FORA).astype(np.uint8)
    return g, Regioes(rot, cx, cy, lado, x0, y0, fator, len(a.paleta))


def holes_de_forma(forma, altura=440):
    """make_holes(cx, cy, angulo) para o motor, com o maior lado = `altura` unidades."""
    def make_holes(cx, cy, angle):
        g = translate(scale(forma, altura, altura, origin=(0, 0)), cx, cy)
        return rotate(g, -angle, origin=(cx, cy)) if angle else g
    return make_holes


def regioes_na_tela(regioes, xs, ys, cx, cy, angulo=0.0, tamanho=1.0, altura=440):
    """Rótulos da logo nos pontos (xs, ys) da tela, em unidades do motor: o caminho inverso de
    holes_de_forma (e da escala `tamanho` que construir_rede aplica em volta do centro)."""
    qx, qy = (xs - cx) / tamanho, (ys - cy) / tamanho
    a = np.radians(angulo)
    u = (qx * np.cos(a) - qy * np.sin(a)) / altura
    v = (qx * np.sin(a) + qy * np.cos(a)) / altura
    return regioes.em(u, v)
