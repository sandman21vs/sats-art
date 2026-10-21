"""Regressão da auditoria rodada 4 (R4-01 a R4-14). Só APIs falsas."""
import asyncio
import gzip
import importlib
import ipaddress
import json
import logging
import os
import time
import tracemalloc

import httpx
import pytest

from servidor import antiabuso, eventos, lnurl
from servidor.coinos import Coinos, CoinosErro
from servidor.config import Config
from servidor.db import DB
from servidor.recompensas import Recompensas, ResgateErro, hoje
from servidor.render import Excesso, Renderizador
from tests.test_auditoria_seguranca import CoinosFalsa


def cfg_teste(**kw):
    opcoes = {"coinos_token": "FAKE_ONLY", "hmac_key": "a" * 40, "ip_salt": "b" * 40,
              "pow_bits": 0, "intervalo_min_s": 0, **kw}
    return Config(**opcoes)


def prova(cfg):
    return {**antiabuso.criar_desafio(cfg.hmac_key, cfg.pow_bits), "nonce": "0"}


def _main(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    return importlib.import_module("servidor.main")


def _ligar_recompensas(monkeypatch, main, tmp_path, **kw):
    falsa = CoinosFalsa()
    cfg = cfg_teste(**kw)
    r = Recompensas(cfg, DB(str(tmp_path / "r4.db")), falsa.client())
    monkeypatch.setattr(main, "recompensas", r)
    monkeypatch.setattr(main, "cfg", cfg)
    for nome in ("lim_resgate", "lim_resgate_global", "lim_export", "lim_desafio", "lim_doacao",
                 "lim_doacao_global", "lim_info", "lim_meu_dia"):
        lim = getattr(main, nome)
        monkeypatch.setattr(main, nome, antiabuso.Limitador(lim.n, lim.janela))
    monkeypatch.setattr(main, "_info_cache", (float("-inf"), None))
    return falsa, r, cfg


def _cliente(main, ip):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=(ip, 1)), base_url="http://site")


# ---------- R4-01: CSRF ----------
def test_post_de_outro_site_e_recusado(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    falsa, r, cfg = _ligar_recompensas(monkeypatch, main, tmp_path)
    cota = antiabuso.hash_cota(cfg.ip_salt, "198.51.100.7")

    async def ataque():
        async with _cliente(main, "198.51.100.7") as c:
            corpo = json.dumps({"token": r.emitir_token(cota), "endereco": "atacante@coinos.io", "prova": prova(cfg)})
            # fetch "no-cors" de outro site: Origin e Sec-Fetch-Site denunciam
            x = await c.post("/api/resgatar", content=corpo, headers={
                "content-type": "application/json", "origin": "https://site-malicioso.example"})
            assert x.status_code == 403
            x = await c.post("/api/resgatar", content=corpo, headers={
                "content-type": "application/json", "sec-fetch-site": "cross-site"})
            assert x.status_code == 403
            # pedido "simples" (text/plain, sem preflight) também não passa
            x = await c.post("/api/resgatar", content=corpo, headers={"content-type": "text/plain;charset=UTF-8"})
            assert x.status_code == 415
            for rota in ("/api/doar", "/api/exportar", "/api/preview"):
                assert (await c.post(rota, content="{}", headers={"origin": "https://x.example",
                                                                  "content-type": "application/json"})).status_code == 403
            # a própria página continua funcionando
            x = await c.post("/api/resgatar", content=corpo, headers={
                "content-type": "application/json", "origin": "http://site", "sec-fetch-site": "same-origin"})
            assert x.status_code == 200
        assert [u for u, _ in falsa.pagos] == ["atacante"]
        await r.coinos.fechar()
    asyncio.run(ataque())


def test_token_so_vale_para_quem_exportou(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    falsa, r, cfg = _ligar_recompensas(monkeypatch, main, tmp_path)

    async def ataque():
        async with _cliente(main, "203.0.113.66") as atacante:
            exp = await atacante.post("/api/exportar", json={"fonte": "sat", "largura": 640})
            token = exp.headers["X-Token-Resgate"]
        async with _cliente(main, "198.51.100.7") as vitima:  # o token foi para o navegador de outra pessoa
            x = await vitima.post("/api/resgatar", json={"token": token, "endereco": "atacante@coinos.io",
                                                         "prova": prova(cfg)})
            assert x.status_code == 400 and "resgatada" in x.json()["erro"]
        async with _cliente(main, "203.0.113.66") as atacante:  # o dono consegue
            x = await atacante.post("/api/resgatar", json={"token": token, "endereco": "ana@coinos.io",
                                                           "prova": prova(cfg)})
            assert x.status_code == 200
        await r.coinos.fechar()
    asyncio.run(ataque())


# ---------- R4-02: cota de sats por /48 ----------
def test_um_48_recebe_so_a_cota_de_uma_pessoa(tmp_path):
    async def ataque():
        falsa = CoinosFalsa()
        cfg = cfg_teste()
        r = Recompensas(cfg, DB(str(tmp_path / "48.db")), falsa.client())
        total = 0
        for i in range(30):  # 30 sub-redes /64 do mesmo /48, 30 contas
            cota = antiabuso.hash_cota(cfg.ip_salt, f"2001:db8:aaaa:{i:x}::1")
            try:
                total += (await r.resgatar(r.emitir_token(cota), f"conta{i}@coinos.io", cota, prova(cfg)))["sats"]
            except ResgateErro as e:
                assert "limite" in str(e)
        assert total == cfg.limite_dia_usuario  # 21, não o teto global
        await r.coinos.fechar()
    asyncio.run(ataque())
    assert antiabuso.hash_cota("s", "2001:db8:1:2::1") == antiabuso.hash_cota("s", "2001:db8:1:ffff::9")
    assert antiabuso.hash_cota("s", "2001:db8:1::1") != antiabuso.hash_cota("s", "2001:db8:2::1")
    assert antiabuso.hash_cota("s", "198.51.100.1") != antiabuso.hash_cota("s", "198.51.100.2")


# ---------- R4-03: pausa diária não é provocável pelo destinatário ----------
class _CarteiraRecusa:
    def __init__(self, status=400):
        self.recusados, self.pagos, self.status = set(), [], status

    async def saldo(self):
        return 100_000

    async def enviar_interno(self, usuario, sats):
        if usuario in self.recusados:
            raise CoinosErro("A Coinos recusou a operação.", ambiguo=True, status=self.status)
        self.pagos.append((usuario, sats))
        return {"ref": f"id-{len(self.pagos)}", "fee": 0}


def test_recusa_do_destinatario_bloqueia_so_o_endereco(tmp_path):
    async def ataque():
        carteira = _CarteiraRecusa()
        cfg = cfg_teste(max_erros_dia=5)
        r = Recompensas(cfg, DB(str(tmp_path / "destino.db")), carteira)
        carteira.recusados = {f"conta{i}" for i in range(5)}
        for i in range(5):
            with pytest.raises(ResgateErro, match="confirmar"):
                await r.resgatar(r.emitir_token(), f"conta{i}@coinos.io", f"ip-{i}", prova(cfg))
        assert not r.recompensas_pausadas()          # ninguém mais é prejudicado
        assert r.db.erros_dia(hoje()) == 0 and r.db.recusas_destino_dia(hoje()) == 5
        with pytest.raises(ResgateErro, match="recusou pagamentos para esse endereço"):
            await r.resgatar(r.emitir_token(), "conta0@coinos.io", "ip-novo", prova(cfg))
        await r.resgatar(r.emitir_token(), "inocente@coinos.io", "ip-inocente", prova(cfg))
        # o token gasto não volta (o débito poderia ter acontecido)
        assert r.db.um("SELECT COUNT(*) FROM tokens WHERE usado = 1")[0] == 6
    asyncio.run(ataque())


def test_recusas_de_destinatario_demais_ainda_pausam(tmp_path):
    async def ataque():
        carteira = _CarteiraRecusa()
        cfg = cfg_teste(max_erros_dia=1)
        r = Recompensas(cfg, DB(str(tmp_path / "muitas.db")), carteira)
        carteira.recusados = {f"c{i}" for i in range(3)}
        for i in range(3):
            with pytest.raises(ResgateErro):
                await r.resgatar(r.emitir_token(), f"c{i}@coinos.io", f"ip-{i}", prova(cfg))
        assert r.recompensas_pausadas()
    asyncio.run(ataque())


@pytest.mark.parametrize("status", [401, 403, 429, 500, None])
def test_erro_da_conta_ou_da_coinos_conta_para_a_pausa(tmp_path, status):
    async def ataque():
        carteira = _CarteiraRecusa(status)
        cfg = cfg_teste(max_erros_dia=2)
        r = Recompensas(cfg, DB(str(tmp_path / f"conta{status}.db")), carteira)
        carteira.recusados = {"a", "b"}
        for u in ("a", "b"):
            with pytest.raises(ResgateErro):
                await r.resgatar(r.emitir_token(), f"{u}@coinos.io", f"ip-{u}", prova(cfg))
        assert r.recompensas_pausadas()
    asyncio.run(ataque())


def test_endereco_do_pote_nao_recebe(tmp_path):
    async def ataque():
        carteira = _CarteiraRecusa()
        cfg = cfg_teste(doacao_endereco="Pote@Coinos.io")
        r = Recompensas(cfg, DB(str(tmp_path / "pote.db")), carteira)
        with pytest.raises(ResgateErro, match="próprio pote"):
            await r.resgatar(r.emitir_token(), "lightning:pote@coinos.io", "ip", prova(cfg))
        assert carteira.pagos == []
    asyncio.run(ataque())


def test_confirmacao_de_endereco_vale_pouco_tempo(monkeypatch, tmp_path):
    from servidor import recompensas as mod
    assert mod.VERIFICACAO_S <= 60
    chamadas = []

    async def verificar(endereco, transport=None):
        chamadas.append(endereco)
    monkeypatch.setattr(lnurl, "verificar_endereco", verificar)
    agora = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: agora[0])

    async def rodar():
        r = Recompensas(cfg_teste(), DB(":memory:"), _CarteiraRecusa())
        await r._confirmar_coinos("ana@coinos.io")
        await r._confirmar_coinos("ana@coinos.io")
        agora[0] += mod.VERIFICACAO_S + 1
        await r._confirmar_coinos("ana@coinos.io")
    asyncio.run(rodar())
    assert len(chamadas) == 2


# ---------- R4-04: limites globais não derrubam o site ----------
def test_info_sai_do_cache_e_nao_tem_limite_global(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "lim_info", antiabuso.Limitador(60, 60))
    monkeypatch.setattr(main, "_info_cache", (float("-inf"), None))
    assert not hasattr(main, "lim_info_global") and not hasattr(main, "lim_desafio_global")

    async def ataque():
        for i in range(25):
            async with _cliente(main, f"2001:db8:bbbb:{i:x}::1") as c:
                for _ in range(60):
                    await c.get("/api/info")
        async with _cliente(main, "203.0.113.200") as c:
            visitante = await c.get("/api/info")
        assert visitante.status_code == 200 and "max_export" in visitante.json()
    asyncio.run(ataque())


def test_limite_global_de_resgate_so_conta_pedidos_com_token_e_prova(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    falsa, r, cfg = _ligar_recompensas(monkeypatch, main, tmp_path)
    monkeypatch.setattr(main, "lim_resgate_global", antiabuso.Limitador(3, 600))

    async def ataque():
        for i in range(40):  # 40 /64 com token inventado
            async with _cliente(main, f"2001:db8:cccc:{i:x}::1") as c:
                x = await c.post("/api/resgatar", json={"token": "x" * 20, "endereco": "a@coinos.io",
                                                        "prova": prova(cfg)})
                assert x.status_code == 400
        assert len(main.lim_resgate_global.eventos) == 0
        cota = antiabuso.hash_cota(cfg.ip_salt, "198.51.100.30")
        async with _cliente(main, "198.51.100.30") as c:
            x = await c.post("/api/resgatar", json={"token": r.emitir_token(cota), "endereco": "ana@coinos.io",
                                                    "prova": prova(cfg)})
            assert x.status_code == 200
        await r.coinos.fechar()
    asyncio.run(ataque())


# ---------- R4-05: corpos lentos por pessoa ----------
def test_corpos_lentos_tem_limite_por_ip(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "PRAZO_JSON_S", 2)
    monkeypatch.setattr(main, "lim_preview", antiabuso.Limitador(1000, 60))
    monkeypatch.setattr(main, "render", Renderizador(1, 8))  # semáforo novo, deste laço de eventos
    liberar = asyncio.Event()

    async def lento():
        await liberar.wait()
        yield b"{}"

    async def ataque():
        async with _cliente(main, "203.0.113.70") as c:
            hdr = {"content-type": "application/json"}
            tarefas = [asyncio.create_task(c.post("/api/preview", content=lento(), headers=hdr))
                       for _ in range(main.MAX_CORPOS_POR_IP)]
            await asyncio.sleep(0.2)
            extra = await c.post("/api/preview", content=b"{}", headers=hdr)
            assert extra.status_code == 429
            async with _cliente(main, "198.51.100.71") as outro:  # outra pessoa não é afetada
                assert (await outro.post("/api/forma", content=b"{}", headers=hdr)).status_code == 400
            liberar.set()
            await asyncio.gather(*tarefas)
        assert main.corpos_em_curso == {}
    asyncio.run(ataque())
    assert main.PRAZO_JSON_S <= 5


def test_docker_aceita_mais_conexoes_e_compose_tem_rotacao_de_log():
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docker = open(os.path.join(raiz, "Dockerfile")).read()
    assert '"--limit-concurrency", "1000"' in docker
    compose = open(os.path.join(raiz, "docker-compose.yml")).read()
    assert "max-size" in compose and "max-file" in compose
    assert "request_buffers" in open(os.path.join(raiz, "README.md")).read()


# ---------- R4-06: cota de render por pessoa ----------
def test_um_ip_nao_ocupa_a_fila_de_render():
    async def rodar():
        r = Renderizador(1, 8)
        liberar = asyncio.Event()
        lim = [("ip|203.0.113.66", 2, 90)]

        def pesado():
            time.sleep(0.05)
            return "ok"

        async def esperar():
            await liberar.wait()
        t = [asyncio.create_task(r._na_fila(pesado, limites=lim)) for _ in range(2)]
        await asyncio.sleep(0.01)
        with pytest.raises(Excesso):
            await r._na_fila(pesado, limites=lim)                      # 3º ao mesmo tempo
        assert await r._na_fila(pesado, limites=[("ip|outro", 2, 90)]) == "ok"
        await asyncio.gather(*t)
        # orçamento de segundos: depois de gastar, espera a janela
        r.cota.gasto["ip|203.0.113.66"].append((time.monotonic(), 100))
        with pytest.raises(Excesso):
            await r._na_fila(pesado, limites=lim)
        assert r.na_fila == 0 and r.cota.em_curso == {}
    asyncio.run(rodar())


def test_rotas_pesadas_passam_a_cota_da_pessoa(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "render", Renderizador(1, 8))
    main.render.cota.gasto["ip|203.0.113.90"] = __import__("collections").deque([(time.monotonic(), 1000)])

    async def rodar():
        async with _cliente(main, "203.0.113.90") as c:
            x = await c.post("/api/preview", json={"seed": 1})
            assert x.status_code == 429
    asyncio.run(rodar())
    assert main.lim_preview.n <= 40


# ---------- R4-07 e R4-12: volume e retenção dos logs ----------
def test_eventos_ruidosos_tem_teto(monkeypatch, caplog):
    monkeypatch.setattr(eventos, "_balde", [10.0, time.monotonic()])
    monkeypatch.setattr(eventos, "_suprimidos", 0)
    with caplog.at_level(logging.INFO):
        for _ in range(1000):
            eventos.evento("pedido", ruidoso=True, rota="/")
        eventos.evento("importante", valor=1)  # eventos normais nunca são descartados
    linhas = [r.dados for r in caplog.records if getattr(r, "dados", None)]
    assert len([d for d in linhas if d["evento"] == "pedido"]) < 30
    assert linhas[-1]["evento"] == "importante"
    assert eventos._suprimidos > 900


def test_logs_velhos_sao_apagados(tmp_path):
    raiz = logging.getLogger()
    antes = list(raiz.handlers)
    try:
        eventos.configurar(str(tmp_path), retencao_dias=30)
        pasta = tmp_path / "logs"
        velho, novo = pasta / "eventos.jsonl.7", pasta / "eventos.jsonl.1"
        velho.write_text("x")
        novo.write_text("x")
        os.utime(velho, (time.time() - 40 * 86400,) * 2)
        eventos._aberto_em = "2000-01-01"   # virou o dia: rotaciona
        assert eventos.aplicar_retencao(30) == 1
        assert not velho.exists() and novo.exists()
        assert eventos._aberto_em == time.strftime("%Y-%m-%d", time.gmtime())
    finally:
        for h in list(raiz.handlers):
            raiz.removeHandler(h)
            h.close()
        for h in antes:
            raiz.addHandler(h)


# ---------- R4-08: LNURL comprimido ----------
def test_lnurl_comprimido_e_recusado_sem_descomprimir(monkeypatch):
    bomba = gzip.compress(b"\0" * (100 * 1024 * 1024), 9)

    async def resolver(host):
        return ipaddress.ip_address("93.184.216.34")
    monkeypatch.setattr(lnurl, "_resolver", resolver)
    pedidos = []

    class Fluxo(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(0, len(bomba), 65536):
                yield bomba[i:i + 65536]

    def handler(req):
        pedidos.append(req.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Fluxo())

    async def ataque():
        tracemalloc.start()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                with pytest.raises(lnurl.LnurlErro, match="comprimido"):
                    await lnurl._get_json(c, "https://carteira.example/.well-known/lnurlp/x")
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert asyncio.run(ataque()) < 5 * 1024 * 1024
    assert pedidos == ["identity"]


# ---------- R4-09: confirmação por lista branca ----------
@pytest.mark.parametrize("resposta", [{"id": "x", "status": "pending"}, {"id": "x", "status": "FAILED "},
                                      {"id": "x", "status": "rejected"}, {"id": "x", "confirmed": False}])
def test_status_sem_confirmacao_fica_ambiguo(tmp_path, resposta):
    def handler(req):
        if req.url.path.endswith("/me"):
            return httpx.Response(200, json={"balance": 100_000})
        return httpx.Response(200, json=resposta)

    async def rodar():
        client = Coinos("FAKE_ONLY", "http://fake.local", transport=httpx.MockTransport(handler))
        cfg = cfg_teste()
        db = DB(str(tmp_path / "status.db"))
        r = Recompensas(cfg, db, client)
        try:
            with pytest.raises(ResgateErro, match="confirmar"):
                await r.resgatar(r.emitir_token(), "ana@coinos.io", "ip", prova(cfg))
            assert [s for (s,) in db.con.execute("SELECT status FROM resgates")] == ["erro"]
        finally:
            await client.fechar()
    asyncio.run(rodar())


@pytest.mark.parametrize("resposta", [{"id": "e1d4"}, {"id": "e1d4", "status": "paid"},
                                      {"id": "e1d4", "confirmed": True}])
def test_confirmacoes_validas_continuam_pagas(resposta):
    async def rodar():
        client = Coinos("FAKE_ONLY", "http://fake.local",
                        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=resposta)))
        try:
            assert (await client.enviar_interno("ana", 10))["ref"] == "e1d4"
        finally:
            await client.fechar()
    asyncio.run(rodar())


# ---------- R4-10: admissão /48 ignora chaves vencidas ----------
def test_admissao_48_ignora_chaves_vencidas(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "lim_admissao", antiabuso.Limitador(64, 600))
    lim = antiabuso.Limitador(5, 60, max_chaves=100)
    for i in range(50):
        lim.permitir(f"198.51.100.{i}")
    for fila in lim.eventos.values():
        fila[0] -= 86400
    for i in range(64):
        main.limitar(lim, f"2001:db8:cccc:{i:x}::1")
    main.limitar(lim, "2001:db8:cccc:ff00::1")      # vizinho do mesmo /48 entra
    assert lim.ativas() == 65


# ---------- R4-11: doações com limite global ----------
def test_doar_tem_limite_global(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    falsa, r, cfg = _ligar_recompensas(monkeypatch, main, tmp_path)
    codigos = []

    async def ataque():
        for i in range(main.lim_doacao_global.n + 5):
            async with _cliente(main, f"2001:db8:dddd:{i:x}::1") as c:
                codigos.append((await c.post("/api/doar", json={"sats": 1})).status_code)
        await r.coinos.fechar()
    asyncio.run(ataque())
    assert codigos.count(200) == main.lim_doacao_global.n and codigos.count(429) == 5


# ---------- R4-14: 500 com cabeçalhos; TRUST_PROXY sempre com segredo ----------
def test_erro_500_tem_cabecalhos_de_seguranca(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)

    async def quebra(p, limites=()):
        raise RuntimeError("falha simulada")
    monkeypatch.setattr(main.render, "miniatura", quebra)

    async def rodar():
        t = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=t, base_url="http://s") as c:
            return await c.post("/api/forma", json={})
    r = asyncio.run(rodar())
    assert r.status_code == 500 and "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_trust_proxy_sem_segredo_nao_inicia_nem_sem_recompensas():
    with pytest.raises(RuntimeError, match="PROXY_SECRET"):
        Config(coinos_token="", trust_proxy=True, proxy_secret="").validar()
    Config(coinos_token="", trust_proxy=True, proxy_secret="s" * 32).validar()


def test_miniatura_nao_depende_do_cache_de_formas():
    import io
    from PIL import Image
    im = Image.new("RGB", (100, 100), "white")
    for x in range(30, 70):
        for y in range(30, 70):
            im.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    r = Renderizador()
    img_id = r.guardar_imagem(buf.getvalue())
    r.formas.maximo = 0                                  # cache que descarta tudo
    png = r._miniatura({"fonte": "imagem", "imagem_id": img_id})
    assert png.size == (220, 220)
