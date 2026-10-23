"""Urna de brincadeira (/urna): vote '13 ou '22 e receba esse tanto de sats.

- Um voto por "pessoa" (a mesma cota dos resgates: IPv4 ou bloco IPv6 /48).
- Votar emite um token de resgate do tipo "urna" com o valor do voto. O resgate passa pelo
  mesmo fluxo das recompensas da arte (prova de trabalho, pote, Coinos), mas com limites
  próprios: um resgate por pessoa/endereço por dia e URNA_DAILY_GLOBAL_SATS no dia.
- O placar só é devolvido para quem já votou.

Desligada por padrão (URNA_ATIVA=1 liga). Desligada, todas as rotas respondem 404.
Registrado em main.py por `criar_router(...)`, como a galeria.
"""
import os
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import antiabuso
from .eventos import evento
from .lnurl import LnurlErro
from .db import CONTA
from .recompensas import ResgateErro, hoje

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(RAIZ, "static")

NUMEROS = ("13", "22")  # o número é também o valor do voto em sats

ESQUEMA = """
CREATE TABLE IF NOT EXISTS votos (
    cota TEXT PRIMARY KEY,   -- hash da cota (antiabuso.hash_cota): um voto por pessoa
    numero TEXT NOT NULL, criado REAL NOT NULL,
    token TEXT               -- token de resgate emitido com o voto (NULL sem recompensas)
);
"""


class Votos:
    def __init__(self, db):
        self.db = db
        with db.trava:
            db.con.executescript(ESQUEMA)

    def votar(self, cota, numero):
        """Grava o voto; False se essa pessoa já tinha votado."""
        return self.db.ex("INSERT OR IGNORE INTO votos(cota, numero, criado) VALUES (?, ?, ?)",
                          cota, numero, time.time()).rowcount == 1

    def guardar_token(self, cota, token):
        self.db.ex("UPDATE votos SET token = ? WHERE cota = ?", token, cota)

    def voto_de(self, cota):
        """(numero, token) ou None."""
        return self.db.um("SELECT numero, token FROM votos WHERE cota = ?", cota)

    def zerar(self):
        """Apaga todos os votos (painel admin). Devolve quantos foram apagados."""
        return self.db.ex("DELETE FROM votos").rowcount

    def por_dia(self, desde):
        """{dia: (votos 13, votos 22)} a partir de `desde` (dias em UTC)."""
        with self.db.trava:
            linhas = self.db.con.execute(
                "SELECT strftime('%Y-%m-%d', criado, 'unixepoch') AS d, numero, COUNT(*) FROM votos "
                "GROUP BY d, numero HAVING d >= ?", (desde,)).fetchall()
        dias = {}
        for d, numero, n in linhas:
            v13, v22 = dias.get(d, (0, 0))
            dias[d] = (v13 + n, v22) if numero == "13" else (v13, v22 + n)
        return dias

    def placar(self):
        with self.db.trava:
            linhas = dict(self.db.con.execute("SELECT numero, COUNT(*) FROM votos GROUP BY numero").fetchall())
        p = {n: linhas.get(n, 0) for n in NUMEROS}
        return {**p, "total": sum(p.values())}


def resumo_admin(db, cfg, dias):
    """Números da urna para o painel /admin: placar completo, votos e sats por dia, gasto de hoje."""
    votos = Votos(db)
    por_dia = votos.por_dia(dias[0])
    with db.trava:
        pagos = {d: (sats, n) for d, sats, n in db.con.execute(
            "SELECT dia, COALESCE(SUM(sats), 0), COUNT(*) FROM resgates WHERE tipo = 'urna' AND status = 'pago' "
            "AND dia >= ? GROUP BY dia", (dias[0],)).fetchall()}
    gasto_hoje = db.um(f"SELECT COALESCE(SUM(sats + fee), 0) FROM resgates WHERE dia = ? AND tipo = 'urna' "
                       f"AND status IN {CONTA}", hoje())[0]
    return {
        "ativa": cfg.urna_ativa,
        "placar": votos.placar(),
        "limite_dia": cfg.urna_limite_dia_global,
        "reserva": cfg.urna_reserva_pote,
        "gasto_hoje": gasto_hoje,
        "total_sats": sum(v[0] for v in pagos.values()),
        "total_resgates": sum(v[1] for v in pagos.values()),
        "dias": [{"dia": d, "votos": sum(por_dia.get(d, (0, 0))), "v13": por_dia.get(d, (0, 0))[0],
                  "v22": por_dia.get(d, (0, 0))[1], "sats": pagos.get(d, (0, 0))[0]} for d in dias],
    }


def contexto_relato(db, votos, cota, cfg, recompensas):
    """Linhas sobre a urna que vão junto num relato do botão "Reportar problema" (Telegram)."""
    voto = votos.voto_de(cota)
    if not voto:
        return ["Urna: ainda não votou"]
    numero, token = voto
    linhas = [f"Urna: votou '{numero}"]
    if recompensas:
        resgatou = db.contar_dia(hoje(), "ip_hash", cota, "urna")
        if resgatou:
            linhas.append(f"Resgate da urna hoje: sim ({numero} sats)")
        elif token and db.token_valido(token, cfg.token_validade_s, cota, "urna"):
            linhas.append("Resgate da urna: ainda não resgatou (token válido)")
        else:
            linhas.append("Resgate da urna: token vencido ou já usado")
    return linhas


def criar_router(*, cfg, db, recompensas_de, ip_de, limitar, ler_json, Recusado, versao, votos=None):
    router = APIRouter()
    votos = votos or Votos(db)
    lim_estado = antiabuso.Limitador(60, 60)
    lim_voto = antiabuso.Limitador(10, 600)
    lim_resgate = antiabuso.Limitador(10, 600)
    lim_resgate_global = antiabuso.Limitador(300, 600)

    def exigir_urna():
        if not cfg.urna_ativa:
            raise HTTPException(404)

    def cota_de(req):
        return antiabuso.hash_cota(cfg.ip_salt, ip_de(req))

    def token_vivo(token, cota):
        """O token do voto, se ainda dá para resgatar com ele."""
        if recompensas_de() and token and db.token_valido(token, cfg.token_validade_s, cota, "urna"):
            return token
        return None

    def gerais():
        """O que todo mundo vê, votando ou não: total de votos (sem dizer quem lidera) e o
        quanto ainda sobra do limite de sats da urna no dia."""
        gasto = db.um(f"SELECT COALESCE(SUM(sats + fee), 0) FROM resgates WHERE dia = ? AND tipo = 'urna' "
                      f"AND status IN {CONTA}", hoje())[0]
        return {"recompensas": bool(recompensas_de()), "numeros": list(NUMEROS),
                "total_votos": votos.placar()["total"],
                "urna_hoje": {"limite": cfg.urna_limite_dia_global, "gasto": gasto,
                              "reserva": cfg.urna_reserva_pote}}

    def resposta(cota, numero, token):
        r = {"votou": True, "numero": numero, "placar": votos.placar(), "token": token_vivo(token, cota)}
        r["sats"] = int(numero) if r["token"] else 0
        return r

    @router.get("/urna")
    async def pagina():
        exigir_urna()
        with open(os.path.join(STATIC, "urna.html"), encoding="utf-8") as f:
            html = f.read().replace('src="/urna.js"', f'src="/urna.js?v={versao("urna.js")}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @router.get("/urna.js")
    async def script():
        exigir_urna()
        return FileResponse(os.path.join(STATIC, "urna.js"), media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @router.get("/api/urna")
    async def estado(req: Request):
        """Quem ainda não votou não recebe o placar."""
        exigir_urna()
        limitar(lim_estado, ip_de(req))
        base = gerais()
        cota = cota_de(req)
        voto = votos.voto_de(cota)
        if not voto:
            return {**base, "votou": False}
        return {**base, **resposta(cota, *voto)}

    @router.post("/api/urna/votar")
    async def votar(req: Request):
        exigir_urna()
        ip = ip_de(req)
        limitar(lim_voto, ip, "Muitos votos seguidos. Espere um pouquinho.")
        numero = (await ler_json(req)).get("numero")
        if numero not in NUMEROS:
            raise Recusado(400, "Vote em 13 ou 22.")
        cota = cota_de(req)
        if votos.votar(cota, numero):
            r = recompensas_de()
            if r:  # o token só nasce depois do voto gravado: dois cliques não rendem dois tokens
                votos.guardar_token(cota, r.emitir_token(cota, tipo="urna", sats=int(numero)))
            evento("urna_voto", numero=numero, ip=antiabuso.hash_ip(cfg.ip_salt, ip)[:12])
        numero_gravado, token = votos.voto_de(cota)  # quem já tinha votado recebe o voto original
        return {**gerais(), **resposta(cota, numero_gravado, token)}

    @router.post("/api/urna/resgatar")
    async def resgatar(req: Request):
        exigir_urna()
        r = recompensas_de()
        if not r:
            raise HTTPException(404, "Recompensas desligadas neste servidor.")
        ip = ip_de(req)
        limitar(lim_resgate, ip, "Muitas tentativas de resgate. Espere alguns minutos.")
        p = await ler_json(req)

        def global_():
            limitar(lim_resgate_global, "todos",
                    "Muitos resgates ao mesmo tempo. Tente de novo em alguns minutos.")
        try:
            return await r.resgatar(p.get("token"), p.get("endereco"), cota_de(req), p.get("prova"),
                                    antes_da_rede=global_, tipo="urna")
        except (ResgateErro, LnurlErro) as e:
            raise Recusado(400, str(e)) from e

    return router
