"""Servidor web (FastAPI): gerador de imagens + recompensas e doações via Coinos.

Rodar: uvicorn servidor.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
(um único processo: os limitadores de taxa ficam em memória)
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from urllib.parse import urlsplit

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import admin, alertas, antiabuso, eventos
from .eventos import evento
from .coinos import Coinos, CoinosErro
from .config import Config
from .db import DB
from .i18n import idioma_de, traduzir
from .lnurl import LnurlErro
from .recompensas import Recompensas, ResgateErro
from .render import Excesso, ImagemInvalida, Ocupado, ParametroInvalido, Renderizador, dimensoes
from .render import FORMATOS, AnimacaoRender, formato_de, lado_para
from motor.animacao import FalhaAnimacao, achar_ffmpeg
from .telegram import Telegram, TelegramErro

log = logging.getLogger("servidor")

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(RAIZ, "static")
MAX_JSON = 16 * 1024
PRAZO_CORPO_S = 20      # tempo máximo para receber o corpo de um upload de imagem
PRAZO_JSON_S = 5        # ... e de um pedido JSON (até 16 KB)
MAX_UPLOADS_EM_CURSO = 4
# corpos sendo recebidos ao mesmo tempo por pessoa: corpos lentos não ocupam o servidor todo
MAX_CORPOS_POR_IP, MAX_CORPOS_POR_48 = 6, 16
# trabalho pesado por pessoa: trabalhos na fila ao mesmo tempo e segundos de render a cada 10 min
RENDER_SIMULTANEOS, RENDER_S_POR_PESSOA = 2, 90
INFO_CACHE_S = 10

cfg = Config()
eventos.configurar(cfg.data_dir, retencao_dias=cfg.retencao_dias)
try:
    cfg.validar()
except RuntimeError as e:
    evento("configuracao_invalida", "error", motivo=str(e))
    raise
PROXIES = antiabuso.redes(cfg.proxies_confiaveis) if cfg.trust_proxy else []
render = Renderizador(cfg.render_workers, cfg.max_fila)
db = DB(os.path.join(cfg.data_dir, "app.db"))
coinos = Coinos(cfg.coinos_token, cfg.coinos_url) if cfg.recompensas_ativas else None
recompensas = Recompensas(cfg, db, coinos) if coinos else None
telegram = Telegram(cfg.telegram_token, cfg.telegram_chat_id) if cfg.reports_ativos else None
if recompensas and telegram:
    recompensas.ao_saldo = alertas.AlertaPote(cfg, db, telegram).verificar
lim_export = antiabuso.Limitador(cfg.exports_por_10min, 600)
lim_preview = antiabuso.Limitador(40, 60)
# prévia rápida (1/3 da largura) que a página pede antes da nítida: limite próprio, igual ao da
# nítida; as duas ainda passam pela cota de segundos de render da pessoa (CotaRender)
lim_preview_rapida = antiabuso.Limitador(40, 60)
PREVIA_RAPIDA_DIV = 3
# animação: ~5 s de CPU cada; poucas por pessoa (além da cota de render)
ANIMACOES_POR_10MIN = 4
lim_animacao = antiabuso.Limitador(ANIMACOES_POR_10MIN, 600)
FFMPEG = achar_ffmpeg()
lim_upload = antiabuso.Limitador(30, 600)
lim_resgate = antiabuso.Limitador(10, 600)
lim_doacao = antiabuso.Limitador(5, 600)
lim_doacao_global = antiabuso.Limitador(60, 600)      # cada doação cria uma invoice na Coinos
lim_status = antiabuso.Limitador(40, 60)
lim_status_global = antiabuso.Limitador(1200, 60)     # o navegador do doador consulta a cada 5 s
lim_info = antiabuso.Limitador(60, 60)                # /api/info sai de um cache: sem limite global
lim_desafio = antiabuso.Limitador(30, 60)             # desafio é só um HMAC: sem limite global
lim_meu_dia = antiabuso.Limitador(60, 60)
lim_report = antiabuso.Limitador(3, 600)
# cada relato vira uma mensagem no Telegram do dono: o teto global segura quem troca de IP
lim_report_global = antiabuso.Limitador(cfg.reports_por_hora, 3600)
# Só conta resgates que já passaram por token válido (do próprio IP) e prova de trabalho:
# bloquear todo mundo exigiria centenas de exportações legítimas.
lim_resgate_global = antiabuso.Limitador(300, 600)
# Com o limitador já meio cheio, um bloco IPv6 /48 só pode abrir 64 chaves /64 novas
# a cada 10 min (sem isso, um único /48 enche a tabela e nega acesso a IPs novos).
# Com folga, não há admissão: vizinhos do mesmo /48 não se atrapalham.
lim_admissao = antiabuso.Limitador(64, 600)
uploads_em_curso = 0
corpos_em_curso = {}  # chave (/64 ou IPv4, e /48) -> corpos sendo recebidos agora
_info_cache = (float("-inf"), None)

SEGURANCA = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' blob: data:; worker-src blob:; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


async def limpeza_periodica():
    while True:
        try:
            apagados = db.limpar(cfg.token_validade_s, antiabuso.VALIDADE_S, cfg.retencao_dias)
            apagados["logs"] = eventos.aplicar_retencao(cfg.retencao_dias)
            if any(apagados.values()):
                evento("limpeza", **apagados)
            orfaos = db.fechar_pendentes_orfaos()
            if orfaos:
                evento("pendentes_orfaos", "error", quantidade=orfaos,
                       acao="viraram 'erro': conferir na Coinos")
        except Exception:
            log.exception("falha na limpeza periódica")
        await asyncio.sleep(600)


@asynccontextmanager
async def ciclo(_app):
    evento("servidor_iniciado", recompensas=bool(recompensas), pagamento_externo=cfg.pagamento_externo,
           recompensas_seq=cfg.recompensas_seq, limite_dia_global=cfg.limite_dia_global,
           reserva_pote=cfg.reserva_pote, trust_proxy=cfg.trust_proxy, max_export=cfg.max_export,
           render_workers=cfg.render_workers, modo_local=cfg.modo_local)
    tarefa = asyncio.create_task(limpeza_periodica())
    yield
    tarefa.cancel()
    if coinos:
        await coinos.fechar()
    if telegram:
        await telegram.fechar()
    evento("servidor_encerrado")


app = FastAPI(title="Rede de Nós", lifespan=ciclo, docs_url=None, redoc_url=None, openapi_url=None)


def _origem_permitida(req: Request):
    """POST só da própria página: um site de terceiros não pode usar o navegador (e o IP) dos
    visitantes dele para resgatar, doar ou exportar aqui (CSRF)."""
    site = req.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origem = req.headers.get("origin")
    if origem is None:
        return True
    permitidos = {req.headers.get("host", "").lower()}
    if cfg.public_url:
        permitidos.add(urlsplit(cfg.public_url).netloc.lower())
    return origem != "null" and urlsplit(origem).netloc.lower() in permitidos


def _idioma(req: Request):
    """Idioma das mensagens de erro: a página manda X-Idioma (pt, en ou de)."""
    return idioma_de(req.headers.get("x-idioma"))


@app.middleware("http")
async def mesma_origem(request: Request, call_next):
    if request.method == "POST":
        if not _origem_permitida(request):
            evento("pedido_outra_origem", "warning", rota=request.url.path[:200], ruidoso=True,
                   origem=(request.headers.get("origin") or "")[:100])
            return JSONResponse({"erro": traduzir("Pedido de outra origem.", _idioma(request))}, status_code=403)
        # Content-Type que um formulário ou fetch "simples" de outro site não consegue mandar
        # sem preflight (que este servidor não autoriza)
        tipo = request.headers.get("content-type", "").split(";")[0].strip().lower()
        esperado = "image/" if request.url.path == "/api/imagem" else "application/json"
        if not (tipo.startswith(esperado) if esperado.endswith("/") else tipo == esperado):
            return JSONResponse({"erro": traduzir("Pedido inválido.", _idioma(request))}, status_code=415)
    return await call_next(request)


@app.middleware("http")
async def cabecalhos(request: Request, call_next):
    t0 = time.perf_counter()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        for k, v in SEGURANCA.items():
            resp.headers.setdefault(k, v)
        return resp
    finally:  # todo pedido vira um evento (sem IP cru e sem query string), com teto de linhas/s
        evento("pedido", "warning" if status >= 500 else "info", ruidoso=status < 500, metodo=request.method,
               rota=request.url.path[:200], status=status, ms=round((time.perf_counter() - t0) * 1000, 1),
               ip=antiabuso.hash_ip(cfg.ip_salt, ip_de(request))[:12],
               agente=(request.headers.get("user-agent") or "")[:60])


class Recusado(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


@app.exception_handler(Recusado)
async def _recusado(req, e: Recusado):
    evento("pedido_recusado", "warning" if e.status == 429 else "info", ruidoso=True, rota=req.url.path[:200],
           status=e.status, motivo=e.msg, ip=antiabuso.hash_ip(cfg.ip_salt, ip_de(req))[:12])
    return JSONResponse({"erro": traduzir(e.msg, _idioma(req))}, status_code=e.status)


@app.exception_handler(Exception)
async def _inesperado(req, e: Exception):
    log.exception("erro inesperado em %s", req.url.path[:200])
    evento("erro_interno", "error", rota=req.url.path[:200], tipo=type(e).__name__)
    # esta resposta é montada fora do middleware `cabecalhos`: põe os cabeçalhos aqui
    return JSONResponse({"erro": traduzir("Erro interno. Tente de novo.", _idioma(req))}, status_code=500, headers=SEGURANCA)


AVISO_PROXY_S = 600
_avisos_proxy = {}  # tipo de aviso -> último instante (cada um sai no máximo a cada 10 min)


def _avisar_proxy(tipo, **campos):
    """Proxy mal configurado faz TODOS os visitantes parecerem um só IP (a cota de sats e os
    limites viram do site inteiro). Isso não quebra nada visível: só o log pode avisar."""
    agora = time.monotonic()
    if agora - _avisos_proxy.get(tipo, float("-inf")) >= AVISO_PROXY_S:
        _avisos_proxy[tipo] = agora
        evento(tipo, "error", **campos)


def ip_de(req: Request):
    xff = ",".join(req.headers.getlist("x-forwarded-for"))
    enviado = req.headers.get("x-proxy-secret")
    segredo_ok = antiabuso.segredo_confere(enviado, cfg.proxy_secret)
    conexao = req.client.host if req.client else ""
    if enviado is not None and not segredo_ok and not getattr(req.state, "avisou_segredo", False):
        req.state.avisou_segredo = True  # alguém tentou se passar pelo proxy (ou o segredo do proxy está errado)
        evento("segredo_proxy_invalido", "warning", rota=req.url.path[:200])
    if cfg.trust_proxy and xff:
        if enviado is None and antiabuso.na_lista(conexao, PROXIES):
            _avisar_proxy("proxy_sem_segredo", acao="o proxy não manda X-Proxy-Secret: todos os visitantes "
                          "contam como um só IP (veja 'Cloudflare Tunnel' ou o Caddyfile no README)")
        elif enviado is not None and segredo_ok and not antiabuso.na_lista(conexao, PROXIES):
            _avisar_proxy("proxy_fora_da_lista", conexao=conexao,
                          acao="o segredo está certo, mas o proxy conecta de fora de TRUSTED_PROXIES: "
                          "acrescente a rede dele em TRUSTED_PROXIES no .env")
    return antiabuso.ip_cliente(conexao, xff, PROXIES, segredo_ok)


def limitar(lim, chave, msg="Muitos pedidos seguidos. Espere um pouquinho."):
    """`chave` é um IP (agrupado por /64 no IPv6) ou um nome fixo para limites globais."""
    k = antiabuso.chave_ip(chave)
    bloco = antiabuso.prefixo_admissao(chave)
    # "meio cheio" conta só chaves ATIVAS: as vencidas ficam na tabela até ela encher
    if (bloco and k not in lim.eventos and lim.ativas() >= lim.max_chaves // 2
            and not lim_admissao.permitir(f"{id(lim)}|{bloco}")):
        raise Recusado(429, msg)
    if not lim.permitir(k):
        raise Recusado(429, msg)


@contextmanager
def vaga_de_corpo(ip):
    """No máximo MAX_CORPOS_POR_IP corpos sendo recebidos ao mesmo tempo por IPv4 ou /64, e
    MAX_CORPOS_POR_48 por bloco /48: poucas origens enviando devagar não ocupam todas as conexões."""
    chaves = {("ip", antiabuso.chave_ip(ip)): MAX_CORPOS_POR_IP}
    bloco = antiabuso.prefixo_admissao(ip)
    if bloco:
        chaves[("48", bloco)] = MAX_CORPOS_POR_48
    if any(corpos_em_curso.get(k, 0) >= maximo for k, maximo in chaves.items()):
        raise Recusado(429, "Muitos envios ao mesmo tempo. Espere um pouquinho.")
    for k in chaves:
        corpos_em_curso[k] = corpos_em_curso.get(k, 0) + 1
    try:
        yield
    finally:
        for k in chaves:
            n = corpos_em_curso.pop(k, 1) - 1
            if n > 0:
                corpos_em_curso[k] = n


async def ler_corpo(req: Request, limite, prazo=None):
    """Lê o corpo sem nunca guardar mais que `limite` bytes nem esperar mais que `prazo`
    (padrão PRAZO_JSON_S)."""
    prazo = PRAZO_JSON_S if prazo is None else prazo
    cl = req.headers.get("content-length")
    if cl is not None and (not cl.isdigit() or int(cl) > limite):
        raise Recusado(413, f"Envio grande demais (máximo {limite // 1024} KB).")

    async def ler():
        buf = bytearray()
        async for parte in req.stream():
            buf += parte
            if len(buf) > limite:
                raise Recusado(413, f"Envio grande demais (máximo {limite // 1024} KB).")
        return bytes(buf)
    with vaga_de_corpo(ip_de(req)):
        try:
            return await asyncio.wait_for(ler(), prazo)
        except asyncio.TimeoutError as e:
            raise Recusado(408, "O envio demorou demais.") from e


def _sem_nan(c):
    raise ValueError(f"valor {c} não permitido")


async def ler_json(req: Request):
    try:
        p = json.loads(await ler_corpo(req, MAX_JSON), parse_constant=_sem_nan)
    except (ValueError, UnicodeDecodeError) as e:
        raise Recusado(400, "Pedido inválido.") from e
    if not isinstance(p, dict):
        raise Recusado(400, "Pedido inválido.")
    return p


def qr_svg(texto):
    img = qrcode.make(texto, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]  # sem o cabeçalho XML, para embutir no HTML


def exigir_recompensas():
    if not recompensas:
        raise HTTPException(404, "Recompensas desligadas neste servidor.")


def limites_render(ip):
    """Cota de render da pessoa (IPv4 ou /64) e do bloco /48 dela (veja render.CotaRender)."""
    lim = [("ip|" + antiabuso.chave_ip(ip), RENDER_SIMULTANEOS, RENDER_S_POR_PESSOA)]
    bloco = antiabuso.prefixo_admissao(ip)
    if bloco:
        lim.append(("48|" + bloco, 2 * RENDER_SIMULTANEOS, 2 * RENDER_S_POR_PESSOA))
    return lim


EXCESSO = "Você já tem desenhos em andamento ou pediu muitos em pouco tempo. Espere um pouco."


async def gerar_ou_recusar(p, largura, fmt, ip):
    try:
        return await render.gerar(p, largura, fmt, limites=limites_render(ip))
    except (ParametroInvalido, ImagemInvalida) as e:
        raise Recusado(400, str(e)) from e
    except Excesso as e:
        raise Recusado(429, EXCESSO) from e
    except Ocupado as e:
        raise Recusado(503, "Muita gente criando agora. Tente de novo em alguns segundos.") from e


# ---------- páginas ----------
def pagina_com_versao():
    """index.html pedindo o app.js e o i18n.js com a versão no endereço (/app.js?v=<hash>).

    Proxies como o Cloudflare podem trocar o Cache-Control do app.js por horas de cache
    no navegador; sem a versão, quem voltasse ao site depois de uma atualização ficaria
    com o JavaScript antigo e a página nova, e o site travaria em "Carregando…"."""
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    for arquivo in ("i18n.js", "app.js", "galeria.js"):
        html = html.replace(f'src="/{arquivo}"', f'src="/{arquivo}?v={versao_app(arquivo)}"')
    return html


def versao_app(arquivo="app.js"):
    with open(os.path.join(STATIC, arquivo), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


@app.get("/")
async def index():
    return HTMLResponse(pagina_com_versao(), headers={"Cache-Control": "no-store"})


@app.get("/app.js")
async def app_js():
    return FileResponse(os.path.join(STATIC, "app.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/i18n.js")
async def i18n_js():
    return FileResponse(os.path.join(STATIC, "i18n.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/info")
async def info(req: Request):
    """Mesma resposta para todos: sai de um cache curto (sem limite global, que qualquer um
    esgotaria para derrubar a página dos outros)."""
    global _info_cache
    limitar(lim_info, ip_de(req))
    if time.monotonic() - _info_cache[0] < INFO_CACHE_S:
        return _info_cache[1]
    d = {
        "recompensas": bool(recompensas),
        "recompensas_seq": cfg.recompensas_seq,
        "limite_dia": cfg.limite_dia_usuario,
        "pagamento_externo": bool(recompensas and recompensas.externos_liberados()),
        "max_export": cfg.max_export,
        "public_url": cfg.public_url,
        "repo_url": cfg.repo_url if cfg.repo_url.startswith("https://") else "",
        "bounty_email": cfg.bounty_email,
        "bounty_percentual": cfg.bounty_percentual,
        "doacao_endereco": cfg.doacao_endereco,
        "doacao_qr": qr_svg(f"lightning:{cfg.doacao_endereco}") if cfg.doacao_endereco else None,
        "pote": None,
        "report_tipos": list(TIPOS_REPORT.items()) if telegram else [],
        "animacao": AnimacaoRender(render, FFMPEG).tipos(),
    }
    if recompensas:
        with suppress(CoinosErro):
            d["pote"] = await recompensas.saldo()
    _info_cache = (time.monotonic(), d)
    return d


# ---------- geração ----------
@app.post("/api/imagem")
async def imagem(req: Request):
    global uploads_em_curso
    ip = ip_de(req)
    limitar(lim_upload, ip, "Muitas imagens enviadas. Espere alguns minutos.")
    if uploads_em_curso >= MAX_UPLOADS_EM_CURSO:  # vaga reservada ANTES de receber o corpo
        raise Recusado(503, "Muita gente enviando imagens agora. Tente de novo em alguns segundos.")
    uploads_em_curso += 1
    try:
        dados = await ler_corpo(req, cfg.max_upload_mb * 1024 * 1024, PRAZO_CORPO_S)
        img_id, cores = await render.guardar(dados, limites=limites_render(ip))
        evento("imagem_recebida", bytes=len(dados), id=img_id, cores=len(cores))
    except ImagemInvalida as e:
        raise Recusado(400, str(e)) from e
    except Excesso as e:
        raise Recusado(429, EXCESSO) from e
    except Ocupado as e:
        raise Recusado(503, "Servidor ocupado. Tente de novo em alguns segundos.") from e
    finally:
        uploads_em_curso -= 1
    # fundo_irregular: degradê/vinheta detectado, a página já liga "Remover fundo"
    return {"id": img_id, "cores": cores, "fundo_irregular": render.fundo_irregular(img_id)}


@app.post("/api/forma")
async def forma(req: Request):
    ip = ip_de(req)
    limitar(lim_preview, ip, "Muitas prévias seguidas. Espere um pouquinho.")
    p = await ler_json(req)
    try:
        png = await render.miniatura(p, limites=limites_render(ip))
    except (ParametroInvalido, ImagemInvalida) as e:
        raise Recusado(400, str(e)) from e
    except Excesso as e:
        raise Recusado(429, EXCESSO) from e
    except Ocupado as e:
        raise Recusado(503, "Servidor ocupado. Tente de novo em alguns segundos.") from e
    # cores desta análise (mudam com "Remover fundo" e os pontos de fundo): a página atualiza os chips
    cores = render.cores_de(p)
    extra = {"X-Cores": json.dumps(cores, separators=(",", ":"))} if cores is not None else {}
    return Response(png, media_type="image/png", headers=extra)


@app.post("/api/preview")
async def preview(req: Request):
    ip = ip_de(req)
    rapida = req.query_params.get("rapida") == "1"
    limitar(lim_preview_rapida if rapida else lim_preview, ip, "Muitas prévias seguidas. Espere um pouquinho.")
    p = await ler_json(req)
    try:
        lado = lado_para(formato_de(p), cfg.preview_w)
    except ParametroInvalido as e:
        raise Recusado(400, str(e)) from e
    if rapida:
        lado = max(160, lado // PREVIA_RAPIDA_DIV)
    jpg = await sem_esperar_quem_desistiu(req, gerar_ou_recusar(p, lado, "JPEG", ip))
    if jpg is None:
        return Response(status_code=204)
    return Response(jpg[0], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def sem_esperar_quem_desistiu(req: Request, coro):
    """Roda `coro`; se a página desistir do pedido (AbortController: um ajuste novo deixou a
    prévia velha) antes de o desenho começar, ele sai da fila sem gastar CPU. Desenho já
    começado termina (thread não se interrompe), mas ninguém espera por ele. None = desistiu."""
    tarefa = asyncio.ensure_future(coro)

    async def vigiar():
        while True:
            msg = await req.receive()
            if msg["type"] == "http.disconnect":
                return

    vigia = asyncio.ensure_future(vigiar())
    try:
        await asyncio.wait({tarefa, vigia}, return_when=asyncio.FIRST_COMPLETED)
        if tarefa.done():
            return tarefa.result()
        tarefa.cancel()
        with suppress(asyncio.CancelledError, Recusado):
            await tarefa
        evento("previa_desistida", ruidoso=True)
        return None
    finally:
        vigia.cancel()
        if not tarefa.done():
            tarefa.cancel()


@app.post("/api/exportar")
async def exportar(req: Request):
    ip = ip_de(req)
    limitar(lim_export, ip, "Muitas exportações em pouco tempo. Espere alguns minutos.")
    p = await ler_json(req)
    largura = p.get("largura", cfg.max_export)
    if isinstance(largura, bool) or not isinstance(largura, int):
        raise Recusado(400, "Largura inválida.")
    try:
        formato = formato_de(p)
    except ParametroInvalido as e:
        raise Recusado(400, str(e)) from e
    largura = max(640, min(largura, lado_para(formato, cfg.max_export)))
    largura -= largura % (16 if formato in ("paisagem", "retrato", "capa_yt") else 4)
    png, c = await gerar_ou_recusar(p, largura, "PNG", ip)
    base = p.get("nome") if isinstance(p.get("nome"), str) and p.get("nome") else str(p.get("fonte") or "rede")
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-")[:40] or "rede"
    w, h = dimensoes(largura, formato=c.formato)  # `largura` é o lado maior (a altura, no retrato)
    nome = f"{base}{'' if c.circulo else '_sem_circulo'}{sufixo_formato(c.formato)}_s{c.seed}_{w}x{h}.png"
    if cfg.modo_local and cfg.salvar_exports_em:  # só no app local (app.py), nunca no servidor
        os.makedirs(cfg.salvar_exports_em, exist_ok=True)
        with open(os.path.join(cfg.salvar_exports_em, nome), "wb") as f:
            f.write(png)
    headers = {"Content-Disposition": f'attachment; filename="{nome}"', "X-Nome-Arquivo": nome}
    if recompensas:
        # o token só vale para quem exportou (mesmo IPv4 ou mesmo /48)
        headers["X-Token-Resgate"] = recompensas.emitir_token(antiabuso.hash_cota(cfg.ip_salt, ip))
    evento("exportacao", largura=largura, retrato=c.retrato, formato=c.formato, fonte=str(p.get("fonte"))[:20], circulo=c.circulo,
           bytes=len(png), token_emitido=bool(recompensas), ip=antiabuso.hash_ip(cfg.ip_salt, ip)[:12])
    return Response(png, media_type="image/png", headers=headers)


def sufixo_formato(formato):
    return "" if formato == "paisagem" else "_" + formato


def nome_base(p):
    base = p.get("nome") if isinstance(p.get("nome"), str) and p.get("nome") else str(p.get("fonte") or "rede")
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-")[:40] or "rede"


# ---------- animação ----------
@app.post("/api/animacao")
async def animacao(req: Request):
    ip = ip_de(req)
    limitar(lim_animacao, ip, "Muitas animações em pouco tempo. Espere alguns minutos.")
    p = await ler_json(req)
    tipo = p.get("tipo", "mp4")
    try:
        dados, c = await AnimacaoRender(render, FFMPEG).gerar(p, tipo, limites=limites_render(ip))
    except (ParametroInvalido, ImagemInvalida) as e:
        raise Recusado(400, str(e)) from e
    except Excesso as e:
        raise Recusado(429, EXCESSO) from e
    except Ocupado as e:
        raise Recusado(503, "Muita gente criando agora. Tente de novo em alguns segundos.") from e
    except FalhaAnimacao as e:
        evento("animacao_falhou", "error", motivo=str(e)[:200])
        raise Recusado(500, "Não consegui gerar a animação. Tente de novo.") from e
    nome = f"{nome_base(p)}{sufixo_formato(c.formato)}_s{c.seed}_animacao.{tipo}"
    return Response(dados, media_type="video/mp4" if tipo == "mp4" else "image/gif",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"', "X-Nome-Arquivo": nome,
                             "Cache-Control": "no-store"})


# ---------- modelos de estilo (miniaturas estáticas) ----------
ESTILOS = os.path.join(STATIC, "estilos")
_ARQ_ESTILO = re.compile(r"[a-z0-9_]{1,40}\.(webp|json)")


@app.get("/estilos/{arquivo}")
async def estilo_arquivo(arquivo: str):
    if not _ARQ_ESTILO.fullmatch(arquivo) or not os.path.isfile(os.path.join(ESTILOS, arquivo)):
        raise HTTPException(404)
    json_ = arquivo.endswith(".json")
    return FileResponse(os.path.join(ESTILOS, arquivo), media_type="application/json" if json_ else "image/webp",
                        headers={"Cache-Control": "no-cache" if json_ else "public, max-age=86400"})


# ---------- recompensas ----------
@app.get("/api/desafio")
async def desafio(req: Request):
    exigir_recompensas()
    limitar(lim_desafio, ip_de(req))
    return antiabuso.criar_desafio(cfg.hmac_key, cfg.pow_bits)


@app.get("/api/meu-dia")
async def meu_dia(req: Request):
    """Progresso do dia por IP (não aceita endereço: não serve para espiar os outros)."""
    exigir_recompensas()
    ip = ip_de(req)
    limitar(lim_meu_dia, ip)
    return recompensas.meu_dia("", antiabuso.hash_cota(cfg.ip_salt, ip))


@app.post("/api/resgatar")
async def resgatar(req: Request):
    exigir_recompensas()
    ip = ip_de(req)
    limitar(lim_resgate, ip, "Muitas tentativas de resgate. Espere alguns minutos.")
    p = await ler_json(req)

    def global_():  # só depois de token e prova de trabalho válidos (veja Recompensas.resgatar)
        limitar(lim_resgate_global, "todos", "Muitos resgates ao mesmo tempo. Tente de novo em alguns minutos.")
    try:
        # cota por "pessoa": IPv4 ou bloco IPv6 /48 (um /48 se consegue de graça)
        return await recompensas.resgatar(p.get("token"), p.get("endereco"),
                                          antiabuso.hash_cota(cfg.ip_salt, ip), p.get("prova"),
                                          antes_da_rede=global_)
    except (ResgateErro, LnurlErro) as e:
        raise Recusado(400, str(e)) from e


@app.post("/api/doar")
async def doar(req: Request):
    exigir_recompensas()
    limitar(lim_doacao, ip_de(req), "Muitos pedidos de doação. Espere alguns minutos.")
    sats = (await ler_json(req)).get("sats")
    if isinstance(sats, bool) or not isinstance(sats, int) or not 1 <= sats <= 1_000_000:
        raise Recusado(400, "Valor inválido.")
    limitar(lim_doacao_global, "todos", "Muitas cobranças criadas agora. Tente de novo em alguns minutos.")
    try:
        inv = await recompensas.criar_doacao(sats)
        evento("doacao_criada", sats=sats, doacao=inv["hash"][:40])
    except CoinosErro as e:
        raise Recusado(502, "Não consegui criar a cobrança agora. Tente de novo.") from e
    return {"hash": inv["hash"], "bolt11": inv["bolt11"], "qr": qr_svg(f"lightning:{inv['bolt11']}")}


@app.get("/api/doar/{hash_}")
async def doacao_status(hash_: str, req: Request):
    exigir_recompensas()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", hash_):
        raise Recusado(404, "Doação não encontrada.")
    limitar(lim_status, ip_de(req))
    limitar(lim_status_global, "todos")
    try:
        return {"pago": await recompensas.doacao_paga(hash_)}
    except ResgateErro as e:
        raise Recusado(404, str(e)) from e
    except CoinosErro as e:
        raise Recusado(502, "Não consegui consultar agora.") from e



# ---------- reportar problema ----------
# As opções prontas ficam aqui (a página recebe a lista em /api/info): o visitante só escolhe
# chaves, nunca manda o texto dos rótulos.
TIPOS_REPORT = {
    "sats_nao_chegaram": "Não recebi os sats",
    "erro_resgate": "Deu erro ao resgatar",
    "arte_travou": "A arte não gera ou travou",
    "imagem": "Minha imagem não funcionou",
    "compartilhar": "Compartilhar não funciona",
    "lento": "Site lento ou fora do ar",
    "outro": "Outra coisa",
}
MAX_REPORT_TEXTO, MAX_REPORT_CONTATO, MAX_REPORT_ERRO = 1000, 120, 300
_CONTROLE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


def _texto_report(valor, limite, linhas=True):
    """Texto livre do visitante: só string, sem caracteres de controle (nem de inverter a
    direção do texto), sem linhas em branco em excesso e cortado no limite."""
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise Recusado(400, "Pedido inválido.")
    t = _CONTROLE.sub("", valor.replace("\r\n", "\n").replace("\r", "\n"))
    t = re.sub(r"\n{3,}", "\n\n", t) if linhas else " ".join(t.split())
    return t.strip()[:limite]


@app.post("/api/reportar")
async def reportar(req: Request):
    if not telegram:
        raise HTTPException(404, "Relatos desligados neste servidor.")
    ip = ip_de(req)
    limitar(lim_report, ip, "Você já mandou alguns relatos agora. Espere uns minutos.")
    p = await ler_json(req)
    tipos = p.get("tipos") or []
    if (not isinstance(tipos, list) or len(tipos) > len(TIPOS_REPORT)
            or any(not isinstance(t, str) or t not in TIPOS_REPORT for t in tipos)):
        raise Recusado(400, "Pedido inválido.")
    tipos = list(dict.fromkeys(tipos))
    texto = _texto_report(p.get("mensagem"), MAX_REPORT_TEXTO)
    contato = _texto_report(p.get("contato"), MAX_REPORT_CONTATO, linhas=False)
    ultimo_erro = _texto_report(p.get("erro"), MAX_REPORT_ERRO, linhas=False)
    tela = _texto_report(p.get("tela"), 20, linhas=False)
    if not tipos and not texto:
        raise Recusado(400, "Marque o que aconteceu ou escreva uma mensagem.")
    limitar(lim_report_global, "todos", "Muitos relatos agora. Tente de novo mais tarde.")

    pessoa = antiabuso.hash_ip(cfg.ip_salt, ip)[:12]  # o mesmo hash dos eventos do log
    partes = ["🐞 Relato de problema · Rede de Nós"]
    if tipos:
        partes.append("\n".join("• " + TIPOS_REPORT[t] for t in tipos))
    if texto:
        partes.append("💬 " + texto)
    if contato:
        partes.append("📮 Contato: " + contato)
    if ultimo_erro:
        partes.append("⚠️ Último erro na tela: " + ultimo_erro)
    contexto = [f"Pessoa (hash do IP nos logs): {pessoa}"]
    if recompensas:
        dia = recompensas.meu_dia("", antiabuso.hash_cota(cfg.ip_salt, ip))
        contexto.append(f"Hoje: {dia['feitos']} resgate(s), {dia['ganho_hoje']} de {cfg.limite_dia_usuario} sats")
        with suppress(CoinosErro):
            contexto.append(f"Pote: {await recompensas.saldo()} sats (reserva {cfg.reserva_pote})")
    formato = "em pé" if p.get("retrato") is True else "deitada"
    if isinstance(p.get("formato"), str) and p["formato"] in FORMATOS:
        formato = p["formato"]
    contexto.append(f"Arte: {formato}" + (f" · tela {tela}" if tela else ""))
    contexto.append(f"Idioma da página: {_idioma(req)}")
    contexto.append("Navegador: " + _texto_report(req.headers.get("user-agent"), 160, linhas=False))
    contexto.append(f"Versão do app: {versao_app()} · {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC")
    partes.append("\n".join(contexto))

    try:
        await telegram.enviar("\n\n".join(partes))
        db.guardar_relato("\n\n".join(partes))
    except TelegramErro as e:
        evento("report_falhou", "error", motivo=str(e), ip=pessoa)
        raise Recusado(502, "Não consegui enviar agora. Tente de novo em alguns minutos.") from e
    # o texto do relato vai só para o Telegram, nunca para o log (pode ter endereço, e-mail...)
    evento("report_enviado", tipos=tipos, caracteres=len(texto), contato=bool(contato), ip=pessoa)
    return {"ok": True}


# ---------- painel admin (/admin; 404 sem ADMIN_TOKEN) ----------
app.include_router(admin.criar_router(cfg, db, lambda: recompensas, ip_de, ler_json))

# ---------- link da arte (/a/<id>) e galeria pública: veja servidor/galeria.py ----------
from . import galeria  # noqa: E402

app.include_router(galeria.criar_router(
    cfg=cfg, render=render, ip_de=ip_de, limitar=limitar, ler_json=ler_json, Recusado=Recusado,
    limites_render=limites_render, pagina=pagina_com_versao, versao=versao_app))

# ---------- urna de brincadeira (/urna; 404 sem URNA_ATIVA=1): veja servidor/urna.py ----------
from . import urna  # noqa: E402

app.include_router(urna.criar_router(
    cfg=cfg, db=db, recompensas_de=lambda: recompensas, ip_de=ip_de, limitar=limitar, ler_json=ler_json,
    Recusado=Recusado, versao=versao_app))
