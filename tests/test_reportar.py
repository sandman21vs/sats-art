"""Botão "Reportar problema": o relato chega ao Telegram do dono, sem vazar segredos nem virar spam."""
import asyncio
import importlib
import logging

import httpx
import pytest

from servidor import antiabuso, telegram as tg

TOKEN = "123456:SEGREDO-DO-BOT"


class TelegramFalso:
    def __init__(self, falhar=False):
        self.enviadas, self.falhar = [], falhar

    async def enviar(self, texto):
        if self.falhar:
            raise tg.TelegramErro("status 500")
        self.enviadas.append(texto)


@pytest.fixture
def main(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    m = importlib.import_module("servidor.main")
    monkeypatch.setattr(m, "telegram", TelegramFalso())
    monkeypatch.setattr(m, "lim_report", antiabuso.Limitador(3, 600))
    monkeypatch.setattr(m, "lim_report_global", antiabuso.Limitador(30, 3600))
    monkeypatch.setattr(m, "lim_info", antiabuso.Limitador(60, 60))
    monkeypatch.setattr(m, "_info_cache", (float("-inf"), None))
    return m


def _post(main, corpo, ip="189.6.10.20", n=1, cabecalhos=None):
    async def rodar():
        t = httpx.ASGITransport(app=main.app, client=(ip, 1))
        async with httpx.AsyncClient(transport=t, base_url="http://site") as c:
            return [await c.post("/api/reportar", json=corpo, headers=cabecalhos or {}) for _ in range(n)]
    return asyncio.run(rodar())


def _info(main):
    async def rodar():
        t = httpx.ASGITransport(app=main.app, client=("189.6.10.20", 1))
        async with httpx.AsyncClient(transport=t, base_url="http://site") as c:
            return (await c.get("/api/info")).json()
    return asyncio.run(rodar())


def test_relato_chega_no_telegram_com_contexto(main, caplog):
    with caplog.at_level(logging.INFO):
        r, = _post(main, {"tipos": ["sats_nao_chegaram", "erro_resgate"], "mensagem": "Exportei e nada.",
                          "contato": "@fulano", "erro": "Não consegui falar com a carteira.",
                          "retrato": True, "tela": "390x844"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    texto, = main.telegram.enviadas
    for trecho in ("Não recebi os sats", "Deu erro ao resgatar", "Exportei e nada.", "@fulano",
                   "Não consegui falar com a carteira.", "em pé", "390x844",
                   antiabuso.hash_ip(main.cfg.ip_salt, "189.6.10.20")[:12]):
        assert trecho in texto
    assert "189.6.10.20" not in texto            # nem o IP cru vai para o Telegram
    assert "Exportei e nada." not in caplog.text  # o texto do relato não vai para o log
    assert "report_enviado" in caplog.text


def test_desligado_sem_bot_configurado(main, monkeypatch):
    monkeypatch.setattr(main, "telegram", None)
    assert _post(main, {"mensagem": "oi"})[0].status_code == 404
    assert _info(main)["report_tipos"] == []


def test_info_lista_os_tipos_quando_ligado(main):
    tipos = _info(main)["report_tipos"]
    assert ["outro", "Outra coisa"] in tipos


@pytest.mark.parametrize("corpo", [
    {},                                             # nada marcado nem escrito
    {"mensagem": "   \n  "},
    {"tipos": ["inventado"]},                       # só chaves conhecidas
    {"tipos": "outro"},
    {"tipos": [1]},
    {"mensagem": ["lista"]},
    {"tipos": ["outro"], "contato": {"x": 1}},
])
def test_pedidos_invalidos(main, corpo):
    assert _post(main, corpo)[0].status_code == 400
    assert main.telegram.enviadas == []


def test_limpa_caracteres_de_controle_e_corta(main):
    _post(main, {"mensagem": "a\x00b‮c\r\n\n\n\n\nd" + "x" * 5000, "contato": "um\ndois"})
    texto, = main.telegram.enviadas
    assert "\x00" not in texto and "‮" not in texto
    assert "abc\n\nd" in texto and "x" * main.MAX_REPORT_TEXTO not in texto
    assert "Contato: um dois" in texto
    assert len(texto) <= tg.LIMITE_TEXTO


def test_limite_por_pessoa(main):
    rs = _post(main, {"tipos": ["outro"]}, n=4)
    assert [r.status_code for r in rs] == [200, 200, 200, 429]
    assert len(main.telegram.enviadas) == 3


def test_limite_global_segura_quem_troca_de_ip(main, monkeypatch):
    monkeypatch.setattr(main, "lim_report_global", antiabuso.Limitador(2, 3600))
    codigos = [_post(main, {"tipos": ["outro"]}, ip=f"200.0.0.{i}")[0].status_code for i in range(4)]
    assert codigos == [200, 200, 429, 429]


def test_pedido_invalido_nao_gasta_o_limite_global(main, monkeypatch):
    monkeypatch.setattr(main, "lim_report_global", antiabuso.Limitador(1, 3600))
    _post(main, {"tipos": ["inventado"]}, ip="200.0.0.1")
    assert _post(main, {"tipos": ["outro"]}, ip="200.0.0.2")[0].status_code == 200


def test_outra_origem_e_recusada(main):
    r, = _post(main, {"tipos": ["outro"]}, cabecalhos={"origin": "https://malvado.example"})
    assert r.status_code == 403 and main.telegram.enviadas == []


def test_falha_do_telegram_avisa_a_pessoa_e_o_log(main, monkeypatch, caplog):
    monkeypatch.setattr(main, "telegram", TelegramFalso(falhar=True))
    with caplog.at_level(logging.INFO):
        r, = _post(main, {"tipos": ["outro"]})
    assert r.status_code == 502
    assert "report_falhou" in caplog.text


# ---------- cliente do Telegram ----------
def _cliente(handler):
    return tg.Telegram(TOKEN, "42", transport=httpx.MockTransport(handler))


def test_cliente_manda_texto_puro_para_o_chat():
    vistos = []

    def handler(req):
        vistos.append(req)
        return httpx.Response(200, json={"ok": True, "result": {}})
    asyncio.run(_cliente(handler).enviar("oi <b>x</b>"))
    req, = vistos
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    corpo = __import__("json").loads(req.content)
    assert corpo["chat_id"] == "42" and corpo["text"] == "oi <b>x</b>" and "parse_mode" not in corpo


@pytest.mark.parametrize("handler", [
    lambda req: (_ for _ in ()).throw(httpx.ConnectError(f"falhou {req.url}", request=req)),
    lambda req: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
    lambda req: httpx.Response(200, text="não é json"),
    lambda req: httpx.Response(200, json={"ok": False}),
])
def test_erros_do_cliente_nunca_carregam_o_token(handler):
    with pytest.raises(tg.TelegramErro) as e:
        asyncio.run(_cliente(handler).enviar("oi"))
    assert TOKEN not in str(e.value) and e.value.__cause__ is None
