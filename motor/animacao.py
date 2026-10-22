"""Animação curta da arte: os nós acendem do símbolo para fora, ficam acesos e apagam (em loop).

A rede é construída uma vez (construir_rede); cada quadro é a mesma pintura (pintar) com uma
"luz" diferente em cada nó, que depende da distância dele ao centro do símbolo. Assim o miolo do
clipe é exatamente a arte parada, e o custo por quadro é o de uma prévia pequena.

Saídas:
- MP4 (H.264): o formato que toda rede aceita como vídeo (Instagram, TikTok, WhatsApp, X...).
  Precisa de um ffmpeg (o pacote imageio-ffmpeg traz um pronto); os quadros vão pelo stdin.
- GIF (só Pillow): reserva para servidor sem ffmpeg. Sem granulado e com uma paleta só.

`gravar()` é um gerador: cada next() pinta e codifica um lote de quadros. Quem chama pode
devolver a vez na fila de render entre um lote e outro (veja servidor/render.py).
"""
import io
import math
import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image

from .rede_nos import Config, Rede, pintar

FPS = 15
SEGUNDOS = 5
LOTE = 15             # quadros por next() de gravar()
# partes do loop (fração do tempo): a onda acende, fica tudo aceso, apaga
ACENDE, APAGA = 0.55, 0.84
LARGURA_ONDA = 0.16   # largura da frente da onda (fração da distância máxima)
MAX_BYTES = 25 * 1024 * 1024
PRAZO_FFMPEG_S = 60


class FalhaAnimacao(RuntimeError):
    pass


def _suave(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def luzes(rede: Rede, quadros):
    """Para cada quadro, (luz de cada nó, luz dos contornos do símbolo)."""
    xy = np.array([(n.x, n.y) for n in rede.nodes], np.float64).reshape(-1, 2)
    d = np.hypot(xy[:, 0] - rede.CX, xy[:, 1] - rede.CY)
    r = d / max(1e-9, float(d.max()) if len(d) else 1.0)   # 0 no centro, 1 no nó mais longe
    r = np.sqrt(r)  # a área cresce com r²: assim a onda acende mais ou menos o mesmo tanto por quadro
    w = LARGURA_ONDA
    for q in range(quadros):
        t = q / quadros
        u = min(1.0, t / ACENDE)
        frente = (1 - (1 - u) ** 1.5) * (1 + 2 * w) - w * 0.5   # rápida no começo, freia no fim
        x = (frente - r) / w                        # >0: a onda já passou por este nó
        rampa = np.clip(x, 0, 1)
        clarao = 0.9 * np.exp(-((x - 1.0) / 0.7) ** 2)   # brilho extra logo depois da onda passar
        luz = rampa * (1 + clarao)
        aneis = _suave(t / 0.1) * (1 + 0.7 * math.exp(-((t - 0.1) / 0.06) ** 2))
        if t > APAGA:                               # apaga tudo para o loop recomeçar do escuro
            f = 1 - _suave((t - APAGA) / (1 - APAGA - 1 / quadros))
            luz, aneis = luz * f, aneis * f
        yield luz, aneis


def achar_ffmpeg():
    """Caminho de um ffmpeg: o do pacote imageio-ffmpeg (Docker) ou um do sistema. None = sem MP4."""
    try:
        import imageio_ffmpeg
        caminho = imageio_ffmpeg.get_ffmpeg_exe()
        if caminho and os.path.isfile(caminho):
            return caminho
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _par(v):
    return v - v % 2


def gravar(rede: Rede, cfg: Config, lado, campo=None, tipo="mp4", ffmpeg=None, fps=FPS, segundos=SEGUNDOS,
           lote=LOTE):
    """Gerador: cada next() pinta e codifica até `lote` quadros e devolve None; o último next()
    devolve o arquivo pronto (bytes). Memória: um quadro por vez (MP4) ou os quadros já em 256
    cores (GIF, ~1/3 do tamanho de um quadro RGB cada)."""
    if tipo == "gif":  # o GIF guarda a duração em centésimos: 15 fps (66,7 ms) tocaria rápido demais
        fps = 100 / round(100 / fps)
    n = round(fps * segundos)
    grao = tipo != "gif"
    ultimo = None   # (luz, aneis, imagem): os quadros do trecho parado saem iguais e não são repintados

    def quadro(acesos):
        nonlocal ultimo
        luz, aneis = acesos
        if ultimo is not None and ultimo[1] == aneis and np.array_equal(ultimo[0], luz):
            return ultimo[2]
        im = pintar(rede, cfg, lado, campo, acesos=acesos, grao=grao)
        ultimo = (luz, aneis, im)
        return im

    if tipo == "gif":
        yield from _gif(rede, n, fps, lote, quadro)
    elif tipo == "mp4":
        if not ffmpeg:
            raise FalhaAnimacao("sem ffmpeg")
        yield from _mp4(rede, n, fps, lote, quadro, ffmpeg)
    else:
        raise ValueError(tipo)


def _gif(rede, n, fps, lote, quadro):
    luz = list(luzes(rede, n))
    # paleta tirada do quadro mais aceso (o fim da onda) e usada em todos: sem cintilar
    ref = quadro(luz[int(n * ACENDE) + 1]).quantize(255, method=Image.Quantize.MEDIANCUT)
    conv = []
    for i, acesos in enumerate(luz):
        conv.append(quadro(acesos).quantize(palette=ref, dither=Image.Dither.NONE))
        if (i + 1) % lote == 0 and i + 1 < n:
            yield None
    buf = io.BytesIO()
    conv[0].save(buf, "GIF", save_all=True, append_images=conv[1:], duration=10 * round(100 / fps), loop=0,
                 disposal=1)
    if buf.tell() > MAX_BYTES:
        raise FalhaAnimacao("GIF grande demais")
    yield buf.getvalue()


def _mp4(rede, n, fps, lote, quadro, ffmpeg):
    pasta = tempfile.mkdtemp(prefix="rede-anim-")
    saida = os.path.join(pasta, "a.mp4")
    proc = None
    try:
        for i, acesos in enumerate(luzes(rede, n)):
            im = quadro(acesos)
            w, h = _par(im.width), _par(im.height)   # H.264 em yuv420p pede lados pares
            if (w, h) != im.size:
                im = im.crop((0, 0, w, h))
            if proc is None:
                proc = subprocess.Popen(
                    [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "pipe:0",
                     "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-threads", "2", "-movflags", "+faststart", saida],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                proc.stdin.write(im.tobytes())
            except (BrokenPipeError, OSError) as e:
                raise FalhaAnimacao("ffmpeg parou") from e
            if (i + 1) % lote == 0 and i + 1 < n:
                yield None
        proc.stdin.close()
        try:
            proc.wait(PRAZO_FFMPEG_S)
        except subprocess.TimeoutExpired as e:
            raise FalhaAnimacao("ffmpeg demorou demais") from e
        if proc.returncode != 0:
            raise FalhaAnimacao("ffmpeg falhou: " + proc.stderr.read(300).decode(errors="replace"))
        if os.path.getsize(saida) > MAX_BYTES:
            raise FalhaAnimacao("vídeo grande demais")
        with open(saida, "rb") as f:
            dados = f.read()
        yield dados
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc is not None and proc.stderr:
            proc.stderr.close()
        shutil.rmtree(pasta, ignore_errors=True)


def main():
    """python -m motor.animacao [mp4|gif] [formato] [lado]: grava a animação do Sat em output/."""
    import sys
    import time

    from motor.rede_nos import construir_rede
    from motor.simbolos import SIMBOLOS
    tipo = sys.argv[1] if len(sys.argv) > 1 else "mp4"
    formato = sys.argv[2] if len(sys.argv) > 2 else "paisagem"
    lado = int(sys.argv[3]) if len(sys.argv) > 3 else 1080
    cfg = Config(angulo=SIMBOLOS["sat"][1], formato=formato)
    t0 = time.perf_counter()
    rede = construir_rede(SIMBOLOS["sat"][0], cfg)
    dados = None
    for dados in gravar(rede, cfg, lado, tipo=tipo, ffmpeg=achar_ffmpeg()):
        pass
    os.makedirs("output", exist_ok=True)
    nome = f"output/sat_animacao_{formato}.{tipo}"
    with open(nome, "wb") as f:
        f.write(dados)
    print(nome, len(dados) // 1024, "KB", round(time.perf_counter() - t0, 1), "s")


if __name__ == "__main__":
    main()
