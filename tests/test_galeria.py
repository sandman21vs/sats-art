"""Link da própria arte (/a/<id>), galeria pública com opt-in, denúncias e moderação."""
import ast
import asyncio
import dataclasses
import importlib
import io
import os
import pathlib
import time

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image, ImageDraw

from servidor import galeria, i18n
from servidor.artes import Artes

RAIZ = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "token-de-teste-123456"
SITE = "https://arte.exemplo"


@pytest.fixture
def main(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "principal"))
    monkeypatch.setenv("COINOS_TOKEN", "")
    return importlib.import_module("servidor.main")


def _app(main, tmp_path, monkeypatch, token=TOKEN, **env):
    """Um app só com as rotas da galeria (DATA_DIR próprio), com os mesmos middlewares do main."""
    if token:
        monkeypatch.setenv("ADMIN_TOKEN", token)
    else:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    cfg = dataclasses.replace(main.cfg, data_dir=str(tmp_path / "dados"), public_url=SITE)
    app = FastAPI()
    app.middleware("http")(main.mesma_origem)
    app.middleware("http")(main.cabecalhos)
    app.exception_handler(main.Recusado)(main._recusado)
    router = galeria.criar_router(cfg=cfg, render=main.render, ip_de=main.ip_de, limitar=main.limitar,
                                  ler_json=main.ler_json, Recusado=main.Recusado, limites_render=main.limites_render,
                                  pagina=main.pagina_com_versao, versao=main.versao_app)
    app.include_router(router)
    app.state.router = router
    return app


def _pedir(app, metodo, rota, ip="189.6.10.20", **kw):
    async def rodar():
        t = httpx.ASGITransport(app=app, client=(ip, 1))
        async with httpx.AsyncClient(transport=t, base_url="http://site") as c:
            return await c.request(metodo, rota, **kw)
    return asyncio.run(rodar())


def _logo(main):
    im = Image.new("RGB", (300, 300), "white")
    ImageDraw.Draw(im).polygon([(150, 30), (60, 170), (140, 170), (110, 270), (240, 120), (160, 120)], fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return main.render.guardar_imagem(buf.getvalue())


def _criar(app, ip="189.6.10.20", **corpo):
    return _pedir(app, "POST", "/api/arte", ip=ip, json={"fonte": "sat", "seed": 7, **corpo})


@pytest.fixture
def app(main, tmp_path, monkeypatch):
    return _app(main, tmp_path, monkeypatch)


def test_criar_e_abrir_o_link_da_arte(app, main):
    r = _criar(app, cor="#21d39b", retrato=True, seed=11)
    assert r.status_code == 200, r.text
    j = r.json()
    assert len(j["id"]) == 16 and j["url"] == f"{SITE}/a/{j['id']}" and len(j["chave"]) >= 20
    dados = _pedir(app, "GET", f"/api/arte/{j['id']}").json()
    assert dados["estado"] == {"fonte": "sat", "seed": 11, "cor": "#21d39b", "retrato": True} and dados["logo"] is False
    og = _pedir(app, "GET", f"/a/{j['id']}/og.jpg")
    assert og.status_code == 200 and og.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(og.content)).size == (1200, 630)
    mini = _pedir(app, "GET", f"/a/{j['id']}/mini.webp")
    assert mini.headers["content-type"] == "image/webp" and Image.open(io.BytesIO(mini.content)).size == (480, 300)
    assert len(mini.content) < 60_000  # miniatura leve
    # a mesma pessoa pedindo o link da mesma arte recebe o mesmo link
    assert _criar(app, cor="#21d39b", retrato=True, seed=11).json()["id"] == j["id"]
    assert _criar(app, cor="#21d39b", retrato=True, seed=11, ip="200.1.2.3").json()["id"] != j["id"]


def test_link_com_logo_guarda_a_imagem_e_devolve_ao_cache(app, main):
    img_id = _logo(main)
    r = _criar(app, fonte="imagem", imagem_id=img_id, limiar=60, inverter=False, cores_off=[])
    assert r.status_code == 200, r.text
    arte = r.json()["id"]
    logo_original = main.render.imagens.pegar(img_id)
    with main.render.imagens.trava:  # o cache em memória esqueceu a imagem (reinício, LRU...)
        del main.render.imagens[img_id]
    dados = _pedir(app, "GET", f"/api/arte/{arte}").json()
    assert dados["logo"] is True and dados["estado"]["imagem_id"] == img_id
    assert main.render.imagens.pegar(img_id) == logo_original  # quem abre já pode ajustar a arte
    assert _pedir(app, "GET", f"/a/{arte}/logo.png").content == logo_original
    # campos de imagem não entram na arte de um símbolo pronto
    r = _criar(app, fonte="sat", imagem_id=img_id, limiar=999)
    assert r.status_code == 200 and "limiar" not in _pedir(app, "GET", f"/api/arte/{r.json()['id']}").json()["estado"]


def test_parametros_validados_como_na_previa(app):
    r = _pedir(app, "POST", "/api/arte", json={"fonte": "sat", "zoom": 99}, headers={"X-Idioma": "en"})
    assert r.status_code == 400 and r.json()["erro"].startswith("Parameter zoom is out of range")
    r = _pedir(app, "POST", "/api/arte", json={"fonte": "imagem", "imagem_id": "0" * 24})
    assert r.status_code == 400 and "expirou" in r.json()["erro"]
    r = _pedir(app, "POST", "/api/arte", json={"fonte": "imagem", "imagem_id": "../../etc/passwd"})
    assert r.status_code == 400
    assert _pedir(app, "POST", "/api/arte", json={"fonte": "sat", "galeria": "sim"}).status_code == 400


def test_pagina_do_link_tem_open_graph_escapado(app):
    nome = '"><script>alert(1)</script>&<b>'
    arte = _criar(app, nome=nome).json()["id"]
    r = _pedir(app, "GET", f"/a/{arte}", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert r.status_code == 200 and "content-security-policy" in r.headers
    h = r.text
    assert "<script>alert(1)" not in h and "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;&amp;&lt;b&gt;" in h
    assert f'<meta property="og:image" content="{SITE}/a/{arte}/og.jpg">' in h
    assert f'<meta property="og:url" content="{SITE}/a/{arte}">' in h
    assert '<meta name="twitter:card" content="summary_large_image">' in h
    assert "Node Network" in h and h.count("<title>") == 1 and 'src="/galeria.js?v=' in h
    # sem nome: título padrão no idioma pedido
    h = _pedir(app, "GET", f"/a/{_criar(app, seed=99).json()['id']}").text
    assert "<title>Uma arte da Rede de Nós</title>" in h


def test_link_inexistente_ou_malformado(app):
    r = _pedir(app, "GET", "/a/AAAAAAAAAAAAAAAA")
    assert r.status_code == 404 and "og:image" not in r.text and "<html" in r.text  # a página avisa
    assert _pedir(app, "GET", "/api/arte/AAAAAAAAAAAAAAAA").status_code == 404
    for rota in ("/a/..%2F..%2Fapp.db/og.jpg", "/a/%2e%2e/og.jpg", "/a/curto/mini.webp", "/api/arte/..%2Fx"):
        assert _pedir(app, "GET", rota).status_code == 404
    with pytest.raises(ValueError):
        app.state.router.artes.caminho("../../x", "og")


def test_expiracao_apaga_link_e_arquivos(app, monkeypatch):
    arte = _criar(app).json()["id"]
    artes = app.state.router.artes
    og = artes.caminho(arte, "og")
    assert os.path.exists(og)
    artes._q("UPDATE artes SET acesso = ? WHERE id = ?", time.time() - 91 * 86400, arte)
    assert artes.limpar(90 * 86400) == 1
    assert not os.path.exists(og) and _pedir(app, "GET", f"/api/arte/{arte}").status_code == 404


def test_acesso_renova_a_expiracao(app):
    arte = _criar(app).json()["id"]
    artes = app.state.router.artes
    artes._q("UPDATE artes SET acesso = ? WHERE id = ?", time.time() - 80 * 86400, arte)
    _pedir(app, "GET", f"/a/{arte}")
    assert artes.limpar(90 * 86400) == 0 and artes.pegar(arte)["acesso"] > time.time() - 60


def test_teto_de_disco_apaga_as_mais_antigas_mas_protege_as_recentes(tmp_path):
    artes = Artes(str(tmp_path))
    criar = lambda i: artes.criar(params={"seed": i}, img_id=None, logo=None, cores=[], nome="", retrato=False,  # noqa: E731
                                  og=b"x" * 600, mini=b"y" * 400, ip_hash="h", assinatura=str(i), galeria=False)[0]
    velha, nova = criar(1), criar(2)
    artes._q("UPDATE artes SET acesso = ? WHERE id = ?", time.time() - 30 * 86400, velha)
    assert artes.liberar(1000, 2500, 7 * 86400) is True  # a velha sai para caber
    assert artes.pegar(velha) is None and artes.pegar(nova) and not os.path.exists(artes.caminho(velha, "og"))
    assert artes.liberar(5000, 2500, 7 * 86400) is False  # a recente nunca sai: recusa


def test_disco_cheio_recusa_novo_link(main, tmp_path, monkeypatch):
    app = _app(main, tmp_path, monkeypatch, ARTES_MAX_MB=10)
    artes = app.state.router.artes
    artes.criar(params={}, img_id=None, logo=None, cores=[], nome="", retrato=False, og=b"x" * (10 * 2**20),
                mini=b"", ip_hash="h", assinatura="cheio", galeria=False)
    r = _criar(app, seed=5, ip="200.9.9.9")
    assert r.status_code == 507 and r.json()["erro"] == "O espaço para links está cheio agora. Tente mais tarde."


def test_limite_de_links_por_ip(main, tmp_path, monkeypatch):
    app = _app(main, tmp_path, monkeypatch, ARTES_POR_HORA=2)
    assert [_criar(app, ip="201.0.0.1").status_code for _ in range(3)] == [200, 200, 429]
    assert _criar(app, ip="201.0.0.2").status_code == 200  # outra pessoa não é afetada


def _ids_galeria(app):
    return [i["id"] for i in _pedir(app, "GET", "/api/galeria").json()["itens"]]


def test_galeria_so_com_opt_in(app):
    fora = _criar(app, seed=1).json()
    dentro = _criar(app, seed=2, galeria=True).json()["id"]
    assert _ids_galeria(app) == [dentro]
    rota = f"/api/arte/{fora['id']}/galeria"
    assert _pedir(app, "POST", rota, json={"chave": "errada", "galeria": True}).status_code == 404
    assert _pedir(app, "POST", rota, json={"galeria": True}).status_code == 404
    assert _ids_galeria(app) == [dentro]
    assert _pedir(app, "POST", rota, json={"chave": fora["chave"], "galeria": True}).json() == {"ok": True, "galeria": True}
    assert _ids_galeria(app) == [fora["id"], dentro]  # a mais recente na galeria primeiro
    _pedir(app, "POST", rota, json={"chave": fora["chave"], "galeria": False})
    assert _ids_galeria(app) == [dentro]


def test_galeria_paginada(app):
    ids = [_criar(app, seed=s, galeria=True).json()["id"] for s in range(5)]
    p1 = _pedir(app, "GET", "/api/galeria?n=3").json()
    assert [i["id"] for i in p1["itens"]] == ids[::-1][:3] and p1["mais"]
    p2 = _pedir(app, "GET", f"/api/galeria?n=3&antes={p1['mais']}").json()
    assert [i["id"] for i in p2["itens"]] == ids[::-1][3:] and p2["mais"] is None


def test_denuncias_escondem_sozinhas(app):
    arte = _criar(app, seed=3, galeria=True).json()["id"]
    denunciar = lambda ip: _pedir(app, "POST", "/api/galeria/denunciar", ip=ip, json={"id": arte})  # noqa: E731
    assert denunciar("10.0.0.1").json() == {"ok": True}
    assert denunciar("10.0.0.1").status_code == 200  # a mesma pessoa conta uma vez só
    denunciar("10.0.0.2")
    assert _ids_galeria(app) == [arte]
    denunciar("10.0.0.3")
    assert _ids_galeria(app) == []
    assert _pedir(app, "GET", f"/a/{arte}").status_code == 200  # o link continua valendo
    r = _pedir(app, "POST", "/api/galeria/denunciar", json={"id": "AAAAAAAAAAAAAAAA"})
    assert r.status_code == 404


def test_limite_de_denuncias_por_ip(app):
    arte = _criar(app, seed=4, galeria=True).json()["id"]
    codigos = [_pedir(app, "POST", "/api/galeria/denunciar", ip="10.1.1.1", json={"id": arte}).status_code
               for _ in range(11)]
    assert codigos[:10] == [200] * 10 and codigos[10] == 429


def test_post_de_outra_origem_ou_sem_json_e_recusado(app):
    r = _pedir(app, "POST", "/api/arte", json={"fonte": "sat"}, headers={"Origin": "https://malvado.exemplo"})
    assert r.status_code == 403
    r = _pedir(app, "POST", "/api/galeria/denunciar", content=b'{"id":"x"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_moderacao_sem_token_nao_existe(main, tmp_path, monkeypatch):
    app = _app(main, tmp_path, monkeypatch, token=None)
    for metodo, rota in (("GET", "/admin/galeria"), ("GET", "/admin/galeria.js"), ("GET", "/admin/galeria/itens"),
                         ("POST", "/admin/galeria/moderar"), ("POST", "/admin/galeria/entrar")):
        r = _pedir(app, metodo, rota, json={}, headers={"Cookie": "admin=qualquer"})
        assert r.status_code == 404, rota
    # token curto demais também desliga
    app = _app(main, tmp_path / "b", monkeypatch, token="curto")
    assert _pedir(app, "GET", "/admin/galeria").status_code == 404


def test_moderacao_com_token_errado_e_recusada(app):
    assert _pedir(app, "GET", "/admin/galeria").status_code == 200  # a página (sem dados) abre para o login
    for cookie in (None, "admin=errado", f"admin={TOKEN}x", f"outro={TOKEN}"):
        r = _pedir(app, "GET", "/admin/galeria/itens", headers={"Cookie": cookie} if cookie else {})
        assert r.status_code == 403, cookie
    r = _pedir(app, "POST", "/admin/galeria/moderar", json={"id": "AAAAAAAAAAAAAAAA", "acao": "apagar"},
               headers={"Cookie": f"admin={TOKEN}"})  # o token cru não é sessão
    assert r.status_code == 403
    assert _pedir(app, "POST", "/admin/galeria/entrar", json={"token": "errado"}).status_code == 403
    codigos = [_pedir(app, "POST", "/admin/galeria/entrar", ip="9.9.9.9", json={"token": "x"}).status_code
               for _ in range(11)]
    assert codigos[-1] == 429  # força bruta limitada


def test_moderacao_esconde_mostra_e_apaga(app):
    r = _pedir(app, "POST", "/admin/galeria/entrar", json={"token": TOKEN})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie and "Secure" in cookie
    adm = {"Cookie": "outro=1; " + cookie.split(";")[0]}
    arte = _criar(app, seed=8, galeria=True, nome="<i>x</i>").json()["id"]
    for ip in ("10.2.0.1", "10.2.0.2", "10.2.0.3"):
        _pedir(app, "POST", "/api/galeria/denunciar", ip=ip, json={"id": arte})
    itens = _pedir(app, "GET", "/admin/galeria/itens?filtro=denunciadas", headers=adm).json()["itens"]
    assert [(i["id"], i["denuncias"], i["visivel"]) for i in itens] == [(arte, 3, False)]
    moderar = lambda acao: _pedir(app, "POST", "/admin/galeria/moderar", json={"id": arte, "acao": acao}, headers=adm)  # noqa: E731
    assert moderar("mostrar").status_code == 200 and _ids_galeria(app) == [arte]  # aprovada: denúncias não escondem
    assert moderar("esconder").status_code == 200 and _ids_galeria(app) == []
    caminho = app.state.router.artes.caminho(arte, "og")
    assert moderar("apagar").status_code == 200
    assert not os.path.exists(caminho) and _pedir(app, "GET", f"/a/{arte}").status_code == 404
    assert moderar("apagar").status_code == 404
    assert _pedir(app, "POST", "/admin/galeria/moderar", json={"id": arte, "acao": "sql"}, headers=adm).status_code == 400


def test_pagina_principal_carrega_galeria_js_com_versao(main):
    r = _pedir(main.app, "GET", "/")
    assert 'src="/galeria.js?v=' in r.text
    js = _pedir(main.app, "GET", "/galeria.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")


def test_mensagens_da_galeria_tem_traducao():
    arvore = ast.parse((RAIZ / "servidor" / "galeria.py").read_text(encoding="utf-8"))
    msgs = {a.value for no in ast.walk(arvore) if isinstance(no, ast.Call)
            and getattr(no.func, "id", getattr(no.func, "attr", "")) in ("Recusado", "limitar", "ParametroInvalido")
            for a in no.args if isinstance(a, ast.Constant) and isinstance(a.value, str) and " " in a.value}
    assert len(msgs) >= 8
    sem = sorted(m for m in msgs if i18n.traduzir(m, "en") == m or i18n.traduzir(m, "de") == m)
    assert not sem, sem


def test_login_do_painel_vale_para_a_moderacao(app):
    # a sessão gravada pelo login do /admin (servidor/admin.py) abre a moderação
    from servidor.admin import COOKIE, _assinar
    expira = str(int(time.time()) + 600)
    adm = {"Cookie": f"{COOKIE}={expira}.{_assinar(TOKEN, expira)}"}
    assert _pedir(app, "GET", "/admin/galeria/itens", headers=adm).status_code == 200
    vencida = str(int(time.time()) - 1)
    adm = {"Cookie": f"{COOKIE}={vencida}.{_assinar(TOKEN, vencida)}"}
    assert _pedir(app, "GET", "/admin/galeria/itens", headers=adm).status_code == 403
