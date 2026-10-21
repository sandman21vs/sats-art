"""Utilidades dos testes: invoices BOLT11 sintéticas (checksum válido, assinatura falsa)."""
import time

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(valores):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in valores:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    return chk


def _palavras(n, minimo=1):
    out = []
    while n or len(out) < minimo:
        out.insert(0, n & 31)
        n >>= 5
    return out


def _tag(tipo, palavras):
    return [tipo, len(palavras) // 32, len(palavras) % 32] + palavras


def _palavras_de_bytes(dados):
    acc, bits, out = 0, 0, []
    for b in dados:
        acc, bits = (acc << 8) | b, bits + 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def rota(base_msat=1000, prop_ppm=1):
    """Um salto de rota privada (51 bytes) com a taxa dada."""
    return (bytes(33) + bytes(8) + base_msat.to_bytes(4, "big") + prop_ppm.to_bytes(4, "big")
            + (40).to_bytes(2, "big"))


def fazer_bolt11(sats=10, expira=3600, rotas=(), ts=None, rede="bc", corromper=False):
    ts = int(time.time()) if ts is None else ts
    hrp = f"ln{rede}{sats * 10}n" if sats else f"ln{rede}"
    dados = _palavras(ts, 7)[-7:]
    dados += _tag(1, [0] * 52)                 # hash do pagamento
    dados += _tag(6, _palavras(expira))        # expiração
    for saltos in rotas:                       # cada item: bytes de um ou mais saltos
        dados += _tag(3, _palavras_de_bytes(saltos))
    dados += [0] * 104                         # "assinatura"
    vals = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + dados + [0] * 6
    pm = _polymod(vals) ^ 1
    dados += [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    texto = hrp + "1" + "".join(CHARSET[d] for d in dados)
    if corromper:
        meio = len(hrp) + 10
        texto = texto[:meio] + ("q" if texto[meio] != "q" else "p") + texto[meio + 1:]
    return texto


# ---------- nenhum teste fala com a rede de verdade ----------
import pytest  # noqa: E402

from servidor import lnurl  # noqa: E402

verificar_endereco_real = lnurl.verificar_endereco


@pytest.fixture(autouse=True)
def coinos_lnurl_falso(monkeypatch):
    """Simula o LNURL público da Coinos: só usuários alfanuméricos existem."""
    async def falso(endereco, transport=None):
        usuario = lnurl.normalizar(endereco).split("@")[0]
        if not usuario.isalnum():
            raise lnurl.LnurlErro("Esse endereço Lightning não existe ou não aceita pagamentos.")
    monkeypatch.setattr(lnurl, "verificar_endereco", falso)


@pytest.fixture(autouse=True)
def balde_de_eventos_cheio(monkeypatch):
    """O teto de linhas por segundo dos eventos ruidosos é global: cada teste começa com ele cheio."""
    import time as _time

    from servidor import eventos
    monkeypatch.setattr(eventos, "_balde", [float(eventos.RUIDOSOS_POR_S * 10), _time.monotonic()])
    monkeypatch.setattr(eventos, "_suprimidos", 0)
