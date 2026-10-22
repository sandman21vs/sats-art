"""Renderização: forma do símbolo, caches e fila limitada de trabalhos pesados."""
import asyncio
import hashlib
import io
import math
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import fields

import numpy as np
from PIL import Image, ImageDraw
from shapely.affinity import affine_transform, scale, translate

from motor.forma_imagem import (MAX_CORES, ImagemInvalida, analisar, forma_normalizada, holes_de_forma,
                                mascara, reduzir, regioes_na_tela)
from motor.rede_nos import Config, campo_de_cores, construir_rede, dimensoes, pintar
from motor.simbolos import SIMBOLOS

from .eventos import evento

# faixas aceitas para cada parâmetro (valores fora são recusados, não "consertados")
FAIXAS = {
    "angulo": (-180, 180), "tamanho": (0.2, 2.5), "raio": (100, 500), "zoom": (0.8, 8),
    "malha": (8, 40), "poeira_longe": (0, 0.6), "halo": (30, 400), "brilho": (0.1, 3), "fundo": (0, 3),
}


class ParametroInvalido(ValueError):
    pass


class Ocupado(Exception):
    """Fila cheia: o cliente deve tentar de novo depois."""


class Excesso(Exception):
    """Esta pessoa já tem trabalho demais na fila ou gastou seu tempo de render da janela."""


class CotaRender:
    """Limite por pessoa (chave = /64, /48...) do trabalho pesado: quantos trabalhos ao mesmo
    tempo e quantos segundos de render numa janela. Sem isso, um único IP pedindo prévias
    caras (até ~5 s cada) ocupa sozinho toda a fila."""

    def __init__(self, janela_s=600, max_chaves=20000):
        self.janela, self.max_chaves = janela_s, max_chaves
        self.em_curso = {}             # chave -> trabalhos na fila agora
        self.gasto = OrderedDict()     # chave -> deque[(instante, segundos)]

    def _segundos(self, chave, agora):
        fila = self.gasto.get(chave)
        while fila and fila[0][0] < agora - self.janela:
            fila.popleft()
        return sum(s for _, s in fila) if fila else 0.0

    def entrar(self, limites):
        """limites = [(chave, max_simultaneos, max_segundos)]. Levanta Excesso ou reserva a vaga."""
        agora = time.monotonic()
        for chave, max_sim, max_seg in limites:
            if self.em_curso.get(chave, 0) >= max_sim or self._segundos(chave, agora) >= max_seg:
                raise Excesso()
        for chave, _, _ in limites:
            self.em_curso[chave] = self.em_curso.get(chave, 0) + 1

    def sair(self, limites, segundos):
        agora = time.monotonic()
        for chave, _, _ in limites:
            n = self.em_curso.get(chave, 0) - 1
            if n > 0:
                self.em_curso[chave] = n
            else:
                self.em_curso.pop(chave, None)
            if segundos > 0:
                self.gasto.setdefault(chave, deque()).append((agora, segundos))
                self.gasto.move_to_end(chave)
        while len(self.gasto) > self.max_chaves:
            self.gasto.popitem(last=False)


class LRU(OrderedDict):
    """Cache LRU limitado por quantidade e, opcionalmente, por bytes."""

    def __init__(self, maximo, max_bytes=None):
        super().__init__()
        self.maximo, self.max_bytes, self.bytes = maximo, max_bytes, 0
        self.trava = threading.Lock()

    def pegar(self, chave):
        with self.trava:
            if chave in self:
                self.move_to_end(chave)
                return self[chave]
            return None

    def por(self, chave, valor):
        with self.trava:
            if chave in self:
                self.bytes -= self._tam(self[chave])
            self[chave] = valor
            self.bytes += self._tam(valor)
            self.move_to_end(chave)
            while len(self) > self.maximo or (self.max_bytes and self.bytes > self.max_bytes and len(self) > 1):
                _, velho = self.popitem(last=False)
                self.bytes -= self._tam(velho)

    def _tam(self, v):
        return len(v) if self.max_bytes and isinstance(v, (bytes, bytearray)) else 0


_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def _cor(v, msg="Cor inválida."):
    # regex, não int(v, 16): int aceita "-1ffff", " fffff" e "f_ffff", que quebrariam a pintura
    if not (isinstance(v, str) and _HEX.fullmatch(v)):
        raise ParametroInvalido(msg)
    return v.lower()


def _cores_off(v):
    """Índices das cores da logo desligadas: lista curta de inteiros 0..MAX_CORES-1."""
    if v is None:
        return ()
    if not isinstance(v, list) or len(v) > MAX_CORES or \
            any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < MAX_CORES for i in v):
        raise ParametroInvalido("Ajuste de cores da imagem inválido.")
    return tuple(sorted(set(v)))


def _numero(nome, v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ParametroInvalido(f"Parâmetro {nome} inválido.")
    v = float(v)
    lo, hi = FAIXAS[nome]
    if not math.isfinite(v) or not lo <= v <= hi:
        raise ParametroInvalido(f"Parâmetro {nome} fora da faixa ({lo} a {hi}).")
    return v


class Renderizador:
    def __init__(self, workers=1, max_fila=8):
        self.imagens = LRU(500, max_bytes=64 * 1024 * 1024)  # id -> PNG reduzido (≤1024px)
        self.formas = LRU(300)    # (id, limiar, inverter, cores_off) -> (forma normalizada, Regioes)
        self.analises = LRU(32)   # id -> Analise (≈2 MB cada: distância ao fundo e cor de cada pixel)
        self.redes = LRU(4)       # chave -> Rede
        self.sem = asyncio.Semaphore(workers)
        self.max_fila = workers + max_fila
        self.na_fila = 0
        self.cota = CotaRender()
        self._tarefas = set()  # trabalhos em andamento (referência forte)

    # ---------- entrada ----------
    def guardar_imagem(self, dados):
        reduzida = reduzir(dados)  # ImagemInvalida se não for uma imagem aceitável
        img_id = hashlib.sha256(reduzida).hexdigest()[:24]
        self.imagens.por(img_id, reduzida)
        return img_id

    def analise(self, img_id, dados):
        """Análise de cor da imagem (feita uma vez; refeita só se saiu do cache)."""
        a = self.analises.pegar(img_id)
        if a is None:
            a = analisar(dados)
            self.analises.por(img_id, a)
        return a

    def _guardar_e_analisar(self, dados):
        """Upload: reduz, guarda e já analisa as cores (a página mostra os chips na hora)."""
        img_id = self.guardar_imagem(dados)
        reduzida = self.imagens.pegar(img_id)
        return img_id, (self.analise(img_id, reduzida).cores() if reduzida is not None else [])

    @staticmethod
    def ler_config(p):
        if not isinstance(p, dict):
            raise ParametroInvalido("Pedido inválido.")
        nomes = {f.name for f in fields(Config)}
        kw = {}
        for k, v in p.items():
            if k not in nomes:
                continue
            if k in ("circulo", "retrato", "multicor"):
                if not isinstance(v, bool):
                    raise ParametroInvalido(f"Parâmetro {k} inválido.")
                kw[k] = v
            elif k == "cor":
                kw[k] = _cor(v)
            elif k == "cores_arte":  # cor de cada parte da logo; null = segue a cor principal
                if not isinstance(v, list) or len(v) > MAX_CORES:
                    raise ParametroInvalido("Cores da arte inválidas.")
                kw[k] = tuple(None if c is None else _cor(c, "Cores da arte inválidas.") for c in v)
            elif k == "seed":
                if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 2 ** 31:
                    raise ParametroInvalido("Semente inválida.")
                kw[k] = v
            else:
                kw[k] = _numero(k, v)
        return Config(**kw)

    def fonte_holes(self, p, com_regioes=False):
        """Devolve (make_holes, chave, forma) para o símbolo pedido (forma só para imagens).
        Com `com_regioes`, acrescenta as Regioes (em que cor da logo cai cada ponto) e a Analise."""
        fonte = p.get("fonte", "sat")
        if fonte == "imagem":
            img_id = p.get("imagem_id")
            dados = self.imagens.pegar(img_id) if isinstance(img_id, str) else None
            if dados is None:
                raise ParametroInvalido("A imagem expirou no servidor. Envie de novo.")
            limiar, inverter = p.get("limiar", 60), p.get("inverter", False)
            if isinstance(limiar, bool) or not isinstance(limiar, int) or not 1 <= limiar <= 254 \
                    or not isinstance(inverter, bool):
                raise ParametroInvalido("Ajuste de imagem inválido.")
            cores_off = _cores_off(p.get("cores_off"))
            chave = (img_id, limiar, inverter, cores_off)
            par = self.formas.pegar(chave)
            analise = None
            if par is None or com_regioes:
                analise = self.analise(img_id, dados)
            if par is None:
                par = forma_normalizada(analise, limiar, inverter, cores_off, com_regioes=True)
                self.formas.por(chave, par)
            forma, regioes = par
            if com_regioes:
                return holes_de_forma(forma), ("imagem",) + chave, forma, regioes, analise
            return holes_de_forma(forma), ("imagem",) + chave, forma
        if not isinstance(fonte, str) or fonte not in SIMBOLOS:
            raise ParametroInvalido("Símbolo desconhecido.")
        if com_regioes:
            return SIMBOLOS[fonte][0], (fonte,), None, None, None
        return SIMBOLOS[fonte][0], (fonte,), None

    # ---------- trabalho pesado (roda em thread, sempre pela fila) ----------
    def _gerar(self, p, largura):
        cfg = self.ler_config(p)
        multicor = cfg.multicor and p.get("fonte") == "imagem"
        make_holes, chave_fonte, _, regioes, analise = (self.fonte_holes(p, com_regioes=True) if multicor
                                                        else self.fonte_holes(p) + (None, None))
        chave = chave_fonte + cfg.chave_rede()
        rede = self.redes.pegar(chave)
        if rede is None:
            rede = construir_rede(make_holes, cfg)
            self.redes.por(chave, rede)
        campo = None
        if multicor and regioes is not None:
            campo = campo_de_cores(rede, cfg, lambda xs, ys: regioes_na_tela(
                regioes, xs, ys, rede.CX, rede.CY, cfg.angulo, cfg.tamanho), self._cores_das_partes(cfg, analise))
        return pintar(rede, cfg, largura, campo), cfg

    @staticmethod
    def _cores_das_partes(cfg, analise):
        """Cor de brilho de cada parte da logo: a escolhida pela pessoa, senão a sugerida pela
        análise, senão (cores neutras) a cor principal."""
        cores = []
        for i, sugerida in enumerate(c["arte"] for c in analise.cores()):
            escolhida = cfg.cores_arte[i] if i < len(cfg.cores_arte) else sugerida
            h = (escolhida or cfg.cor).lstrip("#")
            cores.append(tuple(int(h[j:j + 2], 16) for j in (0, 2, 4)))
        return cores

    def _miniatura(self, p):
        """A forma detectada no print, em branco sobre preto. As partes de cores desligadas
        aparecem apagadas, para a pessoa ver o que tirou."""
        _, chave, g, regioes, analise = self.fonte_holes(p, com_regioes=True)
        if chave[0] != "imagem":
            raise ParametroInvalido("Só há forma detectada para imagens enviadas.")
        lado = 220
        cores_off = chave[4]
        cheia = mascara(analise, chave[2]) if cores_off and not chave[3] else None
        if cheia is not None and cheia.any():
            # enquadra a logo inteira (com as partes tiradas), não só o que sobrou: em pixels da imagem
            ys, xs = np.nonzero(cheia)
            bx0, by0, bx1, by1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
            s = lado * 0.9 / max(bx1 - bx0, by1 - by0)
            ox, oy = lado / 2 - (bx0 + bx1) / 2 * s, lado / 2 - (by0 + by1) / 2 * s
            fora = cheia & ~mascara(analise, chave[2], cores_off=cores_off)
            k = np.arange(lado, dtype=np.float64) + 0.5
            x = np.floor((k[None, :] - ox) / s).astype(np.int64).repeat(lado, 0)
            y = np.floor((k[:, None] - oy) / s).astype(np.int64).repeat(lado, 1)
            h, w = fora.shape
            dentro = (x >= 0) & (x < w) & (y >= 0) & (y < h)
            apagado = np.zeros((lado, lado), bool)
            apagado[dentro] = fora[y[dentro], x[dentro]]
            fundo = np.full((lado, lado, 3), 20, np.uint8)
            fundo[apagado] = (58, 52, 50)
            im = Image.fromarray(fundo, "RGB")
            # forma normalizada -> pixels da imagem (recorte x0, y0 e escala fator das Regioes) -> miniatura
            f = s / regioes.fator
            g = affine_transform(g, [regioes.lado * f, 0, 0, regioes.lado * f,
                                     (regioes.cx / regioes.fator + regioes.x0) * s + ox,
                                     (regioes.cy / regioes.fator + regioes.y0) * s + oy])
        else:
            im = Image.new("RGB", (lado, lado), (20, 20, 20))
            g = translate(scale(g, lado * 0.9, lado * 0.9, origin=(0, 0)), lado / 2, lado / 2)
        d = ImageDraw.Draw(im)
        for parte in (g.geoms if hasattr(g, "geoms") else [g]):
            d.polygon(list(parte.exterior.coords), fill=(240, 240, 240))
            for furo in parte.interiors:
                d.polygon(list(furo.coords), fill=(20, 20, 20))
        return im

    async def _na_fila(self, func, *args, limites=()):
        """Roda `func` numa thread, respeitando o semáforo, o tamanho da fila e a cota da pessoa
        (`limites`, veja CotaRender).

        Uma thread não pode ser interrompida, então a vaga só é devolvida quando a
        thread TERMINA, mesmo que o pedido HTTP seja cancelado antes. Se o pedido
        for cancelado enquanto ainda espera a vez, o trabalho nem começa.
        """
        if self.na_fila >= self.max_fila:
            evento("fila_cheia", "warning", na_fila=self.na_fila, max_fila=self.max_fila)
            raise Ocupado()
        self.cota.entrar(limites)  # Excesso se a pessoa já ocupa ou já gastou demais
        self.na_fila += 1
        iniciado = False
        gasto = 0.0

        def medido():
            nonlocal gasto
            t0 = time.perf_counter()
            try:
                return func(*args)
            finally:
                gasto = time.perf_counter() - t0

        async def trabalho():
            nonlocal iniciado
            try:
                async with self.sem:
                    iniciado = True
                    return await asyncio.to_thread(medido)
            finally:
                self.na_fila -= 1
                self.cota.sair(limites, gasto)

        tarefa = asyncio.create_task(trabalho())
        self._tarefas.add(tarefa)
        tarefa.add_done_callback(self._tarefas.discard)
        try:
            return await asyncio.shield(tarefa)
        except asyncio.CancelledError:
            if not iniciado:
                tarefa.cancel()  # ainda na espera: pode desistir sem deixar thread órfã
            raise

    def _gerar_bytes(self, p, largura, fmt):
        t0 = time.perf_counter()
        im, cfg = self._gerar(p, largura)
        dados = para_bytes(im, fmt)
        if fmt == "PNG":  # prévias são muitas; só as exportações viram evento próprio
            evento("render", largura=largura, ms=round((time.perf_counter() - t0) * 1000))
        return dados, cfg

    async def gerar(self, p, largura, fmt="JPEG", limites=()):
        """Desenha e já codifica (JPEG da prévia ou PNG da exportação) dentro da fila."""
        return await self._na_fila(self._gerar_bytes, p, largura, fmt, limites=limites)

    async def guardar(self, dados, limites=()):
        """Decodificar, reduzir e analisar as cores do upload também é trabalho pesado: entra na
        mesma fila. Devolve (id, cores)."""
        return await self._na_fila(self._guardar_e_analisar, dados, limites=limites)

    async def miniatura(self, p, limites=()):
        return await self._na_fila(lambda: para_bytes(self._miniatura(p), "PNG"), limites=limites)


def para_bytes(im, fmt="JPEG"):
    buf = io.BytesIO()
    im.save(buf, fmt, **({"quality": 90} if fmt == "JPEG" else {"compress_level": 6}))
    return buf.getvalue()


__all__ = ["dimensoes", "Renderizador", "ParametroInvalido", "ImagemInvalida", "Ocupado", "Excesso", "para_bytes"]
