"""Endereço Lightning (usuario@dominio) -> invoice bolt11 via LNURL-pay.

Como o domínio vem do usuário, cada requisição:
- só vai para HTTPS na porta 443, sem usuário/senha na URL e sem seguir redirecionamentos;
- resolve o DNS uma vez, recusa qualquer IP que não seja público e conecta
  NESSE IP (o certificado continua sendo conferido para o nome original), o que
  impede DNS rebinding;
- tem tamanho de resposta (sem compressão) e tempo total limitados.
A invoice devolvida é decodificada e conferida (checksum, rede, valor, validade e
taxa das rotas privadas, que quem cria a invoice escolhe).
"""
import asyncio
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlparse, urlunparse

import httpx

from .eventos import evento

ENDERECO_RE = re.compile(r"^[a-z0-9._+-]{1,64}@([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")
MAX_RESPOSTA = 64 * 1024
PRAZO_TOTAL_S = 15
REDES_PROIBIDAS = [ipaddress.ip_network(r) for r in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16")]


class LnurlErro(Exception):
    pass


def normalizar(endereco):
    e = (endereco or "").strip().lower() if isinstance(endereco, str) else ""
    if e.startswith("lightning:"):
        e = e[len("lightning:"):]
    if not ENDERECO_RE.match(e):
        raise LnurlErro("Endereço Lightning inválido. Use o formato usuario@dominio.")
    return e


# ---------- BOLT11 ----------
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_MULT = {"": 10 ** 11, "m": 10 ** 8, "u": 10 ** 5, "n": 10 ** 2, "p": 10 ** -1}  # msat por unidade de BTC


def _polymod(valores):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in valores:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    return chk


def _bytes(palavras):
    """Palavras de 5 bits -> bytes (descarta o resto que não fecha um byte)."""
    acc, bits, out = 0, 0, bytearray()
    for w in palavras:
        acc, bits = (acc << 5) | w, bits + 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


def _taxa_rota_msat(palavras, msat):
    """Taxa total (msat) cobrada pelos saltos de uma rota privada ('r').

    Cada salto cobra sobre o valor que ELE encaminha (que já inclui as taxas dos
    saltos seguintes): acumula de trás para frente, arredondando para cima."""
    dados, saltos = _bytes(palavras), []
    for i in range(0, len(dados) - 50, 51):  # salto = pubkey(33) scid(8) base(4) prop(4) cltv(2)
        saltos.append((int.from_bytes(dados[i + 41:i + 45], "big"),
                       int.from_bytes(dados[i + 45:i + 49], "big")))
    montante = msat or 0
    for base, prop in reversed(saltos):
        montante += base + (montante * prop + 999_999) // 1_000_000
    return montante - (msat or 0)


def decodificar_bolt11(bolt11):
    """Devolve {msat, timestamp, expira_em, taxa_rotas_msat}. Levanta LnurlErro se inválida.

    A assinatura não é conferida aqui (a Coinos confere ao pagar); o resto sim.
    """
    if not isinstance(bolt11, str) or len(bolt11) > 2000:
        raise LnurlErro("A carteira devolveu uma invoice inválida.")
    s = bolt11.strip().lower()
    if s.startswith("lightning:"):
        s = s[len("lightning:"):]
    pos = s.rfind("1")
    hrp, dados = s[:pos], s[pos + 1:]
    if pos < 1 or len(dados) < 7 + 104 + 6 or any(c not in _CHARSET for c in dados):
        raise LnurlErro("A carteira devolveu uma invoice inválida.")
    palavras = [_CHARSET.index(c) for c in dados]
    if _polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + palavras) != 1:
        raise LnurlErro("A invoice da carteira está corrompida (checksum).")
    m = re.fullmatch(r"lnbc([1-9]\d{0,15})?([munp]?)", hrp)
    if not m or (m.group(2) and not m.group(1)):
        raise LnurlErro("A invoice não é da rede principal do Bitcoin.")
    msat = None
    if m.group(1):
        valor = int(m.group(1))
        if m.group(2) == "p":  # picossats: só vale se fechar msat inteiro
            if valor % 10:
                raise LnurlErro("A invoice tem um valor inválido.")
            msat = valor // 10
        else:
            msat = valor * _MULT[m.group(2)]

    corpo = palavras[:-6 - 104]  # tira checksum e assinatura
    timestamp = 0
    for w in corpo[:7]:
        timestamp = timestamp * 32 + w
    malformada = LnurlErro("A carteira devolveu uma invoice malformada.")
    expira, taxa_rotas, i, vistos = 3600, 0, 7, set()
    while i < len(corpo):  # cada campo precisa caber inteiro no corpo, sem sobras
        if i + 3 > len(corpo):
            raise malformada
        tipo, tam = corpo[i], corpo[i + 1] * 32 + corpo[i + 2]
        if i + 3 + tam > len(corpo):
            raise malformada
        valor = corpo[i + 3:i + 3 + tam]
        if tipo == 6:  # 'x': expiração em segundos
            if tipo in vistos or not 1 <= tam <= 10:
                raise malformada
            expira = 0
            for w in valor:
                expira = expira * 32 + w
        elif tipo == 3:  # 'r': rota privada; quem cria a invoice escolhe a taxa dela
            n = len(_bytes(valor))
            if n == 0 or n % 51:
                raise malformada
            taxa_rotas = max(taxa_rotas, _taxa_rota_msat(valor, msat))
        vistos.add(tipo)
        i += 3 + tam
    return {"msat": msat, "timestamp": timestamp, "expira_em": timestamp + expira, "taxa_rotas_msat": taxa_rotas}


def conferir_invoice(bolt11, msat_esperado, taxa_max_sats=2):
    inv = decodificar_bolt11(bolt11)
    if inv["msat"] != msat_esperado:
        raise LnurlErro("A invoice da carteira veio com valor diferente do pedido.")
    if inv["expira_em"] < time.time() + 60:
        raise LnurlErro("A invoice da carteira já está vencida.")
    if inv["taxa_rotas_msat"] > taxa_max_sats * 1000:
        raise LnurlErro("A carteira desse endereço cobra uma taxa de rota alta demais. "
                        "Use outro endereço Lightning.")
    return inv


# ---------- rede ----------
def _ip_permitido(ip):
    ip = ip.ipv4_mapped or ip if ip.version == 6 else ip
    return ip.is_global and not any(ip in r for r in REDES_PROIBIDAS)


async def _resolver(host):
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise LnurlErro(f"Não encontrei o domínio {host}.") from e
    ips = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not ips or not all(_ip_permitido(ip) for ip in ips):
        evento("lnurl_bloqueado", "warning", host=host[:120], motivo="domínio aponta para rede não pública",
               ips=[str(ip) for ip in ips][:5])
        raise LnurlErro("Esse domínio não aponta para um endereço público.")
    return ips[0]


async def _get_json(cliente, url):
    p = urlparse(url)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.port not in (None, 443)):
        raise LnurlErro("O serviço da carteira precisa usar HTTPS padrão (porta 443).")
    host = p.hostname
    ip = await _resolver(host)
    alvo = f"[{ip}]" if ip.version == 6 else str(ip)
    url_ip = urlunparse(p._replace(netloc=alvo))
    try:
        async with cliente.stream("GET", url_ip, headers={"Host": host, "Accept-Encoding": "identity"},
                                  extensions={"sni_hostname": host}) as r:
            # resposta comprimida poderia virar dezenas de MB antes do limite: só aceitamos sem compressão
            if r.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                raise LnurlErro("A carteira respondeu comprimido; não aceitamos.")
            corpo = b""
            async for parte in _bytes_crus(r):
                corpo += parte
                if len(corpo) > MAX_RESPOSTA:
                    raise LnurlErro("Resposta grande demais da carteira.")
            if r.status_code >= 400:
                raise LnurlErro(f"A carteira respondeu {r.status_code}.")
            if r.status_code >= 300:
                raise LnurlErro("A carteira tentou redirecionar; não seguimos redirecionamentos.")
    except httpx.HTTPError as e:
        raise LnurlErro("Não consegui falar com a carteira desse endereço.") from e
    try:
        d = json.loads(corpo)
    except ValueError as e:
        raise LnurlErro("A carteira devolveu uma resposta inválida.") from e
    if not isinstance(d, dict):
        raise LnurlErro("A carteira devolveu uma resposta inválida.")
    if str(d.get("status", "")).upper() == "ERROR":
        raise LnurlErro("A carteira recusou o pagamento.")
    return d


async def _bytes_crus(r):
    """Bytes da resposta como chegaram (sem descompressão, já recusada acima)."""
    if r.is_stream_consumed:  # resposta já em memória (transportes de teste)
        yield r.content
        return
    async for parte in r.aiter_raw():
        yield parte


def _inteiro(v, padrao):
    if v is None:
        return padrao
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise LnurlErro("A carteira devolveu limites inválidos.")
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError) as e:
        raise LnurlErro("A carteira devolveu limites inválidos.") from e


async def _invoice_para(endereco, sats, transport, taxa_max_sats):
    usuario, dominio = normalizar(endereco).split("@")
    msat = int(sats) * 1000
    async with httpx.AsyncClient(timeout=8, follow_redirects=False, transport=transport) as c:
        info = await _get_json(c, f"https://{dominio}/.well-known/lnurlp/{usuario}")
        callback = info.get("callback")
        if info.get("tag") != "payRequest" or not isinstance(callback, str) or len(callback) > 2000:
            raise LnurlErro("Esse endereço não aceita pagamentos Lightning (LNURL-pay).")
        minimo, maximo = _inteiro(info.get("minSendable"), 1000), _inteiro(info.get("maxSendable"), 0)
        if msat < minimo:
            raise LnurlErro(f"A carteira desse endereço só aceita a partir de {max(1, minimo // 1000)} sats.")
        if maximo and msat > maximo:
            raise LnurlErro("A carteira desse endereço não aceita esse valor.")
        sep = "&" if "?" in callback else "?"
        res = await _get_json(c, f"{callback}{sep}amount={msat}")
    bolt11 = res.get("pr")
    conferir_invoice(bolt11, msat, taxa_max_sats)
    return bolt11


async def _verificar(endereco, transport):
    usuario, dominio = normalizar(endereco).split("@")
    async with httpx.AsyncClient(timeout=8, follow_redirects=False, transport=transport) as c:
        info = await _get_json(c, f"https://{dominio}/.well-known/lnurlp/{usuario}")
    if info.get("tag") != "payRequest" or not isinstance(info.get("callback"), str):
        raise LnurlErro("Esse endereço Lightning não existe ou não aceita pagamentos.")


async def verificar_endereco(endereco, transport=None):
    """Confere, sem pagar nada, que o endereço existe (o servidor dele responde LNURL-pay)."""
    try:
        await asyncio.wait_for(_verificar(endereco, transport), PRAZO_TOTAL_S)
    except asyncio.TimeoutError as e:
        raise LnurlErro("A carteira desse endereço demorou demais para responder.") from e


async def invoice_para(endereco, sats, transport=None, taxa_max_sats=2):
    """Pede à carteira do endereço uma invoice de exatamente `sats` e a devolve."""
    try:
        return await asyncio.wait_for(_invoice_para(endereco, sats, transport, taxa_max_sats), PRAZO_TOTAL_S)
    except asyncio.TimeoutError as e:
        raise LnurlErro("A carteira desse endereço demorou demais para responder.") from e
