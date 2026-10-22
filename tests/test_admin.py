"""Painel /admin (token, cookie, limite de tentativas, dados) e alerta de pote baixo."""
import asyncio
import importlib
import time

import httpx
import pytest

from servidor import alertas, eventos, telegram as tg
from servidor.config import Config
from servidor.db import DB

TOKEN = "token-de-teste-admin-123"


@pytest.fixture
def main(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    m = importlib.import_module("servidor.main")
    yield m
    eventos.gancho = None


def _app(main, monkeypatch, token=TOKEN):
    """Reconstrói o app só com o router do admin, usando o banco e a config do main."""
    from fastapi import FastAPI
    from servidor import admin
    monkeypatch.setattr(main.cfg, "admin_token", token)
    app = FastAPI()
    app.include_router(admin.criar_router(main.cfg, main.db, lambda: None, main.ip_de, main.ler_json))
    return app


def _cliente(app, ip="189.6.10.20"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 1)), base_url="http://site")


def rodar(coro):
    return asyncio.run(coro)


def test_sem_token_tudo_404(main, monkeypatch):
    app = _app(main, monkeypatch, token="")

    async def t():
        async with _cliente(app) as c:
            r = [await c.get("/admin"), await c.get("/admin/admin.js"), await c.get("/api/admin/resumo"),
                 await c.post("/api/admin/login", json={"token": "x"})]
            return [x.status_code for x in r]
    assert rodar(t()) == [404, 404, 404, 404]


def test_token_errado_recusa_e_certo_cria_cookie(main, monkeypatch):
    app = _app(main, monkeypatch)

    async def t():
        async with _cliente(app) as c:
            assert (await c.get("/api/admin/resumo")).status_code == 401
            assert (await c.post("/api/admin/login", json={"token": "errado"})).status_code == 401
            assert (await c.get("/api/admin/resumo")).status_code == 401
            # token na query string nunca vale
            assert (await c.get(f"/api/admin/resumo?token={TOKEN}")).status_code == 401
            r = await c.post("/api/admin/login", json={"token": TOKEN})
            assert r.status_code == 200
            cookie = r.headers["set-cookie"].lower()
            assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
            assert TOKEN not in r.headers["set-cookie"]
            assert (await c.get("/api/admin/resumo")).status_code == 200
            # cookie adulterado não entra
            c.cookies.clear()
            c.cookies.set("admin_sessao", f"{int(time.time()) + 999}.{'0' * 64}")
            assert (await c.get("/api/admin/resumo")).status_code == 401
    rodar(t())


def test_cookie_secure_em_https(main, monkeypatch):
    app = _app(main, monkeypatch)

    async def t():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("189.6.10.20", 1)),
                                     base_url="https://site") as c:
            r = await c.post("/api/admin/login", json={"token": TOKEN})
            assert "secure" in r.headers["set-cookie"].lower()
    rodar(t())


def test_limite_de_tentativas_por_ip(main, monkeypatch):
    app = _app(main, monkeypatch)

    async def t():
        async with _cliente(app, "189.6.10.30") as c:
            codigos = [(await c.post("/api/admin/login", json={"token": f"errado{i}"})).status_code
                       for i in range(10)]
            assert codigos[:8] == [401] * 8 and codigos[8:] == [429, 429]
            # nem o token certo passa depois do limite
            assert (await c.post("/api/admin/login", json={"token": TOKEN})).status_code == 429
        async with _cliente(app, "189.6.10.31") as c:  # outro IP não é afetado
            assert (await c.post("/api/admin/login", json={"token": TOKEN})).status_code == 200
    rodar(t())


def test_dados_do_painel(main, monkeypatch):
    app = _app(main, monkeypatch)
    db = main.db
    hoje = time.strftime("%Y-%m-%d", time.gmtime())
    eventos.evento("exportacao", largura=640)
    eventos.evento("exportacao", largura=640)
    eventos.evento("erro_interno", "error", rota="/x", tipo="ValueError")
    eventos.evento("erro_interno", "error", rota="/y", tipo="ValueError")
    db.guardar_relato("🐞 Relato de teste")
    db.ex("INSERT INTO resgates(dia, endereco, ip_hash, sats, status, criado) VALUES (?, 'a@coinos.io', 'h', 10, 'pago', ?)",
          hoje, time.time())

    async def t():
        async with _cliente(app) as c:
            await c.post("/api/admin/login", json={"token": TOKEN})
            d = (await c.get("/api/admin/resumo")).json()
            assert len(d["dias"]) == 14 and d["dias"][-1]["dia"] == hoje
            assert d["dias"][-1]["artes"] == 2 and d["total_artes"] == 2
            assert d["dias"][-1]["sats"] == 10 and d["total_resgates"] == 1
            assert d["erros"][0] == {"erro": "erro_interno: ValueError", "n": 2}
            assert d["relatos"][0]["texto"] == "🐞 Relato de teste"
            assert d["pote"] is None
    rodar(t())


def test_pagina_servida_com_cookie_e_token_ausente(main, monkeypatch):
    app = _app(main, monkeypatch)

    async def t():
        async with _cliente(app) as c:
            r = await c.get("/admin")
            assert r.status_code == 200 and "/admin/galeria" in r.text
            assert (await c.get("/admin/admin.js")).status_code == 200
    rodar(t())


# ---------- alerta de pote ----------
class TelegramFalso:
    def __init__(self, falhar=False):
        self.enviadas, self.falhar = [], falhar

    async def enviar(self, texto):
        if self.falhar:
            raise tg.TelegramErro("status 500")
        self.enviadas.append(texto)


def _alerta(tmp_path, **kw):
    cfg = Config()
    cfg.alerta_pote_sats, cfg.alerta_pote_horas = 500, 6
    for k, v in kw.items():
        setattr(cfg, k, v)
    t = TelegramFalso()
    db = DB(str(tmp_path / "a.db"))
    return alertas.AlertaPote(cfg, db, t), t, db


def test_alerta_dispara_uma_vez_e_respeita_intervalo(tmp_path):
    a, t, db = _alerta(tmp_path)

    async def t_():
        await a.verificar(800)
        assert t.enviadas == []
        await a.verificar(300)
        await a.verificar(250)
        await a.verificar(100)
        assert len(t.enviadas) == 1 and "300" in t.enviadas[0]
        db.gravar_estado("alerta_pote_ultimo", time.time() - 7 * 3600)  # passou o intervalo
        await a.verificar(90)
        assert len(t.enviadas) == 2
    rodar(t_())


def test_alerta_avisa_quando_reabastece(tmp_path):
    a, t, _ = _alerta(tmp_path)

    async def t_():
        await a.verificar(300)
        await a.verificar(700)
        await a.verificar(800)  # só avisa uma vez
        assert len(t.enviadas) == 2 and "reabastecido" in t.enviadas[1]
    rodar(t_())


def test_alerta_desligado_ou_sem_telegram(tmp_path):
    a, t, _ = _alerta(tmp_path, alerta_pote_sats=0)
    b, _, _ = _alerta(tmp_path)
    b.telegram = None

    async def t_():
        await a.verificar(1)
        await b.verificar(1)
        assert t.enviadas == []
    rodar(t_())


def test_alerta_falha_do_telegram_nao_marca_como_enviado(tmp_path):
    a, t, db = _alerta(tmp_path)
    t.falhar = True

    async def t_():
        await a.verificar(100)
        assert db.ler_estado("alerta_pote_ultimo") is None
        t.falhar = False
        a._tentativa = 0
        await a.verificar(100)
        assert len(t.enviadas) == 1
    rodar(t_())
