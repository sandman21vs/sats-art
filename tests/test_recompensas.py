import asyncio
import hashlib

import pytest

from servidor import antiabuso, lnurl
from servidor.coinos import CoinosErro
from servidor.config import Config
from servidor.db import DB
from servidor.recompensas import Recompensas, ResgateErro, hoje


class CoinosFalsa:
    def __init__(self, saldo=10_000, falha=None, taxa=1):
        self.balance, self.falha, self.taxa = saldo, falha, taxa
        self.pagos, self.internos = [], []

    async def saldo(self):
        return self.balance

    async def pagar_bolt11(self, bolt11):
        if self.falha:
            raise self.falha
        self.pagos.append(bolt11)
        self.balance -= 10 + self.taxa
        return {"ref": f"p{len(self.pagos)}", "fee": self.taxa}

    async def enviar_interno(self, usuario, sats):
        if self.falha:
            raise self.falha
        self.internos.append((usuario, sats))
        self.balance -= sats
        return {"ref": f"i{len(self.internos)}", "fee": 0}


async def invoice_falsa(endereco, sats, **kw):
    return f"lnbc{sats * 10}n1falsa"


def resolver(desafio):
    n = 0
    while True:
        d = hashlib.sha256(f"{desafio['salt']}:{n}".encode()).digest()
        if antiabuso.bits_zerados(d) >= desafio["bits"]:
            return {**desafio, "nonce": str(n)}
        n += 1


def montar(**kw):
    opcoes = {"coinos_token": "x", "pow_bits": 4, "intervalo_min_s": 0, "hmac_key": "k",
              "pagamento_externo": True, **kw}
    cfg = Config(**opcoes)
    coinos = CoinosFalsa()
    r = Recompensas(cfg, DB(":memory:"), coinos, invoice_para=invoice_falsa)
    return r, coinos


def resgatar(r, endereco="ana@carteira.com", ip="ip1", token=None, prova=None):
    token = token or r.emitir_token()
    prova = prova or resolver(antiabuso.criar_desafio(r.cfg.hmac_key, r.cfg.pow_bits))
    return asyncio.run(r.resgatar(token, endereco, ip, prova))


def test_paga_endereco_externo_e_interno():
    r, c = montar()
    assert resgatar(r)["sats"] == 10
    assert c.pagos == ["lnbc100n1falsa"]
    assert resgatar(r, "bob@coinos.io", ip="ip2")["taxa"] == 0
    assert c.internos == [("bob", 10)]


def test_primeira_10_segunda_11():
    r, _ = montar()
    a = resgatar(r)
    assert (a["sats"], a["ganho_hoje"], a["restante_hoje"], a["proxima"]) == (10, 10, 11, 11)
    b = resgatar(r)
    assert (b["sats"], b["ganho_hoje"], b["restante_hoje"], b["proxima"]) == (11, 21, 0, 0)


def test_externo_desligado_por_padrao_nao_consome_nada():
    r, c = montar(pagamento_externo=False)
    t = r.emitir_token()
    with pytest.raises(ResgateErro, match="@coinos.io"):
        resgatar(r, token=t)
    assert c.pagos == []
    assert resgatar(r, "ana@coinos.io", token=t)["sats"] == 10  # token continua valendo


def test_limite_diario_por_endereco_e_por_ip():
    r, _ = montar()
    for _ in range(2):
        resgatar(r)
    with pytest.raises(ResgateErro, match="limite"):
        resgatar(r)
    with pytest.raises(ResgateErro, match="limite"):          # outro endereço, mesmo IP
        resgatar(r, "outro@carteira.com")
    with pytest.raises(ResgateErro, match="limite"):          # mesmo endereço, outro IP
        resgatar(r, ip="ip9")
    assert resgatar(r, "outro@carteira.com", ip="ip9")["sats"] == 10


def test_intervalo_minimo_entre_resgates():
    r, _ = montar(intervalo_min_s=60)
    resgatar(r)
    with pytest.raises(ResgateErro, match="Espere"):
        resgatar(r, "outro@carteira.com")


def test_teto_global_e_reserva_do_pote():
    r, _ = montar(limite_dia_global=22)   # 1º: 10 + 2 de folga de taxa; 2º já passaria
    resgatar(r, "a@x.com", ip="1")
    with pytest.raises(ResgateErro, match="limite de recompensas do site"):
        resgatar(r, "b@x.com", ip="2")

    r, c = montar()
    c.balance = 511
    with pytest.raises(ResgateErro, match="pote"):
        resgatar(r)


def test_token_usado_ou_inexistente():
    r, _ = montar()
    t = r.emitir_token()
    resgatar(r, token=t)
    with pytest.raises(ResgateErro, match="já foi resgatada"):
        resgatar(r, ip="ip2", token=t)
    with pytest.raises(ResgateErro, match="já foi resgatada"):
        resgatar(r, ip="ip3", token="inventado")
    with pytest.raises(ResgateErro, match="já foi resgatada"):
        resgatar(r, ip="ip4", token={"nao": "texto"})


def test_token_expirado():
    r, _ = montar(token_validade_s=-1)
    with pytest.raises(ResgateErro, match="tempo"):
        resgatar(r)


def test_prova_de_trabalho():
    r, _ = montar()
    d = antiabuso.criar_desafio(r.cfg.hmac_key, r.cfg.pow_bits)
    errado = next(str(n) for n in range(1000) if antiabuso.bits_zerados(
        hashlib.sha256(f"{d['salt']}:{n}".encode()).digest()) < d["bits"])
    with pytest.raises(ResgateErro, match="anti-robô"):       # nonce que não resolve
        resgatar(r, prova={**d, "nonce": errado})
    with pytest.raises(ResgateErro, match="anti-robô"):       # desafio adulterado
        resgatar(r, prova={**resolver(d), "bits": 1})
    with pytest.raises(ResgateErro, match="anti-robô"):       # nem é um objeto
        resgatar(r, prova=["x"])
    sol = resolver(antiabuso.criar_desafio(r.cfg.hmac_key, r.cfg.pow_bits))
    resgatar(r, prova=sol)
    with pytest.raises(ResgateErro, match="anti-robô"):       # mesma prova duas vezes
        resgatar(r, ip="ip2", prova=sol)


def test_erro_ambiguo_nao_paga_de_novo_e_conta_no_limite():
    r, c = montar()
    c.falha = CoinosErro("timeout", ambiguo=True)
    t = r.emitir_token()
    with pytest.raises(ResgateErro, match="confirmar"):
        resgatar(r, "ana@coinos.io", token=t)
    c.falha = None
    with pytest.raises(ResgateErro, match="já foi resgatada"):  # token não volta
        resgatar(r, "ana@coinos.io", token=t)
    assert r.restante_hoje("ana@coinos.io", "ip1") == 11


def test_erro_inesperado_no_pagamento_e_tratado_como_ambiguo():
    r, c = montar()
    c.falha = KeyError("resposta estranha")
    t = r.emitir_token()
    with pytest.raises(ResgateErro, match="confirmar"):
        resgatar(r, "ana@coinos.io", token=t)
    with pytest.raises(ResgateErro, match="já foi resgatada"):
        resgatar(r, "ana@coinos.io", token=t)


def test_sem_conexao_libera_token_e_nao_conta():
    r, c = montar()
    c.falha = CoinosErro("sem conexão", ambiguo=False)
    t = r.emitir_token()
    with pytest.raises(ResgateErro, match="indisponível"):
        resgatar(r, token=t)
    c.falha = None
    assert resgatar(r, token=t)["sats"] == 10
    assert r.restante_hoje("ana@carteira.com", "ip1") == 11


def test_falha_no_lnurl_nao_reserva_nada():
    async def invoice_ruim(endereco, sats, **kw):
        raise lnurl.LnurlErro("só a partir de 100 sats")

    async def invoice_quebrada(endereco, sats, **kw):
        raise AttributeError("'list' object has no attribute 'get'")

    r, c = montar(limite_dia_global=15)
    t = r.emitir_token()
    for falsa in (invoice_ruim, invoice_quebrada):
        r.invoice_para = falsa
        with pytest.raises(lnurl.LnurlErro):
            resgatar(r, token=t)
    assert r.db.soma_dia(hoje()) == 0 and c.pagos == []
    r.invoice_para = invoice_falsa
    assert resgatar(r, token=t)["sats"] == 10   # mesmo token, orçamento intacto


def test_taxa_acima_do_maximo_pausa_pagamentos_externos():
    r, c = montar()
    c.taxa = 500
    assert resgatar(r)["taxa"] == 500
    assert not r.externos_liberados()
    with pytest.raises(ResgateErro, match="@coinos.io"):
        resgatar(r, "bob@carteira.com", ip="ip2")
    assert resgatar(r, "bob@coinos.io", ip="ip2")["sats"] == 10  # Coinos interno segue


def test_endereco_invalido():
    r, _ = montar()
    with pytest.raises(lnurl.LnurlErro, match="inválido"):
        resgatar(r, endereco="isso nao e endereco")
