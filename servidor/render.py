"""Renderização: forma do símbolo, caches e fila limitada de trabalhos pesados."""
import asyncio
import hashlib
import io
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import fields

from PIL import Image, ImageDraw
from shapely.affinity import scale, translate

from motor.forma_imagem import ImagemInvalida, forma_normalizada, holes_de_forma, reduzir
from motor.rede_nos import Config, construir_rede, dimensoes, pintar
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
        self.formas = LRU(300)    # (id, limiar, inverter) -> forma normalizada
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

    @staticmethod
    def ler_config(p):
        if not isinstance(p, dict):
            raise ParametroInvalido("Pedido inválido.")
        nomes = {f.name for f in fields(Config)}
        kw = {}
        for k, v in p.items():
            if k not in nomes:
                continue
            if k in ("circulo", "retrato"):
                if not isinstance(v, bool):
                    raise ParametroInvalido(f"Parâmetro {k} inválido.")
                kw[k] = v
            elif k == "cor":
                if not (isinstance(v, str) and len(v) == 7 and v[0] == "#"):
                    raise ParametroInvalido("Cor inválida.")
                try:
                    int(v[1:], 16)
                except ValueError as e:
                    raise ParametroInvalido("Cor inválida.") from e
                kw[k] = v
            elif k == "seed":
                if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 2 ** 31:
                    raise ParametroInvalido("Semente inválida.")
                kw[k] = v
            else:
                kw[k] = _numero(k, v)
        return Config(**kw)

    def fonte_holes(self, p):
        """Devolve (make_holes, chave, forma) para o símbolo pedido (forma só para imagens)."""
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
            chave = (img_id, limiar, inverter)
            forma = self.formas.pegar(chave)
            if forma is None:
                forma = forma_normalizada(dados, limiar, inverter)
                self.formas.por(chave, forma)
            return holes_de_forma(forma), ("imagem",) + chave, forma
        if not isinstance(fonte, str) or fonte not in SIMBOLOS:
            raise ParametroInvalido("Símbolo desconhecido.")
        return SIMBOLOS[fonte][0], (fonte,), None

    # ---------- trabalho pesado (roda em thread, sempre pela fila) ----------
    def _gerar(self, p, largura):
        cfg = self.ler_config(p)
        make_holes, chave_fonte, _ = self.fonte_holes(p)
        chave = chave_fonte + cfg.chave_rede()
        rede = self.redes.pegar(chave)
        if rede is None:
            rede = construir_rede(make_holes, cfg)
            self.redes.por(chave, rede)
        return pintar(rede, cfg, largura), cfg

    def _miniatura(self, p):
        """A forma detectada no print, em branco sobre preto."""
        _, chave, g = self.fonte_holes(p)  # a forma vem junto: o cache pode tê-la descartado
        if chave[0] != "imagem":
            raise ParametroInvalido("Só há forma detectada para imagens enviadas.")
        lado = 220
        g = translate(scale(g, lado * 0.9, lado * 0.9, origin=(0, 0)), lado / 2, lado / 2)
        im = Image.new("RGB", (lado, lado), (20, 20, 20))
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
        """Decodificar e reduzir o upload também é trabalho pesado: entra na mesma fila."""
        return await self._na_fila(self.guardar_imagem, dados, limites=limites)

    async def miniatura(self, p, limites=()):
        return await self._na_fila(lambda: para_bytes(self._miniatura(p), "PNG"), limites=limites)


def para_bytes(im, fmt="JPEG"):
    buf = io.BytesIO()
    im.save(buf, fmt, **({"quality": 90} if fmt == "JPEG" else {"compress_level": 6}))
    return buf.getvalue()


__all__ = ["dimensoes", "Renderizador", "ParametroInvalido", "ImagemInvalida", "Ocupado", "Excesso", "para_bytes"]
