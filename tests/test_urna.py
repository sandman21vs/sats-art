"""Urna de brincadeira: um voto por pessoa, placar só depois de votar, sats com limites próprios."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from servidor import antiabuso, urna
from servidor.recompensas import ResgateErro, hoje
from tests.test_recompensas import montar, resolver

SALT = "sal-de-teste"


def rodar(coro):
    return asyncio.run(coro)


def prova(r):
    return resolver(antiabuso.criar_desafio(r.cfg.hmac_key, r.cfg.pow_bits))


def resgatar(r, token, endereco="ana@coinos.io", cota="c1", tipo="urna"):
    return rodar(r.resgatar(token, endereco, cota, prova(r), tipo=tipo))


# ---------- regras de resgate (Recompensas com tipo="urna") ----------
def test_token_da_urna_paga_o_valor_do_voto_uma_vez():
    r, c = montar()
    t = r.emitir_token("c1", tipo="urna", sats=22)
    assert resgatar(r, t)["sats"] == 22
    assert c.internos == [("ana", 22)]
    t2 = r.emitir_token("c1", tipo="urna", sats=13)
    with pytest.raises(ResgateErro, match="já foram resgatados"):
        resgatar(r, t2)


def test_urna_nao_gasta_o_limite_da_arte_e_vice_versa():
    r, c = montar()
    assert resgatar(r, r.emitir_token("c1", tipo="urna", sats=13))["sats"] == 13
    assert r.meu_dia("ana@coinos.io", "c1")["restante_hoje"] == 21  # 10 + 11 da arte intactos
    assert resgatar(r, r.emitir_token("c1"), tipo="arte")["sats"] == 10


def test_token_de_um_tipo_nao_vale_no_outro():
    r, _ = montar()
    with pytest.raises(ResgateErro, match="tempo para resgatar"):
        resgatar(r, r.emitir_token("c1"), tipo="urna")
    with pytest.raises(ResgateErro, match="Exporte outra imagem"):
        resgatar(r, r.emitir_token("c1", tipo="urna", sats=13), tipo="arte")


def test_limite_global_da_urna_e_separado():
    r, c = montar(urna_limite_dia_global=30, limite_dia_global=1000)
    assert resgatar(r, r.emitir_token("c1", tipo="urna", sats=22))["sats"] == 22
    # 22 + 13 + taxa máxima passa de 30: a urna fecha no dia...
    with pytest.raises(ResgateErro, match="urna de hoje acabaram"):
        resgatar(r, r.emitir_token("c2", tipo="urna", sats=13), endereco="bob@coinos.io", cota="c2")
    # ...mas a arte continua pagando
    assert resgatar(r, r.emitir_token("c2"), endereco="bob@coinos.io", cota="c2", tipo="arte")["sats"] == 10
    assert r.db.um("SELECT tipo FROM resgates WHERE endereco = 'bob@coinos.io'")[0] == "arte"
    assert r.db.um("SELECT COUNT(*) FROM resgates WHERE dia = ? AND tipo = 'urna'", hoje())[0] == 1


def test_banco_antigo_ganha_as_colunas_de_tipo(tmp_path):
    import sqlite3
    from servidor.db import DB
    caminho = str(tmp_path / "velho.db")
    con = sqlite3.connect(caminho)
    con.executescript("CREATE TABLE tokens (token TEXT PRIMARY KEY, criado REAL NOT NULL, usado INTEGER NOT NULL "
                      "DEFAULT 0); CREATE TABLE resgates (id INTEGER PRIMARY KEY AUTOINCREMENT, dia TEXT NOT NULL, "
                      "endereco TEXT NOT NULL, ip_hash TEXT NOT NULL, sats INTEGER NOT NULL, status TEXT NOT NULL, "
                      "ref TEXT, fee INTEGER DEFAULT 0, erro TEXT, criado REAL NOT NULL);"
                      "INSERT INTO resgates(dia, endereco, ip_hash, sats, status, criado) "
                      "VALUES ('2026-10-01', 'a@coinos.io', 'x', 10, 'pago', 0);")
    con.commit()
    con.close()
    db = DB(caminho)
    assert db.um("SELECT tipo FROM resgates")[0] == "arte"
    db.criar_token("t", "d", "urna", 13)
    assert db.sats_do_token("t") == 13 and db.token_valido("t", 60, "d", "urna")


# ---------- rotas ----------
def _app(ativa=True, com_recompensas=True, **kw):
    r, coinos = montar(urna_ativa=ativa, ip_salt=SALT, **kw)

    class Recusado(Exception):
        def __init__(self, status, msg):
            self.status, self.msg = status, msg

    async def ler_json(req):
        return await req.json()

    def limitar(lim, chave, msg="x"):
        if not lim.permitir(antiabuso.chave_ip(chave)):
            raise Recusado(429, msg)

    app = FastAPI()

    @app.exception_handler(Recusado)
    async def _recusado(req, e):
        from fastapi.responses import JSONResponse
        return JSONResponse({"erro": e.msg}, status_code=e.status)

    app.include_router(urna.criar_router(
        cfg=r.cfg, db=r.db, recompensas_de=lambda: r if com_recompensas else None,
        ip_de=lambda req: req.client.host, limitar=limitar, ler_json=ler_json, Recusado=Recusado,
        versao=lambda arq: "v1"))
    return app, r, coinos


def _cliente(app, ip="189.6.10.20"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 1)), base_url="http://site")


def test_desligada_tudo_404():
    app, _, _ = _app(ativa=False)

    async def t():
        async with _cliente(app) as c:
            return [(await c.get("/urna")).status_code, (await c.get("/urna.js")).status_code,
                    (await c.get("/api/urna")).status_code,
                    (await c.post("/api/urna/votar", json={"numero": "13"})).status_code,
                    (await c.post("/api/urna/resgatar", json={})).status_code]
    assert rodar(t()) == [404] * 5


def test_placar_so_depois_de_votar_e_um_voto_por_pessoa():
    app, r, coinos = _app()

    async def t():
        async with _cliente(app) as c:
            antes = (await c.get("/api/urna")).json()
            assert antes["votou"] is False and "placar" not in antes
            v = (await c.post("/api/urna/votar", json={"numero": "22"})).json()
            assert v["numero"] == "22" and v["sats"] == 22 and v["token"]
            assert v["placar"] == {"13": 0, "22": 1, "total": 1}
            # votar de novo não muda o voto nem cria outro token
            de_novo = (await c.post("/api/urna/votar", json={"numero": "13"})).json()
            assert de_novo["numero"] == "22" and de_novo["token"] == v["token"]
            assert de_novo["placar"]["total"] == 1
            assert (await c.get("/api/urna")).json()["token"] == v["token"]

            res = await c.post("/api/urna/resgatar", json={"token": v["token"], "endereco": "ana@coinos.io",
                                                          "prova": prova(r)})
            assert res.status_code == 200 and res.json()["sats"] == 22
            depois = (await c.get("/api/urna")).json()
            assert depois["votou"] and depois["token"] is None and depois["sats"] == 0
        async with _cliente(app, ip="200.1.2.3") as c:
            assert "placar" not in (await c.get("/api/urna")).json()
            assert (await c.post("/api/urna/votar", json={"numero": "13"})).json()["placar"] == \
                {"13": 1, "22": 1, "total": 2}
    rodar(t())
    assert coinos.internos == [("ana", 22)]


def test_numero_invalido_recusado():
    app, _, _ = _app()

    async def t():
        async with _cliente(app) as c:
            return [(await c.post("/api/urna/votar", json={"numero": n})).status_code
                    for n in ("17", 13, None, "")]
    assert rodar(t()) == [400] * 4


def test_token_de_outra_pessoa_nao_resgata():
    app, r, coinos = _app()

    async def t():
        async with _cliente(app) as c:
            token = (await c.post("/api/urna/votar", json={"numero": "13"})).json()["token"]
        async with _cliente(app, ip="200.9.9.9") as c:
            res = await c.post("/api/urna/resgatar", json={"token": token, "endereco": "bob@coinos.io",
                                                          "prova": prova(r)})
            return res.status_code
    assert rodar(t()) == 400
    assert coinos.internos == []


def test_sem_recompensas_vota_sem_token():
    app, _, _ = _app(com_recompensas=False)

    async def t():
        async with _cliente(app) as c:
            v = (await c.post("/api/urna/votar", json={"numero": "13"})).json()
            assert v["token"] is None and v["sats"] == 0 and v["placar"]["13"] == 1
            assert (await c.post("/api/urna/resgatar", json={})).status_code == 404
            assert (await c.get("/urna")).status_code == 200
    rodar(t())
