"""Verificação do token de admin para a moderação da galeria (/admin/galeria).

Provisório, em arquivo próprio para não conflitar com o painel /admin: aceita o mesmo
cookie `admin` (HttpOnly, SameSite=Strict) com o valor de ADMIN_TOKEN, comparado em tempo
constante. Quando o painel /admin existir, basta trocar `admin_ok` pela verificação dele.
"""
import hmac
import os
from http.cookies import CookieError, SimpleCookie

NOME_COOKIE = "admin"
MIN_TOKEN = 12  # token curto demais = moderação desligada (adivinhável)


def token_admin(cfg=None):
    """ADMIN_TOKEN do ambiente (ou do Config, se um dia ele ganhar o campo). Vazio = desligado."""
    t = getattr(cfg, "admin_token", None) or os.environ.get("ADMIN_TOKEN", "")
    t = t.strip()
    return t if len(t) >= MIN_TOKEN else ""


def _valores_do_cookie(cabecalho):
    """Todos os valores de `admin` no Cookie (o navegador manda um por Path quando há mais de um)."""
    valores = []
    for parte in (cabecalho or "").split(";"):
        nome, _, valor = parte.strip().partition("=")
        if nome == NOME_COOKIE and valor:
            try:  # aceita o valor entre aspas, como o SimpleCookie grava
                c = SimpleCookie()
                c.load(f"{NOME_COOKIE}={valor}")
                valores.append(c[NOME_COOKIE].value)
            except CookieError:
                valores.append(valor)
    return valores[:4]


def confere(enviado, token):
    return bool(token) and isinstance(enviado, str) and hmac.compare_digest(enviado.encode(), token.encode())


def admin_ok(req, token):
    """O pedido traz o cookie `admin` certo? Sempre False sem token configurado."""
    if not token:
        return False
    ok = False
    for v in _valores_do_cookie(req.headers.get("cookie")):
        ok |= confere(v, token)  # sem curto-circuito: o tempo não diz qual valor bateu
    return ok


def cabecalho_cookie(token, seguro):
    """Set-Cookie do login da moderação (o painel /admin deve gravar o mesmo)."""
    return (f"{NOME_COOKIE}={token}; Path=/admin; HttpOnly; SameSite=Strict; Max-Age=43200"
            + ("; Secure" if seguro else ""))
