"""Interface em português, inglês e alemão: erros do servidor traduzidos e i18n.js servido com versão."""
import ast
import asyncio
import hashlib
import importlib
import pathlib
import re

import httpx
import pytest

from servidor import i18n

RAIZ = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def main(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    return importlib.import_module("servidor.main")


def _pedir(main, metodo, rota, **kw):
    async def rodar():
        t = httpx.ASGITransport(app=main.app, client=("189.6.10.20", 1))
        async with httpx.AsyncClient(transport=t, base_url="http://site") as c:
            return await c.request(metodo, rota, **kw)
    return asyncio.run(rodar())


def test_idioma_invalido_vira_portugues():
    assert [i18n.idioma_de(v) for v in ("en", "DE", "de-AT", "fr", "", None, "x" * 50)] == ["en", "de", "de", "pt", "pt", "pt", "pt"]


def test_traduz_mensagem_fixa_e_com_valores():
    assert i18n.traduzir("Valor inválido.", "en") == "Invalid amount."
    assert i18n.traduzir("Valor inválido.", "de") == "Ungültiger Betrag."
    assert i18n.traduzir("Valor inválido.", "pt") == "Valor inválido."
    assert i18n.traduzir("Parâmetro zoom fora da faixa (0.8 a 8).", "en") == "Parameter zoom is out of range (0.8 to 8)."
    assert i18n.traduzir("Envio grande demais (máximo 16 KB).", "de") == "Upload zu groß (maximal 16 KB)."
    assert i18n.traduzir("A carteira desse endereço só aceita a partir de 5 sats.", "en").endswith("5 sats or more.")
    assert i18n.traduzir("Não encontrei o domínio exemplo.com.", "de") == "Die Domain exemplo.com wurde nicht gefunden."
    assert i18n.traduzir("Mensagem desconhecida.", "en") == "Mensagem desconhecida."


def _mensagens_do_codigo():
    """Todo texto fixo em português que o código manda para o visitante (ou deixa o servidor devolver)."""
    construtores = {"Recusado", "ImagemInvalida", "ParametroInvalido", "LnurlErro", "ResgateErro", "limitar"}
    achados = set()
    for arq in ("servidor/main.py", "servidor/render.py", "servidor/recompensas.py", "servidor/lnurl.py",
                "motor/forma_imagem.py"):
        arvore = ast.parse((RAIZ / arq).read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Call):
                nome = getattr(no.func, "id", getattr(no.func, "attr", ""))
                if nome in construtores or nome == "HTTPException":
                    for a in no.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str) and " " in a.value:
                            achados.add(a.value)
            elif isinstance(no, ast.Assign) and isinstance(no.value, ast.Constant) and isinstance(no.value.value, str):
                if getattr(no.targets[0], "id", "") in {"EXCESSO", "complexa", "malformada"}:
                    achados.add(no.value.value)
            elif isinstance(no, ast.Dict) and arq.endswith("recompensas.py"):
                achados.update(v.value for v in no.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
                               and v.value.endswith(("!", ".", "?")))
    return achados


def test_toda_mensagem_de_erro_do_codigo_tem_traducao():
    # "complexa" e "malformada" são montadas com duas linhas de texto: o ast já junta
    sem = sorted(m for m in _mensagens_do_codigo()
                 if i18n.traduzir(m, "en") == m and i18n.traduzir(m, "de") == m)
    assert not sem, sem


def test_erro_da_api_sai_no_idioma_pedido(main):
    # Content-Type errado: erro 415 vindo do middleware
    r = _pedir(main, "POST", "/api/preview", content=b"x", headers={"Content-Type": "text/plain", "X-Idioma": "en"})
    assert r.status_code == 415 and r.json() == {"erro": "Invalid request."}
    # corpo que não é JSON: erro 400 vindo do handler de Recusado
    r = _pedir(main, "POST", "/api/preview", content=b"[1]", headers={"Content-Type": "application/json", "X-Idioma": "de"})
    assert r.status_code == 400 and r.json() == {"erro": "Ungültige Anfrage."}
    # parâmetro com valor no meio da mensagem
    r = _pedir(main, "POST", "/api/preview", json={"zoom": 99}, headers={"X-Idioma": "en"})
    assert r.status_code == 400 and r.json()["erro"].startswith("Parameter zoom is out of range")


def test_sem_cabecalho_ou_com_idioma_estranho_continua_em_portugues(main):
    for cab in ({}, {"X-Idioma": "fr"}, {"X-Idioma": "pt"}):
        r = _pedir(main, "POST", "/api/preview", content=b"x", headers={"Content-Type": "text/plain", **cab})
        assert r.json() == {"erro": "Pedido inválido."}


def test_pagina_pede_i18n_js_e_app_js_pela_versao(main):
    v = lambda nome: hashlib.sha256((RAIZ / "static" / nome).read_bytes()).hexdigest()[:12]
    pagina = _pedir(main, "GET", "/")
    assert f'src="/i18n.js?v={v("i18n.js")}"' in pagina.text and f'src="/app.js?v={v("app.js")}"' in pagina.text
    assert 'src="/i18n.js"' not in pagina.text
    assert pagina.text.index("/i18n.js?v=") < pagina.text.index("/app.js?v=")  # o app.js usa o que o i18n.js define
    js = _pedir(main, "GET", f"/i18n.js?v={v('i18n.js')}")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert js.headers["cache-control"] == "no-cache" and "content-security-policy" in js.headers
    assert "const TEXTOS" in js.text


def _chaves_do_dicionario(idioma):
    js = (RAIZ / "static" / "i18n.js").read_text(encoding="utf-8")
    bloco = re.search(rf"^  {idioma}: \{{\n(.*?)^  \}},?\n", js, re.S | re.M).group(1)
    return re.findall(r'^    "([\w.]+)":', bloco, re.M)


def test_os_tres_idiomas_tem_as_mesmas_chaves_e_a_pagina_so_usa_chaves_que_existem():
    pt = _chaves_do_dicionario("pt")
    assert len(pt) > 150 and len(set(pt)) == len(pt)
    assert set(_chaves_do_dicionario("en")) == set(pt) == set(_chaves_do_dicionario("de"))
    usadas = set()
    for arq in ("index.html", "app.js"):
        texto = (RAIZ / "static" / arq).read_text(encoding="utf-8")
        usadas |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([\w.]+)"', texto))
        usadas |= set(re.findall(r'\b(?:t|marcar\(\$\("\w+"\)|marcar\(\w+|K)\(?\s*"([a-z0-9_]+\.[\w.]+|marca|dica|fechar|ordinal)"', texto))
    usadas.discard("sl.")  # as chaves dos sliders vêm da tabela SLIDERS
    faltam = {k for k in usadas if k not in pt}
    assert not faltam, faltam
