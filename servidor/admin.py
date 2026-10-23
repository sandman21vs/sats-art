"""Painel do dono em /admin: números do dia a dia, protegidos por ADMIN_TOKEN.

Sem ADMIN_TOKEN tudo aqui responde 404. O token vai num JSON (nunca na URL) e vira um cookie
assinado, HttpOnly e SameSite=Strict, com validade. Tentativas de login são limitadas por IP.
"""
import hashlib
import hmac
import os
import time
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import antiabuso, eventos, urna
from .coinos import CoinosErro

COOKIE = "admin_sessao"
SESSAO_S = 8 * 3600
DIAS = 14
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
SEM_CACHE = {"Cache-Control": "no-store"}


def _dia(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def _hora(t):
    return time.strftime("%Y-%m-%d %H", time.gmtime(t))


def _rotulo_erro(nome, campos):
    """Nome do evento + o detalhe curto que distingue um erro do outro (sem texto livre longo)."""
    if nome == "pedido":
        return f"http {campos.get('status')} {str(campos.get('rota'))[:60]}"
    detalhe = campos.get("tipo") or campos.get("motivo")
    return f"{nome}: {str(detalhe)[:60]}" if detalhe else nome


def _assinar(token, expira):
    return hmac.new(token.encode(), f"admin|{expira}".encode(), hashlib.sha256).hexdigest()


def criar_router(cfg, db, recompensas, ip_de, ler_json):
    """`recompensas` e `ip_de` leem o estado atual de main.py; `ler_json` é o leitor de corpo dele."""
    router = APIRouter()
    lim_login = antiabuso.Limitador(8, 600)

    def contar_evento(nome, nivel, campos):
        agora = time.time()
        if nome == "exportacao":
            db.contar(_dia(agora), "arte")
        elif nivel == "error" or (nome == "pedido" and (campos.get("status") or 0) >= 500):
            db.contar(_hora(agora), "erro:" + _rotulo_erro(nome, campos))

    if cfg.admin_token:
        eventos.gancho = contar_evento

    def exigir_ligado():
        if not cfg.admin_token:
            raise HTTPException(404, "Not Found")

    def token_ok(enviado):
        # compara os hashes: tempo constante e sem vazar o tamanho do token
        a = hashlib.sha256(str(enviado).encode()).digest()
        return hmac.compare_digest(a, hashlib.sha256(cfg.admin_token.encode()).digest())

    def logado(req: Request):
        valor = req.cookies.get(COOKIE) or ""
        expira, _, assinatura = valor.partition(".")
        if not expira.isdigit() or int(expira) < time.time():
            return False
        return hmac.compare_digest(assinatura, _assinar(cfg.admin_token, expira))

    def exigir_login(req: Request):
        exigir_ligado()
        if not logado(req):
            raise HTTPException(401, "Não autenticado")

    @router.get("/admin")
    async def pagina():
        exigir_ligado()
        return FileResponse(os.path.join(STATIC, "admin.html"), headers=SEM_CACHE)

    @router.get("/admin/admin.js")
    async def pagina_js():
        exigir_ligado()
        return FileResponse(os.path.join(STATIC, "admin.js"), media_type="text/javascript", headers=SEM_CACHE)

    @router.post("/api/admin/login")
    async def login(req: Request):
        exigir_ligado()
        ip = ip_de(req)
        quem = antiabuso.hash_ip(cfg.ip_salt, ip)[:12]
        if not lim_login.permitir(antiabuso.chave_ip(ip)):
            eventos.evento("admin_login_limitado", "warning", ip=quem)
            return JSONResponse({"erro": "Tentativas demais. Espere alguns minutos."}, 429, headers=SEM_CACHE)
        corpo = await ler_json(req)
        if not token_ok(corpo.get("token", "")):
            eventos.evento("admin_login_recusado", "warning", ip=quem)
            return JSONResponse({"erro": "Token incorreto."}, 401, headers=SEM_CACHE)
        expira = str(int(time.time()) + SESSAO_S)
        resp = JSONResponse({"ok": True}, headers=SEM_CACHE)
        https = req.url.scheme == "https" or (
            cfg.trust_proxy and req.headers.get("x-forwarded-proto", "").lower() == "https")
        resp.set_cookie(COOKIE, f"{expira}.{_assinar(cfg.admin_token, expira)}", max_age=SESSAO_S,
                        httponly=True, secure=https, samesite="strict", path="/")
        eventos.evento("admin_login", ip=quem)
        return resp

    @router.post("/api/admin/sair")
    async def sair():
        exigir_ligado()
        resp = JSONResponse({"ok": True}, headers=SEM_CACHE)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @router.get("/api/admin/resumo")
    async def resumo(req: Request):
        exigir_login(req)
        agora = time.time()
        dias = [_dia(agora - i * 86400) for i in range(DIAS - 1, -1, -1)]
        artes = {p: n for p, _, n in db.contadores("arte", dias[0])}
        pagos = {d: (sats, n) for d, sats, n in db.sats_por_dia(dias[0])}
        erros = {}
        for _, nome, n in db.contadores("erro:%", _hora(agora - 24 * 3600)):
            erros[nome[5:]] = erros.get(nome[5:], 0) + n
        pote = None
        rec = recompensas()
        if rec:
            with suppress(CoinosErro):
                pote = await rec.saldo()
        return JSONResponse({
            "dias": [{"dia": d, "artes": artes.get(d, 0), "sats": pagos.get(d, (0, 0))[0],
                      "resgates": pagos.get(d, (0, 0))[1]} for d in dias],
            "total_artes": sum(artes.values()),
            "total_sats": sum(v[0] for v in pagos.values()),
            "total_resgates": sum(v[1] for v in pagos.values()),
            "pote": pote,
            "recompensas": bool(rec),
            "pausadas": bool(rec and rec.recompensas_pausadas()),
            "alerta_pote_sats": cfg.alerta_pote_sats,
            "erros": sorted(({"erro": k, "n": v} for k, v in erros.items()), key=lambda e: -e["n"])[:15],
            "relatos": [{"quando": time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) + " UTC", "texto": x}
                        for t, x in db.ultimos_relatos(20)],
            # urna de brincadeira (/urna): o painel vê o placar mesmo sem ter votado
            "urna": urna.resumo_admin(db, cfg, dias) if cfg.urna_ativa else None,
        }, headers=SEM_CACHE)

    @router.post("/api/admin/urna/zerar")
    async def zerar_urna(req: Request):
        """Apaga todos os votos da urna. Resgates e limites do dia continuam valendo."""
        exigir_login(req)
        if not cfg.urna_ativa:
            raise HTTPException(404, "Not Found")
        n = urna.Votos(db).zerar()
        eventos.evento("urna_zerada", "warning", votos=n, ip=antiabuso.hash_ip(cfg.ip_salt, ip_de(req))[:12])
        return JSONResponse({"ok": True, "apagados": n}, headers=SEM_CACHE)

    return router
