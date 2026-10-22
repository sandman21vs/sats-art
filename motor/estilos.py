"""Modelos prontos de estilo (static/estilos/estilos.json) e as miniaturas deles.

Cada estilo muda só a "cara" da rede (malha, halo, poeira, brilho, fundo, raio, zoom, círculo,
tamanho) e, se depender disso, a cor. O símbolo, a logo, o formato e a semente ficam.
Os parâmetros que o estilo não cita voltam ao valor de "base" (o Clássico).

Gerar as miniaturas de novo (depois de mudar o JSON):  python -m motor.estilos
"""
import json
import os

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASTA = os.path.join(RAIZ, "static", "estilos")
MINI = (192, 108)   # mostrada a 96 x 54 px: nítida em tela retina


def carregar():
    with open(os.path.join(PASTA, "estilos.json"), encoding="utf-8") as f:
        return json.load(f)


def parametros(estilo, dados=None):
    """Parâmetros completos de um estilo (base + os dele)."""
    dados = dados or carregar()
    return {**dados["base"], **estilo["params"]}


def main():
    from PIL import Image

    from motor.rede_nos import Config, construir_rede, pintar
    from motor.simbolos import SIMBOLOS
    dados = carregar()
    make_holes, angulo = SIMBOLOS["sat"]
    for e in dados["estilos"]:
        cfg = Config(angulo=angulo, seed=21, **parametros(e, dados))
        im = pintar(construir_rede(make_holes, cfg), cfg, 960)
        w, h = im.size  # recorta o miolo (70%): na miniatura o símbolo fica legível
        im = im.crop((w * 0.15, h * 0.15, w * 0.85, h * 0.85)).resize(MINI, Image.LANCZOS)
        caminho = os.path.join(PASTA, e["id"] + ".webp")
        im.save(caminho, "WEBP", quality=80, method=6)
        print(caminho, os.path.getsize(caminho), "bytes")


if __name__ == "__main__":
    main()
