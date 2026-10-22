"""Formatos novos (quadrado, capas, papel de parede), modelos de estilo, prévia rápida e animação."""
import asyncio
import hashlib
import importlib
import io
import json
import pathlib
import re
import time

import httpx
import pytest
from PIL import Image

from motor import animacao as anim
from motor.estilos import carregar, parametros
from motor.rede_nos import FORMATOS, Config, construir_rede, dimensoes
from motor.simbolos import SIMBOLOS
from servidor import antiabuso
from servidor.render import ParametroInvalido, Renderizador, formato_de, lado_para

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SAT = SIMBOLOS["sat"][0]


@pytest.fixture
def main(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    m = importlib.import_module("servidor.main")
    monkeypatch.setattr(m, "render", Renderizador(1, 8))  # semáforo novo, deste laço de eventos
    return m


def _pedir(main, metodo, rota, ip="203.0.113.150", **kw):
    async def rodar():
        t = httpx.ASGITransport(app=main.app, client=(ip, 1))
        async with httpx.AsyncClient(transport=t, base_url="http://s") as c:
            return await c.request(metodo, rota, **kw)
    return asyncio.run(rodar())


def _impressao(rede):
    return hashlib.sha256(repr([(n.x, n.y, n.kind, n.size, n.bright) for n in rede.nodes]).encode()
                          + repr(rede.edges).encode()).hexdigest()[:16]


# ---------- formatos ----------
def test_dimensoes_de_cada_formato():
    assert dimensoes(1920, formato="paisagem") == (1920, 1080)
    assert dimensoes(1920, formato="retrato") == (1080, 1920)
    assert dimensoes(1080, formato="quadrado") == (1080, 1080)
    assert dimensoes(1500, formato="capa_x") == (1500, 500)
    assert dimensoes(2560, formato="capa_yt") == (2560, 1440)
    assert dimensoes(2796, formato="celular") == (1290, 2796)
    assert set(FORMATOS) == {"paisagem", "retrato", "quadrado", "capa_x", "capa_yt", "celular"}


def test_retrato_antigo_vira_formato_em_pe_e_formato_manda():
    assert Config(retrato=True).formato == "retrato" and Config().formato == "paisagem"
    assert Renderizador.ler_config({"retrato": True}).formato == "retrato"
    c = Renderizador.ler_config({"retrato": True, "formato": "quadrado"})
    assert c.formato == "quadrado" and not c.retrato
    assert Renderizador.ler_config({"formato": "retrato"}).retrato
    chaves = {Renderizador.ler_config({"formato": f}).chave_rede() for f in FORMATOS}
    assert len(chaves) == len(FORMATOS)  # cada formato tem a sua rede no cache


@pytest.mark.parametrize("valor", ["x", "", 5, None, True, ["quadrado"], "Quadrado"])
def test_formato_invalido_e_recusado(valor):
    with pytest.raises(ParametroInvalido):
        Renderizador.ler_config({"formato": valor})
    with pytest.raises(ParametroInvalido):
        formato_de({"formato": valor})


def test_arte_dos_formatos_antigos_continua_igual():
    # impressões tiradas do motor de antes dos formatos novos (e da poisson mais rápida):
    # a sequência de sorteios de construir_rede ficou intacta
    assert _impressao(construir_rede(SAT, Config(angulo=-21))) == "7712aaf5cf51641e"
    assert _impressao(construir_rede(SAT, Config(angulo=-21, retrato=True))) == "d7073e0606a26789"
    assert _impressao(construir_rede(SAT, Config(angulo=-21, formato="retrato"))) == "d7073e0606a26789"
    assert _impressao(construir_rede(SAT, Config(angulo=-21, circulo=False, seed=7, malha=9))) == "6cf8854307b45c27"


def test_formato_novo_tem_o_simbolo_dentro_da_area_segura():
    for nome, f in FORMATOS.items():
        rede = construir_rede(SAT, Config(angulo=-21, formato=nome))
        assert 0 < rede.CX < rede.W and 0 < rede.CY < rede.H
        if f.segura:
            x0, y0, x1, y1 = f.segura
            r = 300  # raio do disco
            assert x0 * rede.W <= rede.CX - r and rede.CX + r <= x1 * rede.W, nome
            assert y0 * rede.H <= rede.CY - r and rede.CY + r <= y1 * rede.H, nome


def test_lado_maximo_por_formato_nao_passa_dos_pixels_do_16_9():
    teto = 7680 * 4320
    for nome in FORMATOS:
        lado = lado_para(nome, 7680)
        w, h = dimensoes(lado, formato=nome)
        assert lado <= 7680 and w * h <= teto, nome
    assert lado_para("paisagem", 7680) == 7680 and lado_para("quadrado", 7680) < 5800


def test_exportar_quadrado_respeita_teto_e_tem_nome(main, monkeypatch):
    monkeypatch.setattr(main.cfg, "max_export", 1280)
    r = _pedir(main, "POST", "/api/exportar", json={"fonte": "sat", "largura": 5000, "formato": "quadrado"})
    assert r.status_code == 200
    w, h = Image.open(io.BytesIO(r.content)).size
    assert w == h and w * h <= 1280 * 720
    assert re.search(r"_quadrado_s21_\d+x\d+\.png$", r.headers["X-Nome-Arquivo"])
    r = _pedir(main, "POST", "/api/exportar", json={"fonte": "sat", "largura": 1500, "formato": "capa_x"})
    assert Image.open(io.BytesIO(r.content)).size == (1280, 427)
    r = _pedir(main, "POST", "/api/exportar", json={"fonte": "sat", "formato": "nada"})
    assert r.status_code == 400


# ---------- prévia rápida ----------
def test_previa_rapida_tem_um_terco_e_limite_proprio(main, monkeypatch):
    monkeypatch.setattr(main.cfg, "preview_w", 960)
    monkeypatch.setattr(main, "lim_preview", antiabuso.Limitador(1, 60))
    monkeypatch.setattr(main, "lim_preview_rapida", antiabuso.Limitador(2, 60))
    r = _pedir(main, "POST", "/api/preview?rapida=1", json={"seed": 4})
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (320, 180)
    r = _pedir(main, "POST", "/api/preview", json={"seed": 4})
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (960, 540)
    assert _pedir(main, "POST", "/api/preview", json={"seed": 4}).status_code == 429       # a nítida acabou
    assert _pedir(main, "POST", "/api/preview?rapida=1", json={"seed": 4}).status_code == 200
    assert _pedir(main, "POST", "/api/preview?rapida=1", json={"seed": 4}).status_code == 429
    assert main.PREVIA_RAPIDA_DIV >= 2 and importlib.reload  # noqa: B018


def test_limites_reais_da_previa_rapida_nao_sao_frouxos():
    m = importlib.import_module("servidor.main")
    assert m.lim_preview_rapida.n <= m.lim_preview.n <= 40


def test_previa_quadrada_nao_passa_dos_pixels_da_deitada(main, monkeypatch):
    monkeypatch.setattr(main.cfg, "preview_w", 640)
    r = _pedir(main, "POST", "/api/preview", json={"formato": "quadrado"})
    w, h = Image.open(io.BytesIO(r.content)).size
    assert w == h and w * h <= 640 * 360


def test_previa_abandonada_sai_da_fila_sem_desenhar(main):
    desenhou = []

    class Pedido:
        async def receive(self):
            return {"type": "http.disconnect"}

    async def demorado():
        await asyncio.sleep(5)
        desenhou.append(1)

    async def rodar():
        t0 = time.monotonic()
        assert await main.sem_esperar_quem_desistiu(Pedido(), demorado()) is None
        assert time.monotonic() - t0 < 1
    asyncio.run(rodar())
    assert not desenhou


# ---------- estilos ----------
def test_estilos_tem_parametros_validos_traducao_e_miniatura():
    dados = carregar()
    i18n = (RAIZ / "static" / "i18n.js").read_text(encoding="utf-8")
    ids = [e["id"] for e in dados["estilos"]]
    assert len(ids) >= 6 and len(set(ids)) == len(ids) and "neon" in ids and "minimal" in ids
    for e in dados["estilos"]:
        assert re.fullmatch(r"[a-z0-9_]+", e["id"])
        assert set(e["params"]) <= set(dados["base"]) | {"cor"}  # nada de símbolo, formato, semente...
        cfg = Renderizador.ler_config(parametros(e, dados))       # passa pela validação do servidor
        assert cfg.formato == "paisagem"
        assert i18n.count(f'"estilo.{e["id"]}"') == 3              # nome nos três idiomas
        mini = RAIZ / "static" / "estilos" / f"{e['id']}.webp"
        assert mini.stat().st_size < 12 * 1024
        assert Image.open(mini).size == (192, 108)


def test_rota_dos_estilos_serve_so_a_pasta(main):
    r = _pedir(main, "GET", "/estilos/neon.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "max-age" in r.headers["cache-control"] and "content-security-policy" in r.headers
    assert json.loads(_pedir(main, "GET", "/estilos/estilos.json").content)["estilos"]
    for ruim in ("/estilos/..%2Fapp.js", "/estilos/x.py", "/estilos/NEON.webp", "/estilos/nada.webp",
                 "/estilos/.webp", "/estilos/%2e%2e%2fservidor%2fmain.py"):
        assert _pedir(main, "GET", ruim).status_code == 404, ruim


# ---------- animação ----------
def _duracao(im):
    total = 0
    for i in range(im.n_frames):
        im.seek(i)
        total += im.info["duration"]
    return total


def test_animacao_gif_tem_os_quadros_e_cabe_no_limite():
    cfg = Config(angulo=-21)
    rede = construir_rede(SAT, cfg)
    t0 = time.perf_counter()
    passos = list(anim.gravar(rede, cfg, 480, tipo="gif", fps=12, segundos=4, lote=10))
    dt = time.perf_counter() - t0
    dados = passos[-1]
    assert all(p is None for p in passos[:-1]) and len(passos) == 5  # 48 quadros em lotes de 10
    im = Image.open(io.BytesIO(dados))
    # quadros iguais em sequência (o trecho aceso) viram um só, mais longo: soma das durações = 4 s
    assert im.format == "GIF" and im.size == (480, 270) and 30 <= im.n_frames <= 48
    assert abs(_duracao(im) - 4000) <= 100  # o GIF guarda centésimos de segundo
    assert len(dados) < 3 * 1024 * 1024 and dt < 20
    assert im.info.get("loop") == 0


def test_animacao_comeca_escura_acende_e_apaga():
    rede = construir_rede(SAT, Config(angulo=-21))
    luz = list(anim.luzes(rede, 60))
    assert luz[0][0].max() == 0 and luz[0][1] == 0
    assert luz[int(60 * anim.ACENDE) + 2][0].min() >= 0.99           # todos acesos
    assert luz[-1][0].max() < 0.25 and luz[-1][1] < 0.25           # quase apagado: o loop emenda


@pytest.mark.skipif(not anim.achar_ffmpeg(), reason="sem ffmpeg neste computador")
def test_animacao_mp4():
    cfg = Config(angulo=-21, formato="quadrado")
    rede = construir_rede(SAT, cfg)
    dados = list(anim.gravar(rede, cfg, 321, tipo="mp4", ffmpeg=anim.achar_ffmpeg(), fps=12, segundos=2))[-1]
    assert dados[4:8] == b"ftyp" and 1000 < len(dados) < 2 * 1024 * 1024


def test_rota_da_animacao_tem_limite_por_ip_e_valida(main, monkeypatch):
    import servidor.render as render_mod
    monkeypatch.setattr(render_mod, "ANIM_LADO", 320)
    monkeypatch.setattr(main, "lim_animacao", antiabuso.Limitador(2, 600))
    r = _pedir(main, "POST", "/api/animacao", json={"tipo": "gif", "formato": "celular", "seed": 2})
    assert r.status_code == 200 and r.headers["content-type"] == "image/gif"
    assert r.headers["X-Nome-Arquivo"].endswith("_celular_s2_animacao.gif")
    im = Image.open(io.BytesIO(r.content))
    assert abs(_duracao(im) - 1000 * anim.SEGUNDOS) <= 100 and max(im.size) == 320
    assert _pedir(main, "POST", "/api/animacao", json={"tipo": "avi"}).status_code == 400
    r = _pedir(main, "POST", "/api/animacao", json={"tipo": "gif"}, headers={"X-Idioma": "en"})
    assert r.status_code == 429 and r.json()["erro"].startswith("Too many animations")
    # outra pessoa não é afetada; e a fila e a cota voltam a zero
    assert _pedir(main, "POST", "/api/animacao", ip="198.51.100.151", json={"tipo": "x"}).status_code == 400
    assert main.render.na_fila == 0 and main.render.cota.em_curso == {}
    assert main.ANIMACOES_POR_10MIN <= 6


def test_animacao_conta_na_cota_de_render(main, monkeypatch):
    import collections
    main.render.cota.gasto["ip|203.0.113.160"] = collections.deque([(time.monotonic(), 1000)])
    r = _pedir(main, "POST", "/api/animacao", ip="203.0.113.160", json={"tipo": "gif"})
    assert r.status_code == 429


def test_info_diz_quais_animacoes_existem(main):
    tipos = _pedir(main, "GET", "/api/info").json()["animacao"]
    assert "gif" in tipos and set(tipos) <= {"mp4", "gif"}
