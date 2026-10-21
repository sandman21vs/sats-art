"""Proxy (Caddy, Cloudflare Tunnel) mal configurado: todo mundo vira um só IP. O log precisa avisar."""
import asyncio
import importlib
import logging

import httpx

from servidor import antiabuso

SEGREDO = "s" * 40


def _main(monkeypatch, tmp_path, proxies="127.0.0.0/8,::1/128,172.16.0.0/12"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    monkeypatch.setattr(main.cfg, "trust_proxy", True)
    monkeypatch.setattr(main.cfg, "proxy_secret", SEGREDO)
    monkeypatch.setattr(main, "PROXIES", antiabuso.redes(proxies))
    monkeypatch.setattr(main, "_avisos_proxy", {})
    return main


def _pedir(main, conexao, cabecalhos, n=1):
    async def rodar():
        t = httpx.ASGITransport(app=main.app, client=(conexao, 1))
        async with httpx.AsyncClient(transport=t, base_url="http://site") as c:
            for _ in range(n):
                await c.get("/api/info", headers=cabecalhos)
    asyncio.run(rodar())


def _eventos(caplog):
    return [r.dados["evento"] for r in caplog.records if getattr(r, "dados", None)]


def test_cloudflared_sem_segredo_avisa_que_todos_viram_um_ip(monkeypatch, tmp_path, caplog):
    main = _main(monkeypatch, tmp_path)
    with caplog.at_level(logging.INFO):
        _pedir(main, "172.18.0.5", {"x-forwarded-for": "189.6.10.20"}, n=5)
    assert _eventos(caplog).count("proxy_sem_segredo") == 1   # uma vez a cada 10 min, não a cada pedido
    assert "189.6.10.20" not in caplog.text


def test_cloudflared_fora_de_trusted_proxies_avisa(monkeypatch, tmp_path, caplog):
    main = _main(monkeypatch, tmp_path)
    with caplog.at_level(logging.INFO):
        _pedir(main, "192.168.50.2", {"x-forwarded-for": "189.6.10.20", "x-proxy-secret": SEGREDO})
    assert "proxy_fora_da_lista" in _eventos(caplog)


def test_cloudflared_bem_configurado_usa_ip_do_visitante_sem_aviso(monkeypatch, tmp_path, caplog):
    main = _main(monkeypatch, tmp_path)
    vistos = []
    original = main.ip_de
    monkeypatch.setattr(main, "ip_de", lambda req: vistos.append(original(req)) or vistos[-1])
    with caplog.at_level(logging.INFO):
        # o Cloudflare acrescenta o IP real à direita do que o visitante mandou
        _pedir(main, "172.18.0.5", {"x-forwarded-for": "9.9.9.9, 189.6.10.20", "x-proxy-secret": SEGREDO})
    assert set(vistos) == {"189.6.10.20"}
    assert not {"proxy_sem_segredo", "proxy_fora_da_lista", "segredo_proxy_invalido"} & set(_eventos(caplog))


def test_visitante_direto_sem_proxy_nao_gera_aviso(monkeypatch, tmp_path, caplog):
    main = _main(monkeypatch, tmp_path)
    with caplog.at_level(logging.INFO):
        _pedir(main, "203.0.113.9", {"x-forwarded-for": "1.2.3.4"})  # cliente direto tentando forjar
    assert "proxy_sem_segredo" not in _eventos(caplog)


def test_pagina_pede_o_app_js_com_a_versao(monkeypatch, tmp_path):
    """O Cloudflare guarda o app.js por horas no navegador: a página precisa pedir pela versão."""
    import hashlib
    import pathlib

    main = _main(monkeypatch, tmp_path)
    raiz = pathlib.Path(__file__).resolve().parent.parent
    versao = hashlib.sha256((raiz / "static" / "app.js").read_bytes()).hexdigest()[:12]

    async def rodar():
        t = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=t, base_url="http://site") as c:
            return await c.get("/"), await c.get(f"/app.js?v={versao}")
    pagina, js = asyncio.run(rodar())
    assert f'src="/app.js?v={versao}"' in pagina.text and 'src="/app.js"' not in pagina.text
    assert pagina.headers["cache-control"] == "no-store"
    assert "content-security-policy" in pagina.headers
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
