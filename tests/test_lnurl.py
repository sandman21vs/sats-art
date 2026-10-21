import asyncio
import ipaddress
import socket

import httpx
import pytest

from servidor import lnurl
from tests.conftest import fazer_bolt11, rota

IP_CARTEIRA = "203.0.113.10"


@pytest.fixture(autouse=True)
def dns_falso(monkeypatch):
    """carteira.com resolve para um IP público; interno.lan para a rede interna."""
    async def resolver(host):
        if host in ("interno.lan", "localhost"):
            raise lnurl.LnurlErro("Esse domínio não aponta para um endereço público.")
        return ipaddress.ip_address(IP_CARTEIRA)
    monkeypatch.setattr(lnurl, "_resolver", resolver)


def carteira(minimo=1000, maximo=10**9, invoice=None, callback="https://carteira.com/cb", vistos=None, info=None):
    def handler(req: httpx.Request):
        if vistos is not None:
            vistos.append(req)
        if req.url.path == "/.well-known/lnurlp/ana":
            return httpx.Response(200, json=info if info is not None else {
                "tag": "payRequest", "callback": callback, "minSendable": minimo, "maxSendable": maximo})
        if req.url.path == "/cb":
            sats = int(req.url.params["amount"]) // 1000
            return httpx.Response(200, json={"pr": invoice if invoice is not None else fazer_bolt11(sats)})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def pedir(transport, endereco="ana@carteira.com", sats=10):
    return asyncio.run(lnurl.invoice_para(endereco, sats, transport=transport))


def test_resolve_e_confere_valor():
    assert lnurl.decodificar_bolt11(pedir(carteira()))["msat"] == 10_000


def test_conecta_no_ip_verificado_com_nome_original():
    vistos = []
    pedir(carteira(vistos=vistos))
    for req in vistos:
        assert req.url.host == IP_CARTEIRA                    # sem nova resolução de DNS
        assert req.headers["host"] == "carteira.com"
        assert req.extensions["sni_hostname"] == "carteira.com"  # certificado conferido pelo nome


def test_valor_errado_na_invoice():
    with pytest.raises(lnurl.LnurlErro, match="valor diferente"):
        pedir(carteira(invoice=fazer_bolt11(300)))


def test_minimo_da_carteira():
    with pytest.raises(lnurl.LnurlErro, match="a partir de 20 sats"):
        pedir(carteira(minimo=20_000))


@pytest.mark.parametrize("callback, erro", [
    ("https://interno.lan/cb", "público"),
    ("http://carteira.com/cb", "HTTPS"),
    ("https://carteira.com:8443/cb", "443"),
    ("https://user:senha@carteira.com/cb", "HTTPS"),
])
def test_callback_inseguro_e_recusado(callback, erro):
    with pytest.raises(lnurl.LnurlErro, match=erro):
        pedir(carteira(callback=callback))


@pytest.mark.parametrize("info", [[], "texto", {"tag": "payRequest", "callback": 123},
                                  {"tag": "payRequest", "callback": "https://carteira.com/cb", "minSendable": "abc"},
                                  {"tag": "payRequest", "callback": "https://carteira.com/cb", "minSendable": [1]}])
def test_resposta_malformada_vira_lnurlerro(info):
    with pytest.raises(lnurl.LnurlErro):
        pedir(carteira(info=info))


def test_redirecionamento_nao_e_seguido():
    t = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "https://interno.lan/"}))
    with pytest.raises(lnurl.LnurlErro, match="redirecion"):
        pedir(t)


def test_prazo_total(monkeypatch):
    monkeypatch.setattr(lnurl, "PRAZO_TOTAL_S", 0.2)

    async def lento(req):
        await asyncio.sleep(2)
        return httpx.Response(200, json={})
    with pytest.raises(lnurl.LnurlErro, match="demorou"):
        pedir(httpx.MockTransport(lento))


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "0.0.0.0", "::1",
                                "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::1", "fd00::1"])
def test_resolver_bloqueia_rede_interna(monkeypatch, ip):
    monkeypatch.undo()  # usa o _resolver de verdade, com getaddrinfo falso

    async def getaddrinfo(host, porta, **kw):
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, porta))]

    async def rodar():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
        await lnurl._resolver("rebind.example")
    with pytest.raises(lnurl.LnurlErro, match="público"):
        asyncio.run(rodar())


def test_bolt11_conferencias():
    assert lnurl.decodificar_bolt11(fazer_bolt11(10))["msat"] == 10_000
    assert lnurl.decodificar_bolt11(fazer_bolt11(0))["msat"] is None
    with pytest.raises(lnurl.LnurlErro, match="checksum"):
        lnurl.decodificar_bolt11(fazer_bolt11(10, corromper=True))
    with pytest.raises(lnurl.LnurlErro, match="rede principal"):
        lnurl.decodificar_bolt11(fazer_bolt11(10, rede="tb"))
    with pytest.raises(lnurl.LnurlErro, match="vencida"):
        lnurl.conferir_invoice(fazer_bolt11(10, ts=1_600_000_000), 10_000)
    # rota privada com taxa normal passa (a Coinos e outras carteiras usam isso)
    assert lnurl.conferir_invoice(fazer_bolt11(10, rotas=[rota(1000, 1)]), 10_000, 2)
    # rota com taxa abusiva (quem cria a invoice escolhe) é recusada
    with pytest.raises(lnurl.LnurlErro, match="taxa de rota"):
        lnurl.conferir_invoice(fazer_bolt11(10, rotas=[rota(1000, 1), rota(5_000_000, 0)]), 10_000, 2)
    with pytest.raises(lnurl.LnurlErro, match="taxa de rota"):  # soma dos saltos da mesma rota
        lnurl.conferir_invoice(fazer_bolt11(10, rotas=[rota(1500, 0) + rota(1500, 0)]), 10_000, 2)
    with pytest.raises(lnurl.LnurlErro):
        lnurl.decodificar_bolt11("lnbc100n1fake")


@pytest.mark.parametrize("ruim", ["", "ana", "ana@", "@x.com", "a b@x.com", "ana@localhost", None, 123])
def test_endereco_invalido(ruim):
    with pytest.raises(lnurl.LnurlErro):
        lnurl.normalizar(ruim)


def test_normaliza():
    assert lnurl.normalizar("  Lightning:Ana@Carteira.COM ") == "ana@carteira.com"
