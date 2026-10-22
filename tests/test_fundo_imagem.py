"""Fundo que não é liso: "Remover fundo" (superfície suave + sombras), pontos tocados como fundo,
validação dos parâmetros novos, chave do cache e custo limitado."""
import asyncio
import importlib
import io
import time

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from motor import forma_imagem as fi
from servidor.render import ParametroInvalido, Renderizador

W, H = 800, 600


def _png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _forma():
    """Disco com um furo quadrado (o furo tem que continuar furo)."""
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    d.ellipse((250, 150, 550, 450), fill=255)
    d.rectangle((360, 260, 440, 340), fill=0)
    return m


def _sobre(fundo, m, cor):
    return Image.composite(Image.new("RGB", fundo.size, cor), fundo, m)


def _degrade(c0, c1):
    t = np.linspace(0, 1, H)[:, None, None]
    a = np.array(c0) * (1 - t) + np.array(c1) * t
    return Image.fromarray(np.broadcast_to(a, (H, W, 3)).astype(np.uint8))


def _iou(m, gt):
    return (m & gt).sum() / max(1, (m | gt).sum())


def _gt(m, dados):
    return np.asarray(m.resize(Image.open(io.BytesIO(dados)).size)) > 127


# ---------- motor ----------
def test_logo_sobre_degrade():
    m = _forma()
    dados = fi.reduzir(_png(_sobre(_degrade((40, 80, 200), (240, 240, 255)), m, (20, 20, 20))))
    antes, depois = fi.analisar(dados), fi.analisar(dados, remover_fundo=True)
    assert antes.fundo_irregular                       # a página liga "Remover fundo" sozinha
    assert _iou(fi.mascara(antes), _gt(m, dados)) < 0.5  # o degradê virava forma
    assert _iou(fi.mascara(depois), _gt(m, dados)) > 0.97
    assert len(depois.paleta) == 1                     # sem as cores do degradê nos chips


def test_logo_sobre_vinheta():
    m = _forma()
    yy, xx = np.mgrid[0:H, 0:W]
    r = (np.hypot((xx - W / 2) / (W / 2), (yy - H / 2) / (H / 2)) / 1.414)[..., None]
    fundo = Image.fromarray((np.array([250, 240, 220]) * (1 - r) + np.array([120, 100, 80]) * r).astype(np.uint8))
    dados = fi.reduzir(_png(_sobre(fundo, m, (30, 60, 160))))
    a = fi.analisar(dados, remover_fundo=True)
    assert a.fundo_irregular and _iou(fi.mascara(a), _gt(m, dados)) > 0.97


def test_logo_com_sombra():
    m = _forma()
    sombra = Image.fromarray(np.roll(np.asarray(m.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * 0.35))),
                                     (18, 18), (0, 1)))
    fundo = Image.composite(Image.new("RGB", (W, H), "black"), Image.new("RGB", (W, H), (235, 235, 240)), sombra)
    dados = fi.reduzir(_png(_sobre(fundo, m, (220, 60, 20))))
    gt = _gt(m, dados)
    antes, depois = fi.mascara(fi.analisar(dados)), fi.mascara(fi.analisar(dados, remover_fundo=True))
    assert _iou(antes, gt) < 0.95 and _iou(depois, gt) > 0.99


def test_logo_encostando_na_maior_parte_da_borda_com_ponto_de_fundo():
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    d.rectangle((0, 0, 260, H), fill=255)
    d.rectangle((540, 0, W, H), fill=255)
    d.rectangle((0, 420, W, H), fill=255)
    d.ellipse((330, 100, 470, 240), fill=255)
    dados = fi.reduzir(_png(_sobre(Image.new("RGB", (W, H), (250, 250, 250)), m, (10, 10, 10))))
    gt = _gt(m, dados)
    assert _iou(fi.mascara(fi.analisar(dados)), gt) < 0.1          # a borda é quase toda logo
    for limpar in (False, True):                                    # um toque no fundo resolve
        assert _iou(fi.mascara(fi.analisar(dados, limpar, [(0.5, 0.1)])), gt) > 0.9


def test_ponto_numa_cor_isolada_nao_entorta_o_degrade():
    """Tocar no miolo branco de uma logo sobre degradê: o miolo vira furo e o degradê continua fundo."""
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).ellipse((250, 150, 550, 450), fill=255)
    im = _sobre(_degrade((40, 80, 200), (240, 240, 255)), m, (20, 20, 20))
    ImageDraw.Draw(im).rectangle((360, 260, 440, 340), fill="white")
    dados = fi.reduzir(_png(im))
    furo = Image.new("L", (W, H), 0)
    ImageDraw.Draw(furo).rectangle((360, 260, 440, 340), fill=255)
    gt = _gt(m, dados) & ~_gt(furo, dados)
    a = fi.analisar(dados, remover_fundo=True, pontos=[(0.5, 0.5)])
    assert _iou(fi.mascara(a), gt) > 0.97 and len(a.paleta) == 1


def test_ponto_encostado_na_logo_nao_estraga_o_ajuste():
    """Toque no degradê quase em cima da borda da logo: o miolo branco (parte da logo) fica."""
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).ellipse((250, 150, 550, 450), fill=255)
    im = _sobre(_degrade((40, 80, 200), (240, 240, 255)), m, (20, 20, 20))
    ImageDraw.Draw(im).rectangle((360, 260, 440, 340), fill="white")
    dados = fi.reduzir(_png(im))
    a = fi.analisar(dados, remover_fundo=True, pontos=[(0.439, 0.248)])
    assert a.dist[300, 400] > 100 and a.dist[50, 400] < 20 and len(a.paleta) == 2


def test_remover_fundo_com_ponto_numa_barra_da_interface():
    m = _forma()
    im = _sobre(Image.new("RGB", (W, H), "white"), m, (0, 0, 0))
    ImageDraw.Draw(im).rectangle((0, 0, W, 60), fill=(90, 110, 140))
    dados = fi.reduzir(_png(im))
    assert _iou(fi.mascara(fi.analisar(dados, remover_fundo=True, pontos=[(0.5, 0.05)])), _gt(m, dados)) > 0.97


def test_logo_encostando_em_um_lado_continua_certa():
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    d.rectangle((0, 200, 500, H), fill=255)
    d.ellipse((300, 50, 700, 450), fill=255)
    dados = fi.reduzir(_png(_sobre(Image.new("RGB", (W, H), (250, 250, 250)), m, (10, 10, 10))))
    for limpar in (False, True):
        assert _iou(fi.mascara(fi.analisar(dados, limpar)), _gt(m, dados)) > 0.99


def test_ponto_de_fundo_tira_a_cor_tocada():
    """Print com um pedaço da interface (barra cinza-azulada): tocar nela tira essa cor."""
    m = _forma()
    im = _sobre(Image.new("RGB", (W, H), "white"), m, (0, 0, 0))
    ImageDraw.Draw(im).rectangle((0, 0, W, 60), fill=(90, 110, 140))
    dados = fi.reduzir(_png(im))
    gt = _gt(m, dados)
    sem = fi.mascara(fi.analisar(dados))
    assert sem[:50].mean() > 0.9                        # a barra entrava no símbolo
    com = fi.mascara(fi.analisar(dados, pontos=[(0.5, 0.05)]))
    assert com[:50].mean() < 0.01 and _iou(com, gt) > 0.99


def test_pontos_ruins_no_motor_sao_ignorados():
    dados = fi.reduzir(_png(_sobre(Image.new("RGB", (W, H), "white"), _forma(), (0, 0, 0))))
    base = fi.mascara(fi.analisar(dados))
    lixo = [(float("nan"), 0.5), (2, 2), (-1, 0.5)] + [(0.0, 0.0)] * 20
    assert np.array_equal(fi.mascara(fi.analisar(dados, pontos=lixo)), base)


def test_logo_pb_de_fundo_liso_nao_sugere_remover_fundo_e_nao_muda():
    m = _forma()
    dados = fi.reduzir(_png(_sobre(Image.new("RGB", (W, H), "white"), m, (0, 0, 0))))
    a = fi.analisar(dados)
    assert not a.fundo_irregular
    assert np.array_equal(fi.mascara(fi.analisar(dados, remover_fundo=True)), fi.mascara(a))


def test_png_transparente_ignora_as_opcoes_de_fundo():
    im = Image.new("RGBA", (600, 600), (255, 255, 255, 0))
    ImageDraw.Draw(im).ellipse((100, 100, 500, 500), fill=(30, 90, 200, 255))
    dados = fi.reduzir(_png(im))
    a, b = fi.analisar(dados), fi.analisar(dados, True, [(0.5, 0.5)])
    assert a.alfa and b.alfa and np.array_equal(fi.mascara(a), fi.mascara(b))


def test_custo_limitado_em_ruido_com_tudo_ligado():
    arr = (np.random.default_rng(1).random((1024, 1024, 3)) * 255).astype(np.uint8)
    dados = fi.reduzir(_png(Image.fromarray(arr)))
    t0 = time.perf_counter()
    fi.analisar(dados)
    base = time.perf_counter() - t0
    t0 = time.perf_counter()
    a = fi.analisar(dados, remover_fundo=True, pontos=[(i / 8, i / 8) for i in range(fi.MAX_PONTOS_FUNDO)])
    custo = time.perf_counter() - t0
    assert 1 <= len(a.paleta) <= fi.MAX_CORES
    assert custo < base * 2 + 0.5                      # no máximo o dobro da análise normal (+ folga)


# ---------- servidor ----------
def _com_imagem(im):
    r = Renderizador()
    img_id, cores = r._guardar_e_analisar(_png(im))
    return r, img_id, cores


@pytest.mark.parametrize("extra", [
    {"limpar_fundo": 1}, {"limpar_fundo": "sim"}, {"pontos_fundo": "0.5,0.5"}, {"pontos_fundo": [[0.5]]},
    {"pontos_fundo": [[0.5, 1.5]]}, {"pontos_fundo": [[-0.1, 0.5]]}, {"pontos_fundo": [[True, 0.5]]},
    {"pontos_fundo": [["0.5", 0.5]]}, {"pontos_fundo": [[0.5, 0.5, 0.5]]}, {"pontos_fundo": [{"x": 0.5, "y": 0.5}]},
    {"pontos_fundo": [[0.5, 0.5]] * 9}, {"pontos_fundo": {"a": 1}},
])
def test_parametros_de_fundo_invalidos_sao_recusados(extra):
    r, img_id, _ = _com_imagem(_sobre(Image.new("RGB", (W, H), "white"), _forma(), (0, 0, 0)))
    with pytest.raises(ParametroInvalido):
        r.fonte_holes({"fonte": "imagem", "imagem_id": img_id, **extra})


def test_nan_e_infinito_nos_pontos_sao_recusados():
    r, img_id, _ = _com_imagem(_sobre(Image.new("RGB", (W, H), "white"), _forma(), (0, 0, 0)))
    for v in (float("nan"), float("inf")):
        with pytest.raises(ParametroInvalido):
            r.fonte_holes({"fonte": "imagem", "imagem_id": img_id, "pontos_fundo": [[v, 0.5]]})


def test_opcoes_de_fundo_entram_na_chave_do_cache():
    r, img_id, _ = _com_imagem(_sobre(_degrade((40, 80, 200), (240, 240, 255)), _forma(), (20, 20, 20)))
    p = {"fonte": "imagem", "imagem_id": img_id}
    _, ka, fa = r.fonte_holes(p)
    _, kb, fb = r.fonte_holes({**p, "limpar_fundo": True})
    _, kc, _ = r.fonte_holes({**p, "pontos_fundo": [[0.5, 0.02], [0.1, 0.9]]})
    _, kd, _ = r.fonte_holes({**p, "pontos_fundo": [[0.1, 0.9], [0.5, 0.02], [0.10001, 0.9]]})  # ordem e arredondamento
    assert len({ka, kb, kc}) == 3 and kc == kd and fa.area != fb.area
    assert r.analises.pegar(img_id) is not None and r.analises.pegar((img_id, True, ())) is not None


def test_upload_com_degrade_ja_devolve_as_cores_sem_o_fundo():
    r, img_id, cores = _com_imagem(_sobre(_degrade((40, 80, 200), (240, 240, 255)), _forma(), (20, 20, 20)))
    assert r.fundo_irregular(img_id) and len(cores) == 1
    assert r.cores_de({"imagem_id": img_id, "limpar_fundo": True}) == cores
    assert r.cores_de({"imagem_id": img_id, "pontos_fundo": [[0.5, 0.5]]}) is None   # ainda não analisada
    assert r.cores_de({"imagem_id": img_id, "limpar_fundo": "x"}) is None


def test_api_devolve_sugestao_e_cores_da_forma(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    im = _sobre(_degrade((40, 80, 200), (240, 240, 255)), _forma(), (20, 20, 20))

    async def rodar():
        t = httpx.ASGITransport(app=main.app, client=("189.6.10.21", 1))
        async with httpx.AsyncClient(transport=t, base_url="http://site") as c:
            up = await c.post("/api/imagem", content=_png(im), headers={"Content-Type": "image/png"})
            p = {"fonte": "imagem", "imagem_id": up.json()["id"], "limpar_fundo": True, "pontos_fundo": [[0.5, 0.02]]}
            forma = await c.post("/api/forma", json=p)
            ruim = await c.post("/api/forma", json={**p, "pontos_fundo": [[2, 2]]}, headers={"X-Idioma": "en"})
            return up, forma, ruim
    up, forma, ruim = asyncio.run(rodar())
    assert up.status_code == 200 and up.json()["fundo_irregular"] is True
    assert forma.status_code == 200 and forma.headers["content-type"] == "image/png"
    import json
    assert len(json.loads(forma.headers["x-cores"])) >= 1
    assert ruim.status_code == 400 and ruim.json() == {"erro": "Invalid image background setting."}
