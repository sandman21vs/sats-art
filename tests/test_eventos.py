"""Registro de eventos: tudo vira linha JSON, sem IP cru, endereço completo ou segredo."""
import asyncio
import importlib
import json
import logging

import httpx

from servidor import antiabuso, eventos
from servidor.config import Config
from servidor.db import DB
from servidor.recompensas import Recompensas


def _linhas(caplog):
    return [getattr(r, "dados", {}) for r in caplog.records if getattr(r, "dados", None)]


def test_formato_json_e_mascara():
    rec = logging.LogRecord("eventos", logging.INFO, __file__, 1, "x", None, None)
    rec.dados = {"evento": "teste", "sats": 10}
    d = json.loads(eventos._FormatoJSON().format(rec))
    assert d["evento"] == "teste" and d["sats"] == 10 and d["nivel"] == "info" and d["ts"].endswith("Z")
    assert eventos.mascarar_endereco("ana.silva@coinos.io") == "an***@coinos.io"
    assert eventos.mascarar_endereco(None) is None


def test_arquivo_de_log_recebe_eventos(tmp_path):
    raiz = logging.getLogger()
    antes = list(raiz.handlers)
    try:
        eventos.configurar(str(tmp_path))
        eventos.evento("teste_arquivo", valor=1)
        for h in raiz.handlers:
            h.flush()
        linhas = (tmp_path / "logs" / "eventos.jsonl").read_text().splitlines()
        assert json.loads(linhas[-1])["evento"] == "teste_arquivo"
    finally:
        for h in list(raiz.handlers):
            raiz.removeHandler(h)
        for h in antes:
            raiz.addHandler(h)


def test_pedido_vira_evento_sem_ip_cru(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")

    async def chamar():
        t = httpx.ASGITransport(app=main.app, client=("203.0.113.77", 4444))
        async with httpx.AsyncClient(transport=t, base_url="http://local") as c:
            await c.get("/")
            await c.post("/api/exportar", json={"largura": "abc"})
    with caplog.at_level(logging.INFO):
        asyncio.run(chamar())
    linhas = _linhas(caplog)
    pedidos = [d for d in linhas if d["evento"] == "pedido"]
    assert {d["rota"] for d in pedidos} >= {"/", "/api/exportar"}
    assert any(d["evento"] == "pedido_recusado" and d["status"] == 400 for d in linhas)
    assert "203.0.113.77" not in caplog.text
    assert pedidos[0]["ip"] == antiabuso.hash_ip(main.cfg.ip_salt, "203.0.113.77")[:12]


def test_resgate_gera_eventos_com_endereco_mascarado(caplog):
    class Carteira:
        async def saldo(self):
            return 100_000

        async def enviar_interno(self, u, s):
            return {"ref": "id-123", "fee": 0}

    async def rodar():
        cfg = Config(coinos_token="FAKE_ONLY", hmac_key="k", pow_bits=0, intervalo_min_s=0)
        r = Recompensas(cfg, DB(":memory:"), Carteira())
        await r.resgatar(r.emitir_token(), "anaclara@coinos.io", "hash-do-ip-123456",
                         {**antiabuso.criar_desafio("k", 0), "nonce": "0"})
    with caplog.at_level(logging.INFO):
        asyncio.run(rodar())
    nomes = [d["evento"] for d in _linhas(caplog)]
    for esperado in ("resgate_tentativa", "reserva_feita", "pagamento_confirmado", "resgate_pago"):
        assert esperado in nomes
    texto = json.dumps(_linhas(caplog), ensure_ascii=False)
    assert "anaclara@coinos.io" not in texto and "an***@coinos.io" in texto
