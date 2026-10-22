"""Verificação do admin para a moderação da galeria (/admin/galeria).

Usa a mesma sessão do painel /admin (cookie assinado `admin_sessao`, veja servidor/admin.py):
um login vale para os dois.
"""
import hashlib
import hmac
import os
import time

from .admin import COOKIE, SESSAO_S, _assinar

MIN_TOKEN = 12  # token curto demais = moderação desligada (adivinhável)


def token_admin(cfg=None):
    """ADMIN_TOKEN do Config (ou do ambiente). Vazio = desligado."""
    t = getattr(cfg, "admin_token", None) or os.environ.get("ADMIN_TOKEN", "")
    t = t.strip()
    return t if len(t) >= MIN_TOKEN else ""


def confere(enviado, token):
    if not token or not isinstance(enviado, str):
        return False
    # compara os hashes: tempo constante e sem vazar o tamanho do token
    return hmac.compare_digest(hashlib.sha256(enviado.encode()).digest(), hashlib.sha256(token.encode()).digest())


def admin_ok(req, token):
    """O pedido traz a sessão de admin válida? Sempre False sem token configurado."""
    if not token:
        return False
    expira, _, assinatura = (req.cookies.get(COOKIE) or "").partition(".")
    if not expira.isdigit() or int(expira) < time.time():
        return False
    return hmac.compare_digest(assinatura, _assinar(token, expira))


def cabecalho_cookie(token, seguro):
    """Set-Cookie da sessão de admin (o mesmo que o login do painel /admin grava)."""
    expira = str(int(time.time()) + SESSAO_S)
    return (f"{COOKIE}={expira}.{_assinar(token, expira)}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSAO_S}"
            + ("; Secure" if seguro else ""))
