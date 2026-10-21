"""Prova de trabalho no navegador (no estilo do ALTCHA, sem serviço externo), IP do
visitante e limitadores.

O servidor entrega {salt, bits, expira, assinatura}. O navegador procura um
nonce tal que sha256(salt + ":" + nonce) comece com `bits` bits zerados e
devolve tudo junto. Cada desafio só vale uma vez e por poucos minutos.
"""
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import OrderedDict, deque

VALIDADE_S = 600


def _assinar(chave, salt, bits, expira):
    return hmac.new(chave.encode(), f"{salt}|{bits}|{expira}".encode(), hashlib.sha256).hexdigest()


def criar_desafio(chave, bits):
    salt = secrets.token_hex(16)
    expira = int(time.time()) + VALIDADE_S
    return {"salt": salt, "bits": bits, "expira": expira, "assinatura": _assinar(chave, salt, bits, expira)}


def bits_zerados(digest: bytes):
    n = 0
    for b in digest:
        if b == 0:
            n += 8
            continue
        return n + 8 - b.bit_length()
    return n


def verificar(chave, bits_min, sol, queimar):
    """sol = desafio + nonce. `queimar(salt)` registra o uso e devolve False se repetido."""
    if not isinstance(sol, dict):
        return False
    try:
        salt, bits, expira, nonce = str(sol["salt"]), int(sol["bits"]), int(sol["expira"]), str(sol["nonce"])
        assinatura = str(sol["assinatura"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if bits < bits_min or expira < time.time() or len(nonce) > 32 or len(salt) > 64:
        return False
    if not hmac.compare_digest(assinatura, _assinar(chave, salt, bits, expira)):
        return False
    if bits_zerados(hashlib.sha256(f"{salt}:{nonce}".encode()).digest()) < bits:
        return False
    return queimar(salt)


# ---------- IP do visitante ----------
def redes(texto):
    """'172.16.0.0/12, 127.0.0.1' -> lista de redes."""
    return [ipaddress.ip_network(p.strip(), strict=False) for p in (texto or "").split(",") if p.strip()]


def _ip(texto):
    try:
        ip = ipaddress.ip_address(texto.strip().strip("[]"))
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if ip.version == 6 else ip


def segredo_confere(enviado, esperado):
    """O proxy prova que é ele mesmo enviando um segredo que só ele e o app conhecem."""
    if not esperado:  # só sem TRUST_PROXY (Config.validar exige o segredo com ele)
        return True
    return isinstance(enviado, str) and hmac.compare_digest(enviado.encode(), esperado.encode())


def na_lista(ip_texto, redes_):
    """O IP (texto) está numa das redes? False se não for um IP."""
    ip = _ip(ip_texto or "")
    return ip is not None and any(ip in r for r in redes_)


def ip_cliente(conexao, x_forwarded_for, proxies_confiaveis, segredo_ok=True):
    """IP real do visitante.

    Só olha o X-Forwarded-For se a conexão veio de um proxy confiável E trouxe o
    segredo do proxy (`segredo_ok`). Nesse caso percorre a lista da DIREITA para a
    esquerda (cada proxy acrescenta no fim) e devolve o primeiro endereço que não é
    de um proxy confiável. O que o cliente escreve no começo do cabeçalho nunca é usado.
    """
    origem = _ip(conexao or "")
    if origem is None:
        return "?"
    confiavel = lambda ip: any(ip in r for r in proxies_confiaveis)  # noqa: E731
    if not segredo_ok or not proxies_confiaveis or not confiavel(origem) or not x_forwarded_for:
        return str(origem)
    for parte in reversed(x_forwarded_for.split(",")):
        ip = _ip(parte)
        if ip is None:
            return str(origem)  # cabeçalho malformado: não confiar nele
        if not confiavel(ip):
            return str(ip)
    return str(origem)


def chave_ip(ip_texto):
    """IPv6 é agrupado por /64: um único usuário costuma ter 2^64 endereços."""
    ip = _ip(ip_texto)
    if ip is None:
        return ip_texto
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def prefixo_admissao(ip_texto):
    """IPv6 agrupado por /48: quanto um mesmo bloco pode criar de chaves /64 novas."""
    ip = _ip(ip_texto)
    if ip is None or ip.version == 4:
        return None
    return str(ipaddress.ip_network(f"{ip}/48", strict=False))


def hash_ip(salt, ip):
    return hashlib.sha256(f"{salt}|{chave_ip(ip)}".encode()).hexdigest()[:32]


def chave_cota(ip_texto):
    """Para DINHEIRO, um bloco IPv6 /48 conta como uma pessoa: um /48 se consegue de graça
    (túneis IPv6, VPS) e teria 65.536 sub-redes /64. IPv4 continua por endereço."""
    ip = _ip(ip_texto)
    if ip is not None and ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/48", strict=False))
    return chave_ip(ip_texto)


def hash_cota(salt, ip):
    """Identifica a "pessoa" nas cotas de sats (resgates, intervalo, token de resgate)."""
    return hashlib.sha256(f"{salt}|cota|{chave_cota(ip)}".encode()).hexdigest()[:32]


class Limitador:
    """Janela deslizante em memória: no máximo `n` eventos por `janela_s` por chave.

    Guarda no máximo `max_chaves` chaves. Quando enche, só apaga chaves cuja
    janela já expirou; se todas ainda estiverem ativas, recusa chaves NOVAS em vez
    de esquecer limites em andamento (encher a tabela não zera o limite de ninguém).
    """

    def __init__(self, n, janela_s, max_chaves=50000):
        self.n, self.janela, self.max_chaves = n, janela_s, max_chaves
        self.eventos = OrderedDict()  # chave -> fila de instantes; ordem = último uso
        self._varrido_em = float("-inf")

    def ativas(self):
        """Quantas chaves ainda têm eventos dentro da janela. Apaga as vencidas, varrendo a
        tabela no máximo uma vez a cada 5 s (a varredura custa O(chaves))."""
        agora = time.monotonic()
        if agora - self._varrido_em >= 5:
            self._varrido_em = agora
            for chave in [k for k, f in self.eventos.items() if not f or f[-1] < agora - self.janela]:
                del self.eventos[chave]
        return len(self.eventos)

    def _limpar_expiradas(self, agora):
        while self.eventos:
            chave, fila = next(iter(self.eventos.items()))
            if fila and fila[-1] >= agora - self.janela:
                break  # a mais antiga ainda está ativa, então todas estão
            del self.eventos[chave]

    def permitir(self, chave):
        agora = time.monotonic()
        fila = self.eventos.pop(chave, None)
        if fila is None:
            if len(self.eventos) >= self.max_chaves:
                self._limpar_expiradas(agora)
            if len(self.eventos) >= self.max_chaves:
                return False  # tabela cheia de limites ativos: não aceita chave nova
            fila = deque()
        while fila and fila[0] < agora - self.janela:
            fila.popleft()
        permitido = len(fila) < self.n
        if permitido:
            fila.append(agora)
        if fila:
            self.eventos[chave] = fila  # vai para o fim (mais recente)
        return permitido
