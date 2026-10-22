"""Logos coloridas: máscara pela cor, cores dominantes, cor por parte na arte e validação."""
import asyncio
import importlib
import io

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from motor import forma_imagem as fi
from servidor.render import ParametroInvalido, Renderizador


def _png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _logo_pb():
    im = Image.new("RGB", (800, 600), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((200, 100, 600, 500), fill="black")
    d.rectangle((350, 250, 450, 350), fill="white")
    return im


def _logo_colorida():
    """Amarelo e azul-claro (luminância parecida com a do branco) e vermelho, sobre branco."""
    im = Image.new("RGB", (1000, 700), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((100, 100, 400, 300), fill="#ffe14d")
    d.rectangle((100, 320, 400, 520), fill="#7fe0ff")
    d.ellipse((500, 100, 900, 500), fill="#e2342b")
    return im


def _mascara_antiga(dados, limiar):
    rgba = np.asarray(Image.open(io.BytesIO(dados)).convert("RGBA"), np.float32)
    lum = rgba[..., 0] * 0.299 + rgba[..., 1] * 0.587 + rgba[..., 2] * 0.114
    borda = np.concatenate([lum[0], lum[-1], lum[:, 0], lum[:, -1]])
    return np.abs(lum - np.median(borda)) > limiar


# ---------- máscara ----------
@pytest.mark.parametrize("limiar", [5, 60, 200])
def test_logo_preto_e_branco_da_o_mesmo_resultado(limiar):
    dados = fi.reduzir(_png(_logo_pb()))
    assert np.array_equal(fi.mascara(dados, limiar), _mascara_antiga(dados, limiar))
    a = fi.analisar(dados)
    assert len(a.paleta) == 1 and a.paleta[0] == (0, 0, 0)


def test_logo_colorida_sobre_branco_entra_inteira():
    dados = fi.reduzir(_png(_logo_colorida()))
    novo, antigo = fi.mascara(dados), _mascara_antiga(dados, 60)
    # o amarelo e o azul-claro sumiam pela luminância; pela cor entram
    assert novo[200, 250] and novo[420, 250] and not antigo[200, 250] and not antigo[420, 250]
    a = fi.analisar(dados)
    cores = a.cores()
    hexes = [c["hex"] for c in cores]
    assert hexes[0] == "#e2342b" and sorted(hexes[1:]) == ["#7fe0ff", "#ffe14d"]   # da maior para a menor
    assert abs(sum(c["parte"] for c in cores) - 1) < 0.01
    assert all(c["arte"] and c["arte"].startswith("#") for c in cores)


def test_desligar_uma_cor_tira_a_parte_do_simbolo():
    a = fi.analisar(fi.reduzir(_png(_logo_colorida())))
    amarelo = [c["hex"] for c in a.cores()].index("#ffe14d")
    m = fi.mascara(a, cores_off=[amarelo])
    assert not m[200, 250] and m[420, 250] and m[300, 700]
    assert fi.mascara(a, cores_off=[0, 1, 2]).sum() == 0
    assert fi.mascara(a, cores_off=[5]).sum() == fi.mascara(a).sum()     # índice sem cor: ignorado


def test_png_transparente_continua_pelo_alfa():
    im = Image.new("RGBA", (600, 600), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((100, 100, 500, 500), fill=(30, 90, 200, 255))
    d.rectangle((250, 250, 350, 350), fill=(250, 200, 0, 255))
    a = fi.analisar(fi.reduzir(_png(im)))
    assert a.alfa and len(a.paleta) == 2
    m = fi.mascara(a, limiar=200)                       # o limiar não muda nada no modo alfa
    assert m[300, 300] and m[150, 300] and not m[10, 10]
    amarelo = 1 if a.paleta[1][2] < 100 else 0
    assert not fi.mascara(a, cores_off=[amarelo])[300, 300]


def test_texto_pequeno_preto_vira_uma_cor_propria():
    im = _logo_colorida()
    ImageDraw.Draw(im).rectangle((520, 560, 880, 600), fill="#111111")   # "tagline" com ~1% da logo
    for fmt, kw in (("PNG", {}), ("JPEG", {"quality": 60})):
        buf = io.BytesIO()
        im.save(buf, fmt, **kw)
        cores = fi.analisar(fi.reduzir(buf.getvalue())).cores()
        assert len(cores) == 4 and any(c["arte"] is None for c in cores)


def test_neutras_seguem_a_cor_principal():
    assert fi.cor_para_arte((0, 0, 0)) is None and fi.cor_para_arte((240, 240, 240)) is None
    r, g, b = (int(fi.cor_para_arte((20, 30, 90))[i:i + 2], 16) for i in (1, 3, 5))
    assert b > 200 and b > r and b > g                  # azul escuro vira azul aceso


def test_analise_tem_custo_limitado_em_ruido():
    arr = (np.random.default_rng(1).random((1024, 1024, 3)) * 255).astype(np.uint8)
    a = fi.analisar(fi.reduzir(_png(Image.fromarray(arr))))
    assert 1 <= len(a.paleta) <= fi.MAX_CORES


# ---------- servidor ----------
def _com_imagem(im):
    r = Renderizador()
    img_id, cores = r._guardar_e_analisar(_png(im))
    return r, img_id, cores


def test_upload_devolve_as_cores_e_guarda_a_analise():
    r, img_id, cores = _com_imagem(_logo_colorida())
    assert len(cores) == 3 and r.analises.pegar(img_id) is not None


def test_cores_desligadas_entram_na_chave_do_cache():
    r, img_id, _ = _com_imagem(_logo_colorida())
    p = {"fonte": "imagem", "imagem_id": img_id}
    _, chave_a, forma_a = r.fonte_holes(p)
    _, chave_b, forma_b = r.fonte_holes({**p, "cores_off": [1]})
    _, chave_c, _ = r.fonte_holes({**p, "cores_off": [1, 1]})
    assert chave_a != chave_b and chave_b == chave_c and forma_a.area != forma_b.area
    # a rede (cache por chave da fonte) também muda
    ka = chave_a + r.ler_config(p).chave_rede()
    kb = chave_b + r.ler_config(p).chave_rede()
    assert ka != kb


@pytest.mark.parametrize("cores_off", ["0", [True], [-1], [6], [0.5], list(range(7)), {"a": 1}])
def test_cores_off_invalidas_sao_recusadas(cores_off):
    r, img_id, _ = _com_imagem(_logo_colorida())
    with pytest.raises(ParametroInvalido):
        r.fonte_holes({"fonte": "imagem", "imagem_id": img_id, "cores_off": cores_off})


@pytest.mark.parametrize("p", [{"multicor": "sim"}, {"multicor": 1}, {"cores_arte": "#ff0000"},
                               {"cores_arte": ["#ff000"]}, {"cores_arte": ["ff0000"]}, {"cores_arte": [1]},
                               {"cores_arte": ["#ff0000"] * 7}, {"cores_arte": ["#-1ffff"]},
                               {"cor": "#f_ffff"}, {"cor": "# fffff"}, {"cor": "#-1ffff"}])
def test_parametros_de_cor_invalidos_sao_recusados(p):
    with pytest.raises(ParametroInvalido):
        Renderizador.ler_config(p)


def test_cores_arte_aceita_hex_e_null():
    cfg = Renderizador.ler_config({"multicor": True, "cores_arte": ["#FF0000", None]})
    assert cfg.multicor and cfg.cores_arte == ("#ff0000", None)


def test_arte_multicolorida_pinta_cada_parte_com_a_sua_cor():
    r, img_id, cores = _com_imagem(_logo_colorida())
    p = {"fonte": "imagem", "imagem_id": img_id, "seed": 3, "multicor": True,
         "cores_arte": ["#ff0000", "#00ff00", "#0000ff"]}
    im, _ = r._gerar(p, 960)
    a = np.asarray(im, np.float32)
    h, w, _ = a.shape
    # a parte vermelha (círculo) fica à direita do centro, as outras à esquerda
    dir_, esq = a[h // 4: 3 * h // 4, w // 2: 3 * w // 4], a[h // 4: 3 * h // 4, w // 4: w // 2]
    assert dir_[..., 0].sum() > dir_[..., 1].sum() and dir_[..., 0].sum() > dir_[..., 2].sum()
    assert esq[..., 1].sum() + esq[..., 2].sum() > 2 * esq[..., 0].sum() * 0.8
    um, _ = r._gerar({**p, "multicor": False}, 960)
    assert not np.array_equal(np.asarray(um), np.asarray(im))


def test_multicor_com_simbolo_pronto_e_ignorado():
    r = Renderizador()
    a, _ = r._gerar({"seed": 3, "multicor": True}, 640)
    b, _ = r._gerar({"seed": 3}, 640)
    assert np.array_equal(np.asarray(a), np.asarray(b))


def test_miniatura_mostra_as_partes_desligadas_apagadas():
    r, img_id, _ = _com_imagem(_logo_colorida())
    p = {"fonte": "imagem", "imagem_id": img_id}
    cheia = np.asarray(r._miniatura(p))
    sem = np.asarray(r._miniatura({**p, "cores_off": [1]}))
    assert sem.shape == (220, 220, 3)
    assert ((sem == (58, 52, 50)).all(-1)).sum() > 500            # a parte tirada aparece apagada
    assert not ((cheia == (58, 52, 50)).all(-1)).any()


def test_api_imagem_devolve_cores(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    monkeypatch.setattr(main, "render", Renderizador(1, 8))

    async def chamar():
        t = httpx.ASGITransport(app=main.app, client=("203.0.113.41", 1))
        async with httpx.AsyncClient(transport=t, base_url="http://s") as c:
            r = await c.post("/api/imagem", content=_png(_logo_colorida()), headers={"content-type": "image/png"})
            j = r.json()
            ruim = await c.post("/api/forma", json={"fonte": "imagem", "imagem_id": j["id"], "cores_off": ["x"]})
            boa = await c.post("/api/forma", json={"fonte": "imagem", "imagem_id": j["id"], "cores_off": [0]})
            prev = await c.post("/api/preview", json={"fonte": "imagem", "imagem_id": j["id"], "multicor": True,
                                                      "cores_arte": [c["arte"] for c in j["cores"]]})
            return r, j, ruim, boa, prev
    r, j, ruim, boa, prev = asyncio.run(chamar())
    assert r.status_code == 200 and sorted(c["hex"] for c in j["cores"]) == ["#7fe0ff", "#e2342b", "#ffe14d"]
    assert ruim.status_code == 400 and boa.status_code == 200 and prev.status_code == 200
