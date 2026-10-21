"""Regressão da auditoria rodada 3 (R3-01 a R3-05). Só APIs falsas."""
import asyncio
import importlib
import time

import httpx
import pytest

from servidor import antiabuso, lnurl
from servidor.coinos import Coinos, CoinosErro
from servidor.config import Config
from servidor.db import DB
from servidor.recompensas import Recompensas, ResgateErro
from tests.conftest import fazer_bolt11, rota


def cfg_teste(**kw):
    opcoes = {"coinos_token": "FAKE_ONLY", "hmac_key": "audit", "pow_bits": 0, "intervalo_min_s": 0, **kw}
    return Config(**opcoes)


def prova(cfg):
    return {**antiabuso.criar_desafio(cfg.hmac_key, cfg.pow_bits), "nonce": "0"}


# ---------- R3-01: vizinhos do mesmo /48 não se bloqueiam com a tabela folgada ----------
def test_admissao_48_so_vale_com_limitador_meio_cheio(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    monkeypatch.setattr(main, "lim_admissao", antiabuso.Limitador(64, 600))
    lim = antiabuso.Limitador(5, 600, max_chaves=10_000)
    for i in range(64):
        main.limitar(lim, f"2001:db8:1:{i:x}::1")
    main.limitar(lim, "2001:db8:1:100::1")       # vizinho do mesmo /48 entra
    # com o limitador meio cheio, a admissão volta a valer
    cheio = antiabuso.Limitador(5, 600, max_chaves=100)
    for i in range(50):
        cheio.permitir(f"198.51.100.{i}")
    monkeypatch.setattr(main, "lim_admissao", antiabuso.Limitador(2, 600))
    main.limitar(cheio, "2001:db8:2:1::1")
    main.limitar(cheio, "2001:db8:2:2::1")
    with pytest.raises(main.Recusado):
        main.limitar(cheio, "2001:db8:2:3::1")


# ---------- R3-02: saldo atrasado da Coinos ----------
def test_saldo_atrasado_nao_cruza_a_reserva(tmp_path):
    class Carteira:
        def __init__(self):
            self.real, self.pagos = 520, 0

        async def saldo(self):
            return 520  # sempre o mesmo número velho

        async def enviar_interno(self, u, s):
            self.real -= s
            self.pagos += 1
            return {"ref": f"id-{self.pagos}", "fee": 0}

    async def ataque():
        carteira = Carteira()
        cfg = cfg_teste(recompensas_seq=[10], reserva_pote=500)
        r = Recompensas(cfg, DB(str(tmp_path / "r3-02.db")), carteira)
        for i in range(3):
            try:
                await r.resgatar(r.emitir_token(), f"ana{i}@coinos.io", f"ip-{i}", prova(cfg))
            except ResgateErro as e:
                assert "pote" in str(e)
        assert carteira.real >= cfg.reserva_pote
    asyncio.run(ataque())


# ---------- R3-03: 2xx sem confirmação não vira "pago" ----------
@pytest.mark.parametrize("resposta", [{"error": "recipient rejected"}, {"status": "failed", "id": "x"}, {}, {"id": ""}])
def test_send_sem_confirmacao_fica_ambiguo(tmp_path, resposta):
    def handler(req):
        if req.url.path.endswith("/me"):
            return httpx.Response(200, json={"balance": 100_000})
        return httpx.Response(200, json=resposta)

    async def ataque():
        client = Coinos("FAKE_ONLY", "http://fake.local", transport=httpx.MockTransport(handler))
        cfg = cfg_teste()
        db = DB(str(tmp_path / "r3-03.db"))
        r = Recompensas(cfg, db, client)
        t = r.emitir_token()
        try:
            with pytest.raises(ResgateErro, match="confirmar"):
                await r.resgatar(t, "ana@coinos.io", "ip", prova(cfg))
            assert [s for (s,) in db.con.execute("SELECT status FROM resgates")] == ["erro"]
            assert not db.token_valido(t, 999)
        finally:
            await client.fechar()
    asyncio.run(ataque())


def test_send_com_id_continua_pago():
    def handler(req):
        return httpx.Response(200, json={"id": "e1d4df8b-0000-4000-8000-000000000000", "amount": 10})

    async def rodar():
        client = Coinos("FAKE_ONLY", "http://fake.local", transport=httpx.MockTransport(handler))
        try:
            assert (await client.enviar_interno("ana", 10))["ref"].startswith("e1d4df8b")
        finally:
            await client.fechar()
    asyncio.run(rodar())


# ---------- R3-04: taxa composta de rota privada ----------
def test_taxa_de_rota_e_composta_salto_a_salto():
    inv = fazer_bolt11(10, rotas=[rota(0, 100_000) + rota(0, 100_000)])  # dois saltos de 10%
    assert lnurl.decodificar_bolt11(inv)["taxa_rotas_msat"] == 2100  # 1000 + 1100, não 2000
    with pytest.raises(lnurl.LnurlErro, match="taxa de rota"):
        lnurl.conferir_invoice(inv, 10_000, taxa_max_sats=2)


# ---------- R3-05: pagamentos externos um por vez entre processos ----------
def test_dois_processos_nao_pagam_externo_ao_mesmo_tempo(tmp_path):
    class Carteira:
        def __init__(self):
            self.pedidos = 0

        async def saldo(self):
            return 100_000

        async def pagar_bolt11(self, _b):
            self.pedidos += 1
            await asyncio.sleep(0.1)
            return {"ref": f"h{self.pedidos}", "fee": 100}

    async def invoice(_e, _s, **_kw):
        return "lnbc100n1fake"

    async def ataque():
        path = str(tmp_path / "r3-05.db")
        carteira = Carteira()
        cfg = cfg_teste(recompensas_seq=[10], pagamento_externo=True, taxa_max=2)
        a = Recompensas(cfg, DB(path), carteira, invoice_para=invoice)   # "processo" A
        b = Recompensas(cfg, DB(path), carteira, invoice_para=invoice)   # "processo" B
        res = await asyncio.gather(
            a.resgatar(a.emitir_token(), "x@wallet.example", "ip-a", prova(cfg)),
            b.resgatar(b.emitir_token(), "y@wallet.example", "ip-b", prova(cfg)),
            return_exceptions=True)
        assert carteira.pedidos == 1
        assert sum(isinstance(x, ResgateErro) for x in res) == 1
        assert not a.externos_liberados() and not b.externos_liberados()
        assert DB(path).ler_estado("vez_externo") is None  # a vez foi devolvida
    asyncio.run(ataque())


def test_vez_externa_presa_por_queda_bloqueia_ate_conferencia():
    db = DB(":memory:")
    assert db.adquirir_vez_externo("processo-que-caiu")
    assert not db.adquirir_vez_externo("outro")      # presa: alguém precisa conferir na Coinos
    db.liberar_vez_externo("processo-que-caiu")
    assert db.adquirir_vez_externo("outro")
