"""Testes de regressão da reauditoria de 21/10/2026 (achados R01–R07, A1–A10 e N01–N09).
Só APIs falsas; nada toca a rede."""
import asyncio
import importlib
import logging
import threading
import time

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from servidor import antiabuso, lnurl
from servidor.coinos import Coinos, CoinosErro
from servidor.config import Config
from servidor.db import DB
from servidor.recompensas import Recompensas, ResgateErro, hoje
from tests.conftest import verificar_endereco_real


def cfg_teste(**kw):
    opcoes = {"coinos_token": "FAKE_ONLY", "hmac_key": "audit", "pow_bits": 0, "intervalo_min_s": 0, **kw}
    return Config(**opcoes)


def prova(cfg):
    return {**antiabuso.criar_desafio(cfg.hmac_key, cfg.pow_bits), "nonce": "0"}


# ---------- R01: proxy só com segredo ----------
def test_xff_do_gateway_docker_exige_segredo_do_proxy():
    proxies = antiabuso.redes(Config(trust_proxy=True).proxies_confiaveis)
    # cliente direto pela porta publicada: chega pelo gateway, mas sem o segredo
    assert antiabuso.ip_cliente("172.17.0.1", "198.51.100.10", proxies, segredo_ok=False) == "172.17.0.1"
    assert antiabuso.ip_cliente("172.17.0.1", "198.51.100.11", proxies, segredo_ok=False) == "172.17.0.1"
    # o proxy de verdade manda o segredo
    assert antiabuso.ip_cliente("172.17.0.1", "198.51.100.10", proxies, segredo_ok=True) == "198.51.100.10"
    assert antiabuso.segredo_confere("a" * 64, "a" * 64)
    assert not antiabuso.segredo_confere("b" * 64, "a" * 64)
    assert not antiabuso.segredo_confere(None, "a" * 64)


def test_trust_proxy_com_recompensas_exige_proxy_secret(monkeypatch):
    monkeypatch.setenv("POW_HMAC_KEY", "a" * 64)
    monkeypatch.setenv("IP_SALT", "b" * 64)
    with pytest.raises(RuntimeError, match="PROXY_SECRET"):
        Config(coinos_token="FAKE_ONLY", trust_proxy=True, proxy_secret="").validar()
    Config(coinos_token="FAKE_ONLY", trust_proxy=True, proxy_secret="c" * 64).validar()


# ---------- R02: endereço inexistente não queima orçamento ----------
def _coinos_rejeita_send(status=400):
    app = FastAPI()
    enviados = []

    @app.get("/me")
    async def me():
        return {"balance": 100_000}

    @app.post("/send")
    async def send(req: Request):
        enviados.append((await req.json())["username"])
        return Response("usuário inexistente", status_code=status)

    return Coinos("FAKE_ONLY", "http://fake.local", transport=httpx.ASGITransport(app=app)), enviados


def test_usuario_coinos_inexistente_nao_chega_ao_send(tmp_path):
    async def ataque():
        client, enviados = _coinos_rejeita_send()
        cfg = cfg_teste(limite_dia_global=15)
        db = DB(str(tmp_path / "sem_debito.db"))
        r = Recompensas(cfg, db, client)
        try:
            token = r.emitir_token()
            with pytest.raises(lnurl.LnurlErro, match="não existe"):
                await r.resgatar(token, "+@coinos.io", "ip-a", prova(cfg))
            assert enviados == [] and db.soma_dia(hoje()) == 0
            assert db.token_valido(token, cfg.token_validade_s)  # o token nem foi gasto
        finally:
            await client.fechar()
    asyncio.run(ataque())


def test_erros_demais_no_dia_pausam_recompensas(tmp_path):
    async def ataque():
        client, enviados = _coinos_rejeita_send(500)  # erro da Coinos (não do destinatário): ambíguo
        cfg = cfg_teste(max_erros_dia=2)
        r = Recompensas(cfg, DB(str(tmp_path / "pausa.db")), client)
        try:
            for i in range(2):
                with pytest.raises(ResgateErro, match="confirmar"):
                    await r.resgatar(r.emitir_token(), f"ana{i}@coinos.io", f"ip-{i}", prova(cfg))
            assert r.recompensas_pausadas()
            with pytest.raises(ResgateErro, match="pausadas"):
                await r.resgatar(r.emitir_token(), "bob@coinos.io", "ip-9", prova(cfg))
            assert len(enviados) == 2
        finally:
            await client.fechar()
    asyncio.run(ataque())


# ---------- R03: uma consulta de saldo por vez ----------
def test_saldo_tem_consulta_unica_para_pedidos_simultaneos():
    class CoinosLenta:
        def __init__(self):
            self.consultas = 0

        async def saldo(self):
            self.consultas += 1
            await asyncio.sleep(0.03)
            return 100_000

    async def ataque():
        fake = CoinosLenta()
        r = Recompensas(cfg_teste(), DB(":memory:"), fake)
        valores = await asyncio.gather(*(r.saldo() for _ in range(50)))
        assert valores == [100_000] * 50
        assert fake.consultas == 1
    asyncio.run(ataque())


# ---------- R05: saldo lido logo antes da reserva ----------
def test_saldo_fresco_nao_deixa_cruzar_a_reserva(tmp_path):
    class Carteira:
        def __init__(self):
            self.balance = 520
            self.pagos = 0

        async def saldo(self):
            return self.balance

        async def pagar_bolt11(self, _bolt11):
            self.balance -= 10
            self.pagos += 1
            return {"ref": str(self.pagos), "fee": 0}

    async def ataque():
        carteira = Carteira()
        cfg = cfg_teste(recompensas_seq=[10], pagamento_externo=True, reserva_pote=500)
        liberacoes = [asyncio.Event() for _ in range(3)]
        todos = asyncio.Event()
        chegaram = 0

        async def invoice(endereco, _sats, **_kw):
            nonlocal chegaram
            i = int(endereco.split("@")[0][-1])
            chegaram += 1
            if chegaram == 3:
                todos.set()
            await liberacoes[i].wait()
            return "lnbc100n1fake"

        r = Recompensas(cfg, DB(str(tmp_path / "saldo.db")), carteira, invoice_para=invoice)
        tarefas = [asyncio.create_task(r.resgatar(r.emitir_token(), f"ana{i}@wallet.example",
                                                   f"ip-{i}", prova(cfg))) for i in range(3)]
        await asyncio.wait_for(todos.wait(), 1)
        for ev in liberacoes:
            ev.set()
        resultados = await asyncio.gather(*tarefas, return_exceptions=True)
        assert sum(isinstance(x, dict) for x in resultados) == 1
        assert all("pote" in str(x) for x in resultados if isinstance(x, ResgateErro))
        assert carteira.balance >= cfg.reserva_pote
    asyncio.run(ataque())


# ---------- R06 / A5: taxa externa ----------
def _carteira_com_taxa(taxa):
    class Carteira:
        def __init__(self):
            self.pedidos = 0

        async def saldo(self):
            return 100_000

        async def pagar_bolt11(self, _bolt11):
            self.pedidos += 1
            await asyncio.sleep(0.01)
            return {"ref": str(self.pedidos), "fee": taxa}
    return Carteira()


async def _invoice(_endereco, _sats, **_kw):
    return "lnbc100n1fake"


@pytest.mark.parametrize("taxa", [100, None])
def test_taxa_alta_ou_invalida_pausa_antes_do_proximo_pagamento(tmp_path, taxa):
    async def ataque():
        carteira = _carteira_com_taxa(taxa)
        cfg = cfg_teste(recompensas_seq=[10], pagamento_externo=True, taxa_max=2)
        r = Recompensas(cfg, DB(str(tmp_path / "taxas.db")), carteira, invoice_para=_invoice)
        respostas = await asyncio.gather(*(
            r.resgatar(r.emitir_token(), f"ana{i}@wallet.example", f"ip-{i}", prova(cfg))
            for i in range(3)), return_exceptions=True)
        assert carteira.pedidos == 1                      # só o primeiro chegou a pagar
        assert sum(isinstance(x, ResgateErro) for x in respostas) == 2
        assert not r.externos_liberados()
    asyncio.run(ataque())


# ---------- R04: uploads na fila ----------
def test_uploads_entram_na_fila_e_o_excesso_recebe_503(monkeypatch, tmp_path):
    async def ataque():
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("COINOS_TOKEN", "")
        main = importlib.import_module("servidor.main")
        ativos, pico = 0, 0
        trava = threading.Lock()
        liberar = threading.Event()

        def upload_lento(_dados):
            nonlocal ativos, pico
            with trava:
                ativos += 1
                pico = max(pico, ativos)
            liberar.wait(1)
            with trava:
                ativos -= 1
            return "id-falso"

        monkeypatch.setattr(main.render, "guardar_imagem", upload_lento)
        monkeypatch.setattr(main, "lim_upload", antiabuso.Limitador(1000, 600))
        transport = httpx.ASGITransport(app=main.app, client=("203.0.113.88", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as c:
            tarefas = [asyncio.create_task(c.post("/api/imagem", content=b"x", headers={"content-type": "image/png"}))
                       for _ in range(12)]
            await asyncio.sleep(0.3)
            liberar.set()
            respostas = await asyncio.gather(*tarefas)
        codigos = [x.status_code for x in respostas]
        assert pico <= main.cfg.render_workers           # nunca mais decodificações que workers
        assert codigos.count(503) >= 12 - main.MAX_UPLOADS_EM_CURSO
    asyncio.run(ataque())


# ---------- R07: retenção ----------
def test_limpeza_apaga_dados_antigos_e_preserva_os_recentes(tmp_path):
    db = DB(str(tmp_path / "retencao.db"))
    antigo, agora = time.time() - 400 * 86400, time.time()
    for status, criado in (("pago", antigo), ("falhou", antigo), ("pago", agora), ("erro", agora)):
        db.ex("INSERT INTO resgates(dia,endereco,ip_hash,sats,status,criado) VALUES (?,?,?,?,?,?)",
              "2020-01-01", "pessoa@coinos.io", "hash-ip", 10, status, criado)
    db.ex("INSERT INTO doacoes(hash,sats,pago,criado) VALUES (?,?,?,?)", "antiga", 1, 1, antigo)
    db.ex("INSERT INTO doacoes(hash,sats,pago,criado) VALUES (?,?,?,?)", "nova", 1, 1, agora)
    db.limpar(retencao_dias=30)
    assert sorted(r[0] for r in db.con.execute("SELECT status FROM resgates")) == ["erro", "pago"]
    assert [r[0] for r in db.con.execute("SELECT hash FROM doacoes")] == ["nova"]


# ---------- A2: limites de rota por /64 ----------
def test_limites_das_rotas_agrupam_ipv6_por_64(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    lim = antiabuso.Limitador(1, 600)
    main.limitar(lim, "2001:db8:1:2::1")
    with pytest.raises(main.Recusado):
        main.limitar(lim, "2001:db8:1:2:abcd::99")  # mesmo /64, outro endereço
    main.limitar(lim, "2001:db8:1:3::1")


# ---------- A4: corpo lento ----------
def test_corpo_lento_e_cortado_pelo_prazo(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    monkeypatch.setattr(main, "PRAZO_JSON_S", 0.2)

    async def pingando():
        for _ in range(50):
            yield b"x"
            await asyncio.sleep(0.05)

    async def ataque():
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as c:
            r = await c.post("/api/preview", content=pingando(), headers={"content-type": "application/json"})
        assert r.status_code == 408
    asyncio.run(ataque())


# ---------- A6: exportações não são gravadas no servidor ----------
def test_servidor_publico_nao_grava_exportacoes(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    destino = tmp_path / "exports"
    monkeypatch.setattr(main.cfg, "salvar_exports_em", str(destino))
    monkeypatch.setattr(main.cfg, "modo_local", False)

    async def chamar():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as c:
            return await c.post("/api/exportar", json={"fonte": "sat", "largura": 640})
    assert asyncio.run(chamar()).status_code == 200
    assert not destino.exists()


# ---------- A7: logs sem corpo externo ----------
def test_log_nao_registra_corpo_da_coinos(caplog):
    app = FastAPI()

    @app.post("/send")
    async def send():
        return Response("segredo-interno TOKEN=abc123 nsec1xyz", status_code=400)

    async def ataque():
        client = Coinos("FAKE_ONLY", "http://fake.local", transport=httpx.ASGITransport(app=app))
        try:
            with pytest.raises(CoinosErro):
                await client.enviar_interno("ana", 10)
        finally:
            await client.fechar()
    with caplog.at_level(logging.WARNING):
        asyncio.run(ataque())
    assert "HTTP 400" in caplog.text
    assert "segredo-interno" not in caplog.text and "nsec1" not in caplog.text


# ---------- verificação real de endereço (com transporte falso) ----------
def test_verificar_endereco_confere_lnurl(monkeypatch):
    import ipaddress

    async def publico(_host):
        return ipaddress.ip_address("203.0.113.10")
    monkeypatch.setattr(lnurl, "_resolver", publico)

    def handler(req):
        if req.url.path == "/.well-known/lnurlp/ana":
            return httpx.Response(200, json={"tag": "payRequest", "callback": "https://coinos.io/cb"})
        return httpx.Response(404, json={"status": "ERROR", "reason": "not found"})

    t = httpx.MockTransport(handler)
    asyncio.run(verificar_endereco_real("ana@coinos.io", transport=t))
    with pytest.raises(lnurl.LnurlErro):
        asyncio.run(verificar_endereco_real("ninguem@coinos.io", transport=t))


# ---------- revisão adicional: cancelamento e virada do dia ----------
class _CoinosLenta:
    def __init__(self, espera=0.3):
        self.espera, self.enviados = espera, []

    async def saldo(self):
        return 100_000

    async def enviar_interno(self, usuario, sats):
        await asyncio.sleep(self.espera)
        self.enviados.append((usuario, sats))
        return {"ref": "ok", "fee": 0}


def test_pedido_cancelado_no_meio_do_pagamento_ainda_fecha_o_resgate():
    async def ataque():
        cfg = cfg_teste()
        coinos = _CoinosLenta()
        r = Recompensas(cfg, DB(":memory:"), coinos)
        pedido = asyncio.create_task(r.resgatar(r.emitir_token(), "ana@coinos.io", "ip", prova(cfg)))
        await asyncio.sleep(0.1)  # já reservou e está esperando a Coinos
        pedido.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pedido
        await asyncio.wait_for(asyncio.gather(*r._tarefas), 2)  # o pagamento segue sozinho
        assert coinos.enviados == [("ana", 10)]
        assert [s for (s,) in r.db.con.execute("SELECT status FROM resgates")] == ["pago"]
    asyncio.run(ataque())


def test_pagamento_interrompido_pelo_desligamento_vira_erro():
    async def ataque():
        cfg = cfg_teste()
        r = Recompensas(cfg, DB(":memory:"), _CoinosLenta(espera=5))
        pedido = asyncio.create_task(r.resgatar(r.emitir_token(), "ana@coinos.io", "ip", prova(cfg)))
        await asyncio.sleep(0.1)
        for t in list(r._tarefas):  # como o asyncio faz ao encerrar o servidor
            t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pedido
        await asyncio.sleep(0)
        assert [s for (s,) in r.db.con.execute("SELECT status FROM resgates")] == ["erro"]
    asyncio.run(ataque())


def test_pendente_orfao_de_queda_vira_erro():
    db = DB(":memory:")
    db.ex("INSERT INTO resgates(dia,endereco,ip_hash,sats,status,criado) VALUES ('2020-01-01','a@coinos.io','h',10,'pendente',?)",
          time.time() - 3600)
    db.ex("INSERT INTO resgates(dia,endereco,ip_hash,sats,status,criado) VALUES ('2020-01-01','b@coinos.io','h',10,'pendente',?)",
          time.time())
    assert db.fechar_pendentes_orfaos() == 1
    assert sorted(s for (s,) in db.con.execute("SELECT status FROM resgates")) == ["erro", "pendente"]


def test_reserva_do_pote_conta_taxa_dos_pendentes_de_outros_dias():
    from servidor.db import Recusa
    db = DB(":memory:")
    for i in range(50):
        db.ex("INSERT INTO resgates(dia,endereco,ip_hash,sats,status,criado) VALUES ('2020-01-01',?,?,10,'pendente',?)",
              f"a{i}@coinos.io", f"h{i}", time.time())
    db.criar_token("t")
    reserva, taxa, sats = 500, 2, 10
    precisa = reserva + 50 * sats + sats + 51 * taxa
    args = dict(token="t", validade_s=999, dia="2099-01-01", endereco="b@coinos.io", ip_hash="x", seq=[sats],
                limite_global=10_000, taxa_max=taxa, intervalo_min_s=0, saldo_em=time.time(), reserva_pote=reserva)
    with pytest.raises(Recusa, match="pote"):
        db.reservar_resgate(saldo=precisa - 1, **args)
    assert db.reservar_resgate(saldo=precisa, **args)[1] == sats


def test_cancelar_pedidos_nao_libera_vaga_com_thread_ainda_rodando():
    from servidor.render import Ocupado, Renderizador

    async def ataque():
        r = Renderizador(workers=1, max_fila=1)   # 2 vagas no total
        liberar = threading.Event()
        rodando = []

        def pesado():
            rodando.append(1)
            liberar.wait(2)

        a = asyncio.create_task(r._na_fila(pesado))
        b = asyncio.create_task(r._na_fila(pesado))
        await asyncio.sleep(0.1)
        a.cancel()                 # 'a' já está na thread: a vaga NÃO pode voltar
        b.cancel()                 # 'b' ainda esperava a vez: desiste sem rodar
        for t in (a, b):
            with pytest.raises(asyncio.CancelledError):
                await t
        await asyncio.sleep(0.05)
        assert r.na_fila == 1      # só a vaga da thread que segue rodando
        with pytest.raises(Ocupado):
            r.max_fila = 1
            await r._na_fila(pesado)
        liberar.set()
        await asyncio.wait_for(asyncio.gather(*r._tarefas, return_exceptions=True), 3)
        assert r.na_fila == 0 and rodando == [1]
    asyncio.run(ataque())


# ---------- segunda auditoria: N04–N09 ----------
def _bolt11_cru(hrp, campos, ts=None):
    """Monta uma invoice com checksum válido a partir de campos já em palavras de 5 bits."""
    from tests.conftest import CHARSET, _palavras, _polymod
    ts = int(time.time()) if ts is None else ts
    dados = _palavras(ts, 7)[-7:] + campos + [0] * 104
    vals = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + dados + [0] * 6
    pm = _polymod(vals) ^ 1
    dados += [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in dados)


@pytest.mark.parametrize("hrp, campos", [
    ("lnbc100n", [3, 31, 31, 1, 2]),                     # campo 'r' declara 1023 palavras, tem 2
    ("lnbc100n", [1, 1, 20] + [0] * 52 + [7]),            # sobra de 1 palavra no fim
    ("lnbc100n", [3, 0, 10] + [1] * 10),                  # rota com tamanho que não é múltiplo de 51 bytes
    ("lnbc100n", [6, 0, 2, 1, 1, 6, 0, 2, 1, 1]),         # expiração repetida
    ("lnbc15p", [1, 1, 20] + [0] * 52),                   # picossats que não fecham msat inteiro
    ("lnbc0100n", [1, 1, 20] + [0] * 52),                 # valor com zero à esquerda
    ("lnbcn", [1, 1, 20] + [0] * 52),                     # multiplicador sem valor
])
def test_bolt11_estruturalmente_invalida_e_recusada(hrp, campos):
    with pytest.raises(lnurl.LnurlErro):
        lnurl.decodificar_bolt11(_bolt11_cru(hrp, campos))


def test_bolt11_picossats_inteiros_ainda_valem():
    assert lnurl.decodificar_bolt11(_bolt11_cru("lnbc100000p", [1, 1, 20] + [0] * 52))["msat"] == 10_000


def test_erro_em_pagamento_externo_pausa_so_os_externos(tmp_path):
    class Carteira:
        def __init__(self):
            self.internos = []

        async def saldo(self):
            return 100_000

        async def pagar_bolt11(self, _b):
            raise CoinosErro("recusou", ambiguo=True)

        async def enviar_interno(self, u, s):
            self.internos.append(u)
            return {"ref": "ok", "fee": 0}

    async def ataque():
        carteira = Carteira()
        cfg = cfg_teste(pagamento_externo=True, max_erros_dia=2)
        r = Recompensas(cfg, DB(str(tmp_path / "n05.db")), carteira, invoice_para=_invoice)
        with pytest.raises(ResgateErro, match="confirmar"):
            await r.resgatar(r.emitir_token(), "x0@evil.example", "ip-0", prova(cfg))
        assert not r.externos_liberados()          # externos pausados na hora
        assert not r.recompensas_pausadas()        # mas quem usa Coinos segue recebendo
        assert (await r.resgatar(r.emitir_token(), "bob@coinos.io", "ip-9", prova(cfg)))["sats"] == 10
        assert carteira.internos == ["bob"]
    asyncio.run(ataque())


def test_status_de_uma_doacao_tem_consulta_unica_simultanea(tmp_path):
    class CoinosLenta:
        consultas = 0

        async def invoice_paga(self, _h):
            CoinosLenta.consultas += 1
            await asyncio.sleep(0.05)
            return False

    async def ataque():
        db = DB(str(tmp_path / "n06.db"))
        db.nova_doacao("abc", 21)
        r = Recompensas(cfg_teste(), db, CoinosLenta())
        assert await asyncio.gather(*(r.doacao_paga("abc") for _ in range(50))) == [False] * 50
        assert CoinosLenta.consultas == 1
    asyncio.run(ataque())


def test_um_bloco_ipv6_nao_enche_o_limitador(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    monkeypatch.setattr(main, "lim_admissao", antiabuso.Limitador(64, 600))
    lim = antiabuso.Limitador(5, 600, max_chaves=1000)
    recusados = 0
    for i in range(2000):  # 2000 sub-redes /64 do mesmo /48
        try:
            main.limitar(lim, f"2001:db8:1:{i:x}::1")
        except main.Recusado:
            recusados += 1
    # o bloco ocupa no máximo metade da tabela + 64 chaves; nunca a enche (R3-01 mudou a regra)
    assert len(lim.eventos) == lim.max_chaves // 2 + 64 < lim.max_chaves
    assert recusados == 2000 - len(lim.eventos)
    main.limitar(lim, "203.0.113.50")          # um visitante novo continua entrando
    main.limitar(lim, "2001:db8:2::1")         # inclusive de outro bloco IPv6


def test_liberar_resgate_ja_pago_nao_devolve_token():
    db = DB(":memory:")
    db.criar_token("t")
    rid, _ = db.reservar_resgate(token="t", validade_s=999, dia="2099-01-01", endereco="a@coinos.io",
                                 ip_hash="h", seq=[10], limite_global=10_000, taxa_max=2, intervalo_min_s=0,
                                 saldo=100_000, saldo_em=time.time(), reserva_pote=0)
    db.fechar_resgate(rid, "pago", ref="r")
    db.liberar_resgate(rid, "t", "liberação tardia")
    assert not db.token_valido("t", 999)
    assert db.um("SELECT status FROM resgates WHERE id = ?", rid)[0] == "pago"


def test_desafio_e_meu_dia_tem_limite(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    for nome in ("lim_desafio", "lim_meu_dia"):
        lim = getattr(main, nome)
        for _ in range(lim.n):
            main.limitar(lim, "198.51.100.200")
        with pytest.raises(main.Recusado):
            main.limitar(lim, "198.51.100.200")
