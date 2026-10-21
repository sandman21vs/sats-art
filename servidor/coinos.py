"""Cliente mínimo da API da Coinos (https://coinos.io/api, token Bearer)."""
import logging
import math

import httpx

log = logging.getLogger("coinos")


class CoinosErro(Exception):
    """ambiguo=True: não dá para garantir que nada saiu da conta.

    A mensagem é sempre genérica (vai para o visitante); o detalhe só vai para o log.
    """

    def __init__(self, msg, ambiguo=False, status=None):
        super().__init__(msg)
        self.ambiguo = ambiguo
        self.status = status  # status HTTP da resposta da Coinos, se houve resposta


class Coinos:
    def __init__(self, token, base="https://coinos.io/api", transport=None):
        self.http = httpx.AsyncClient(
            base_url=base, timeout=httpx.Timeout(30, connect=10), transport=transport,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    async def _req(self, metodo, rota, pagamento=False, **kw):
        """pagamento=True: rota que move sats. Qualquer falha depois de a requisição
        poder ter chegado à Coinos é ambígua (o débito pode ter acontecido)."""
        try:
            r = await self.http.request(metodo, rota, **kw)
        except httpx.ConnectError as e:  # não chegou a conectar: nada foi enviado
            log.warning("Coinos %s %s: sem conexão (%s)", metodo, rota, type(e).__name__)
            raise CoinosErro("Coinos indisponível no momento.") from e
        except httpx.HTTPError as e:
            log.warning("Coinos %s %s: %s", metodo, rota, type(e).__name__)
            raise CoinosErro("A Coinos não respondeu direito.", ambiguo=pagamento) from e
        if r.status_code >= 400:
            # só status e tamanho: o corpo é texto externo e pode conter qualquer coisa
            log.warning("Coinos %s %s -> HTTP %s (%s bytes)", metodo, rota, r.status_code, len(r.content))
            # Num pagamento, nem um 4xx garante que não houve débito: não arriscar.
            raise CoinosErro(f"A Coinos recusou a operação ({r.status_code}).",
                             ambiguo=pagamento, status=r.status_code)
        try:
            d = r.json()
        except ValueError:
            d = None
        if not isinstance(d, dict):
            log.warning("Coinos %s %s: resposta não é um objeto JSON", metodo, rota)
            raise CoinosErro("Resposta inesperada da Coinos.", ambiguo=pagamento)
        if d.get("error"):
            log.warning("Coinos %s %s: HTTP %s com indicação de erro", metodo, rota, r.status_code)
            raise CoinosErro("A Coinos não confirmou a operação.", ambiguo=pagamento)
        if pagamento and not _status_de_sucesso(d):
            log.warning("Coinos %s %s: status de pagamento não confirmado", metodo, rota)
            raise CoinosErro("A Coinos não confirmou o pagamento.", ambiguo=True)
        return d

    async def saldo(self):
        b = (await self._req("GET", "/me")).get("balance")
        return int(b) if isinstance(b, (int, float)) else 0

    async def criar_invoice(self, sats, memo=None):
        inv = {"type": "lightning", "amount": int(sats)}
        if memo:
            inv["memo"] = memo
        d = await self._req("POST", "/invoice", json={"invoice": inv})
        hash_, bolt11 = d.get("id") or d.get("hash"), d.get("text")  # id é curto e serve na consulta
        if not isinstance(hash_, str) or not isinstance(bolt11, str):
            raise CoinosErro("Resposta inesperada da Coinos.")
        return {"hash": hash_, "bolt11": bolt11}

    async def invoice_paga(self, hash_):
        d = await self._req("GET", f"/invoice/{hash_}")
        return bool(d.get("received")) or bool(d.get("settled"))

    async def pagar_bolt11(self, bolt11):
        d = await self._req("POST", "/payments", pagamento=True, json={"payreq": bolt11})
        # fee=None quando a Coinos não informa uma taxa válida: quem chama trata como suspeito
        return {"ref": _comprovante(d, "hash", "id"), "fee": _taxa(d.get("fee"))}

    async def enviar_interno(self, usuario, sats):
        d = await self._req("POST", "/send", pagamento=True, json={"amount": int(sats), "username": usuario})
        return {"ref": _comprovante(d, "id", "hash"), "fee": 0}

    async def fechar(self):
        await self.http.aclose()


# Status que confirmam um pagamento. Qualquer outro valor (pending, rejected, failed...) fica
# ambíguo, para conferir. Sem campo "status" vale o comprovante (id/hash), como observado na Coinos.
STATUS_OK = {"paid", "complete", "completed", "confirmed", "settled", "success", "succeeded", "ok"}


def _status_de_sucesso(d):
    if "status" in d and str(d["status"]).strip().lower() not in STATUS_OK:
        return False
    if d.get("confirmed") is False or d.get("pending") is True:
        return False
    return True


def _comprovante(d, *campos):
    """Sem identificador do pagamento não há confirmação: fica ambíguo, para conferir.
    (Nos pagamentos reais observados, /send devolve `id` UUID e /payments, `hash`.)"""
    for campo in campos:
        v = d.get(campo)
        if isinstance(v, str) and v.strip():
            return v.strip()[:400]
    log.warning("Coinos: pagamento respondido sem identificador")
    raise CoinosErro("A Coinos não confirmou o pagamento.", ambiguo=True)


def _taxa(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return None
    return int(v)
