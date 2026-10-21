"""Testes de regressão das duas primeiras auditorias de segurança (S01-S15, X01-X05).

Cada teste reproduz um ataque que funcionava antes e confirma que agora é
barrado. Tudo roda local, com uma Coinos falsa; nada toca a carteira real.
"""
import asyncio
import io
import json
import pathlib

import httpx
import numpy as np
import pytest
from fastapi import FastAPI, Request, Response
from PIL import Image

from servidor import antiabuso, lnurl
from servidor.coinos import Coinos
from servidor.config import Config
from servidor.db import DB
from servidor.recompensas import Recompensas, ResgateErro, hoje
from servidor.render import ImagemInvalida, ParametroInvalido, Renderizador

RAIZ = pathlib.Path(__file__).resolve().parent.parent


class CoinosFalsa:
    def __init__(self):
        self.app = FastAPI()
        self.balance = 100_000
        self.pagos = []
        self.consultas_invoice = 0
        self.falhar_apos_enviar = False
        self.taxa_pagamento = 0
        self.barreira = False
        self.entradas = 0
        self.liberar = asyncio.Event()

        @self.app.get("/me")
        async def me():
            if self.barreira:
                self.entradas += 1
                if self.entradas == 2:
                    self.liberar.set()
                await asyncio.wait_for(self.liberar.wait(), 2)
            return {"balance": self.balance}

        @self.app.post("/send")
        async def send(request: Request):
            d = await request.json()
            self.pagos.append((d["username"], d["amount"]))
            self.balance -= d["amount"]
            if self.falhar_apos_enviar:
                self.falhar_apos_enviar = False
                return Response("falha apos debito " + request.headers["authorization"], status_code=400)
            return {"id": str(len(self.pagos))}

        @self.app.post("/payments")
        async def payments(request: Request):
            d = await request.json()
            self.pagos.append(("bolt11", d["payreq"]))
            self.balance -= self.taxa_pagamento
            return {"hash": str(len(self.pagos)), "fee": self.taxa_pagamento}

        @self.app.post("/invoice")
        async def invoice(request: Request):
            d = await request.json()
            sats = d["invoice"]["amount"]
            return {"id": "fake-invoice", "text": f"lnbc{sats * 10}n1fake"}

        @self.app.get("/invoice/{invoice_id}")
        async def invoice_status(invoice_id: str):
            self.consultas_invoice += 1
            return {"received": False}

    def client(self):
        return Coinos("FAKE_ONLY", "http://fake.local", transport=httpx.ASGITransport(app=self.app))


def criar_coinos_falsa_local():
    """Para teste HTTP: uvicorn test_auditoria_seguranca:criar_coinos_falsa_local --factory --app-dir tests."""
    return CoinosFalsa().app


def prova(cfg):
    return {**antiabuso.criar_desafio(cfg.hmac_key, cfg.pow_bits), "nonce": "0"}


def cfg_teste(**kw):
    opcoes = {"coinos_token": "FAKE_ONLY", "hmac_key": "audit", "pow_bits": 0, "intervalo_min_s": 0, **kw}
    return Config(**opcoes)


async def invoice_falsa(_endereco, sats, **_kw):
    return f"lnbc{sats * 10}n1fake"


# ---------- S02: limites atômicos entre processos ----------
def test_dois_workers_nao_estouram_limites(tmp_path):
    async def ataque():
        path = str(tmp_path / "audit.db")
        fake = CoinosFalsa()
        cfg = cfg_teste(recompensas_seq=[10], limite_dia_global=15)
        a = Recompensas(cfg, DB(path), fake.client())
        b = Recompensas(cfg, DB(path), fake.client())
        ta, tb = a.emitir_token(), b.emitir_token()
        fake.barreira = True
        try:
            results = await asyncio.gather(
                a.resgatar(ta, "ana@coinos.io", "ip-a", prova(cfg)),
                b.resgatar(tb, "ana@coinos.io", "ip-b", prova(cfg)),
                return_exceptions=True,
            )
            assert sum(isinstance(x, dict) for x in results) == 1
            assert sum(isinstance(x, ResgateErro) for x in results) == 1
            assert fake.pagos == [("ana", 10)]
            assert DB(path).soma_dia(hoje()) == 10
        finally:
            await a.coinos.fechar()
            await b.coinos.fechar()
    asyncio.run(ataque())


def test_mesmo_token_so_pode_ser_usado_uma_vez_entre_conexoes(tmp_path):
    async def ataque():
        path = str(tmp_path / "mesmo_token.db")
        fake = CoinosFalsa()
        cfg = cfg_teste()
        a = Recompensas(cfg, DB(path), fake.client())
        b = Recompensas(cfg, DB(path), fake.client())
        token = a.emitir_token()
        fake.barreira = True
        try:
            results = await asyncio.gather(
                a.resgatar(token, "ana@coinos.io", "ip-a", prova(cfg)),
                b.resgatar(token, "bob@coinos.io", "ip-b", prova(cfg)),
                return_exceptions=True,
            )
            assert sum(isinstance(x, ResgateErro) for x in results) == 1
            assert len(fake.pagos) == 1
        finally:
            await a.coinos.fechar()
            await b.coinos.fechar()
    asyncio.run(ataque())


# ---------- S01 / A2: IP do visitante ----------
PROXY = antiabuso.redes("127.0.0.0/8,172.16.0.0/12")


def test_xff_de_cliente_direto_e_ignorado():
    assert antiabuso.ip_cliente("203.0.113.7", "198.51.100.99", PROXY) == "203.0.113.7"
    assert antiabuso.ip_cliente("203.0.113.7", "198.51.100.99", []) == "203.0.113.7"


def test_xff_atras_do_proxy_usa_o_valor_da_direita():
    # o cliente inventa "1.2.3.4"; o proxy acrescenta o IP real no fim
    assert antiabuso.ip_cliente("172.18.0.1", "1.2.3.4, 203.0.113.7", PROXY) == "203.0.113.7"
    assert antiabuso.ip_cliente("172.18.0.1", "1.2.3.4, 203.0.113.7, 172.18.0.5", PROXY) == "203.0.113.7"
    assert antiabuso.ip_cliente("172.18.0.1", "lixo", PROXY) == "172.18.0.1"


def test_uvicorn_nao_confia_em_cabecalhos_de_proxy():
    dockerfile = (RAIZ / "Dockerfile").read_text()
    assert "--forwarded-allow-ips" not in dockerfile
    assert "--no-proxy-headers" in dockerfile


def test_ipv6_do_mesmo_64_conta_como_um_so():
    a = antiabuso.hash_ip("s", "2001:db8:1:2::1")
    b = antiabuso.hash_ip("s", "2001:db8:1:2:ffff:ffff:ffff:ffff")
    c = antiabuso.hash_ip("s", "2001:db8:1:3::1")
    assert a == b != c
    assert antiabuso.hash_ip("s", "::ffff:203.0.113.7") == antiabuso.hash_ip("s", "203.0.113.7")


# ---------- S10: limitador ----------
def test_limitador_tem_teto_de_chaves_e_nao_esquece_limites_ativos():
    lim = antiabuso.Limitador(1, 600, max_chaves=1000)
    assert lim.permitir("vitima")
    assert not lim.permitir("vitima")
    aceitas = sum(lim.permitir(f"ip-falso-{i}") for i in range(10_000))
    assert aceitas == 999 and len(lim.eventos) == 1000      # memória limitada
    assert not lim.permitir("vitima")                         # inundar não zera a vítima


# ---------- S04 / M4: imagens ----------
def _png(arr_ou_im):
    out = io.BytesIO()
    (arr_ou_im if isinstance(arr_ou_im, Image.Image) else Image.fromarray(arr_ou_im)).save(out, "PNG")
    return out.getvalue()


def test_upload_de_100_milhoes_de_pixels_e_recusado():
    payload = _png(Image.new("1", (10_000, 10_000)))
    assert len(payload) < 8 * 1024 * 1024
    with pytest.raises(ImagemInvalida):
        Renderizador(1).guardar_imagem(payload)


def test_imagem_guardada_e_reduzida():
    r = Renderizador(1)
    img_id = r.guardar_imagem(_png(Image.new("RGB", (4000, 3000), "white")))
    with Image.open(io.BytesIO(r.imagens.pegar(img_id))) as im:
        assert max(im.size) <= 1024


@pytest.mark.parametrize("arr", [
    (np.tile((np.arange(360) % 3 == 0), (360, 1)) * 255).astype(np.uint8),           # listras de 1 px
    ((np.indices((360, 360)) % 3 == 0).all(axis=0) * 255).astype(np.uint8),        # pontos
    (np.random.default_rng(1).random((400, 400)) > 0.5).astype(np.uint8) * 255,     # ruído
])
def test_imagem_complexa_demais_e_recusada(arr):
    r = Renderizador(1)
    img_id = r.guardar_imagem(_png(arr))
    with pytest.raises(ImagemInvalida, match="detalhes demais"):
        r._gerar({"fonte": "imagem", "imagem_id": img_id}, 640)


def test_fila_cheia_responde_ocupado():
    from servidor.render import Ocupado

    async def ataque():
        r = Renderizador(1, max_fila=0)
        r.na_fila = 1
        with pytest.raises(Ocupado):
            await r.gerar({"fonte": "sat"}, 640)
    asyncio.run(ataque())


@pytest.mark.parametrize("p", [{"zoom": float("nan")}, {"raio": float("inf")}, {"angulo": 1e9},
                               {"zoom": "2"}, {"seed": -1}, {"seed": 2 ** 40}, {"circulo": "sim"},
                               {"cor": "#zzzzzz"}, {"malha": 1}])
def test_parametros_absurdos_sao_recusados(p):
    with pytest.raises(ParametroInvalido):
        Renderizador.ler_config(p)


# ---------- S08: status de doação ----------
def test_status_doacao_tem_cache(tmp_path):
    async def ataque():
        fake = CoinosFalsa()
        client = fake.client()
        db = DB(str(tmp_path / "doacao.db"))
        db.nova_doacao("fake-invoice", 1)
        r = Recompensas(cfg_teste(), db, client)
        try:
            for _ in range(100):
                assert not await r.doacao_paga("fake-invoice")
            assert fake.consultas_invoice == 1
            db.marcar_doacao_paga("fake-invoice")
            r._status_doacao.clear()
            assert await r.doacao_paga("fake-invoice")
            assert fake.consultas_invoice == 1  # já paga: nem consulta
        finally:
            await client.fechar()
    asyncio.run(ataque())


# ---------- S12 + S05: erros da Coinos ----------
def test_erro_da_coinos_nunca_chega_ao_visitante_nem_libera_token(tmp_path):
    async def ataque():
        fake = CoinosFalsa()
        fake.falhar_apos_enviar = True  # debita, responde 400 e ainda ecoa o Authorization
        client = fake.client()
        cfg = cfg_teste()
        r = Recompensas(cfg, DB(str(tmp_path / "erro400.db")), client)
        token = r.emitir_token()
        try:
            with pytest.raises(ResgateErro) as exc:
                await r.resgatar(token, "ana@coinos.io", "ip-a", prova(cfg))
            assert "Bearer" not in str(exc.value) and "FAKE_ONLY" not in str(exc.value)
            assert "confirmar" in str(exc.value)
            with pytest.raises(ResgateErro, match="já foi resgatada"):
                await r.resgatar(token, "bob@coinos.io", "ip-b", prova(cfg))
            assert fake.pagos == [("ana", 10)]
        finally:
            await client.fechar()
    asyncio.run(ataque())


# ---------- S03: carteira lenta ----------
def test_lnurl_lenta_nao_bloqueia_os_outros_resgates(tmp_path):
    async def ataque():
        fake = CoinosFalsa()
        client = fake.client()
        cfg = cfg_teste(pagamento_externo=True)
        chegou, liberar = asyncio.Event(), asyncio.Event()

        async def invoice_lenta(_endereco, sats, **_kw):
            chegou.set()
            await liberar.wait()
            return f"lnbc{sats * 10}n1fake"

        r = Recompensas(cfg, DB(str(tmp_path / "lento.db")), client, invoice_para=invoice_lenta)
        primeiro = asyncio.create_task(r.resgatar(r.emitir_token(), "ana@wallet.example", "ip-a", prova(cfg)))
        try:
            await asyncio.wait_for(chegou.wait(), 1)
            segundo = await asyncio.wait_for(
                r.resgatar(r.emitir_token(), "bob@coinos.io", "ip-b", prova(cfg)), 1)
            assert segundo["sats"] == 10
            assert not primeiro.done()
            liberar.set()
            await primeiro
        finally:
            liberar.set()
            await client.fechar()
    asyncio.run(ataque())


# ---------- S06: taxa ----------
def test_pagamento_externo_desligado_por_padrao(tmp_path):
    async def ataque():
        fake = CoinosFalsa()
        client = fake.client()
        cfg = cfg_teste()
        r = Recompensas(cfg, DB(str(tmp_path / "ext.db")), client, invoice_para=invoice_falsa)
        try:
            assert cfg.pagamento_externo is False
            with pytest.raises(ResgateErro, match="@coinos.io"):
                await r.resgatar(r.emitir_token(), "ana@wallet.example", "ip-a", prova(cfg))
            assert fake.pagos == []
        finally:
            await client.fechar()
    asyncio.run(ataque())


def test_taxa_acima_do_teto_pausa_pagamentos_externos(tmp_path):
    async def ataque():
        fake = CoinosFalsa()
        fake.taxa_pagamento = 1000
        client = fake.client()
        cfg = cfg_teste(pagamento_externo=True, limite_dia_global=1000, taxa_max=2)
        r = Recompensas(cfg, DB(str(tmp_path / "taxa.db")), client, invoice_para=invoice_falsa)
        try:
            await r.resgatar(r.emitir_token(), "ana@wallet.example", "ip-a", prova(cfg))
            with pytest.raises(ResgateErro, match="@coinos.io"):
                await r.resgatar(r.emitir_token(), "bob@wallet.example", "ip-b", prova(cfg))
            assert len(fake.pagos) == 1
        finally:
            await client.fechar()
    asyncio.run(ataque())


# ---------- S07 / S13: LNURL ----------
def test_lnurl_recusa_callback_em_porta_arbitraria(monkeypatch):
    import ipaddress

    async def publico(_host):
        return ipaddress.ip_address("203.0.113.10")

    monkeypatch.setattr(lnurl, "_resolver", publico)

    def handler(request):
        if request.url.path.startswith("/.well-known/"):
            return httpx.Response(200, json={"tag": "payRequest",
                                             "callback": "https://wallet.example:8443/cb", "minSendable": 1000})
        return httpx.Response(200, json={"pr": "lnbc100n1fake"})

    with pytest.raises(lnurl.LnurlErro, match="443"):
        asyncio.run(lnurl.invoice_para("ana@wallet.example", 10, transport=httpx.MockTransport(handler)))


def test_resposta_lnurl_tipo_errado_vira_erro_tratado(monkeypatch):
    import ipaddress

    async def publico(_host):
        return ipaddress.ip_address("203.0.113.10")

    monkeypatch.setattr(lnurl, "_resolver", publico)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=[]))
    with pytest.raises(lnurl.LnurlErro):
        asyncio.run(lnurl.invoice_para("ana@wallet.example", 10, transport=transport))


def test_lnurl_malformado_nao_queima_teto_global(tmp_path):
    async def ataque():
        fake = CoinosFalsa()
        client = fake.client()
        cfg = cfg_teste(pagamento_externo=True, limite_dia_global=15)

        async def invoice_malformado(_endereco, _sats, **_kw):
            raise AttributeError("'list' object has no attribute 'get'")

        db = DB(str(tmp_path / "malformado.db"))
        r = Recompensas(cfg, db, client, invoice_para=invoice_malformado)
        try:
            with pytest.raises(lnurl.LnurlErro):
                await r.resgatar(r.emitir_token(), "ana@wallet.example", "ip-a", prova(cfg))
            assert fake.pagos == [] and db.soma_dia(hoje()) == 0
            res = await r.resgatar(r.emitir_token(), "bob@coinos.io", "ip-b", prova(cfg))
            assert res["sats"] == 10
        finally:
            await client.fechar()
    asyncio.run(ataque())


# ---------- S09: chaves fixas ----------
def test_recompensas_exigem_chaves_fixas(monkeypatch):
    monkeypatch.delenv("POW_HMAC_KEY", raising=False)
    monkeypatch.delenv("IP_SALT", raising=False)
    with pytest.raises(RuntimeError, match="POW_HMAC_KEY"):
        Config(coinos_token="FAKE_ONLY").validar()
    monkeypatch.setenv("POW_HMAC_KEY", "a" * 64)
    monkeypatch.setenv("IP_SALT", "b" * 64)
    Config(coinos_token="FAKE_ONLY").validar()
    Config(coinos_token="").validar()  # sem recompensas não exige nada


# ---------- HTTP (app sem recompensas) ----------
@pytest.fixture
def cliente_http(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    from servidor.main import app

    async def chamar(metodo, rota, **kw):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as c:
            return await c.request(metodo, rota, **kw)
    return lambda *a, **kw: asyncio.run(chamar(*a, **kw))


def test_largura_invalida_retorna_400(cliente_http):
    assert cliente_http("POST", "/api/exportar", json={"largura": "abc"}).status_code == 400


def test_json_com_nan_retorna_400(cliente_http):
    r = cliente_http("POST", "/api/preview", content=b'{"zoom": NaN}', headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_corpo_grande_e_recusado_pelo_content_length(cliente_http):
    r = cliente_http("POST", "/api/imagem", content=b"x" * (9 * 1024 * 1024), headers={"content-type": "image/png"})
    assert r.status_code == 413
    r = cliente_http("POST", "/api/preview", content=json.dumps({"x": "y" * 20000}),
                     headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_cabecalhos_de_seguranca(cliente_http):
    r = cliente_http("GET", "/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"].split("script-src")[1].split(";")[0]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "<script>" not in r.text  # nenhum script embutido na página


def test_qr_svg_nao_insere_texto_hostil_como_markup(cliente_http):
    from servidor.main import qr_svg

    svg = qr_svg('lightning:<script>alert(1)</script>')
    assert "<svg" in svg
    assert "<script" not in svg
    assert "alert(1)" not in svg
