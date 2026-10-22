"""Registro de eventos: tudo que acontece no sistema, uma linha JSON por evento.

Vai para a saída padrão (aparece em `docker compose logs`) e para
`<DATA_DIR>/logs/eventos.jsonl`, com rotação por tamanho e por dia; arquivos mais
velhos que a retenção são apagados. Nunca registra segredos: IPs entram como hash
salgado (o mesmo usado no banco), endereços Lightning entram mascarados, e tokens,
chaves e corpos de resposta externos não entram.

Eventos "ruidosos" (um por pedido, que qualquer um gera à vontade) têm teto de
linhas por segundo: um flood não enche o disco; o que for descartado é contado.
"""
import json
import logging
import logging.handlers
import os
import sys
import time
import traceback

_log = logging.getLogger("eventos")
_arquivo = None             # handler do arquivo (para a rotação diária e a retenção)
_nossos = []                # handlers instalados por configurar() (os de outros ficam)
_aberto_em = None           # dia (UTC) em que o arquivo atual começou

RUIDOSOS_POR_S = 10         # teto de linhas por segundo dos eventos ruidosos
_balde = [float(RUIDOSOS_POR_S * 10), time.monotonic()]   # fichas, último reabastecimento
_suprimidos = 0
gancho = None               # função(nome, nivel, campos) chamada a cada evento (painel admin: contadores)


def _permitir_ruidoso():
    global _suprimidos
    agora = time.monotonic()
    fichas, antes = _balde
    fichas = min(RUIDOSOS_POR_S * 10, fichas + (agora - antes) * RUIDOSOS_POR_S)
    _balde[1] = agora
    if fichas >= 1:
        _balde[0] = fichas - 1
        return True
    _balde[0] = fichas
    _suprimidos += 1
    return False


class _FormatoJSON(logging.Formatter):
    def format(self, rec):
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(rec.created)) + f".{int(rec.msecs):03d}Z",
             "nivel": rec.levelname.lower(), "origem": rec.name}
        dados = getattr(rec, "dados", None)
        if dados:
            d.update(dados)
        else:
            d["evento"] = "log"
            d["msg"] = rec.getMessage()
        if rec.exc_info:
            d["erro"] = "".join(traceback.format_exception(*rec.exc_info))[-4000:]
        return json.dumps(d, ensure_ascii=False, default=str)


def configurar(data_dir, max_mb=20, arquivos=10, retencao_dias=30):
    """Todo log do processo (eventos, avisos, erros) sai em JSON na tela e no arquivo."""
    global _arquivo, _aberto_em
    fmt = _FormatoJSON()
    raiz = logging.getLogger()
    for h in _nossos:  # reconfigurar não duplica linhas nem mexe em handlers de terceiros
        raiz.removeHandler(h)
        h.close()
    _nossos.clear()
    tela = logging.StreamHandler(sys.stdout)
    tela.setFormatter(fmt)
    raiz.addHandler(tela)
    _nossos.append(tela)
    try:
        pasta = os.path.join(data_dir, "logs")
        os.makedirs(pasta, exist_ok=True)
        arq = logging.handlers.RotatingFileHandler(os.path.join(pasta, "eventos.jsonl"),
                                                   maxBytes=max_mb * 1024 * 1024,
                                                   backupCount=max(arquivos, retencao_dias + 1),
                                                   encoding="utf-8")
        arq.setFormatter(fmt)
        raiz.addHandler(arq)
        _nossos.append(arq)
        _arquivo, _aberto_em = arq, time.strftime("%Y-%m-%d", time.gmtime())
    except OSError:
        raiz.warning("não consegui abrir o arquivo de log; seguindo só com a saída padrão")
    raiz.setLevel(logging.INFO)
    for nome in ("uvicorn", "uvicorn.error"):
        logging.getLogger(nome).handlers.clear()
        logging.getLogger(nome).propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # o middleware registra cada pedido (sem IP cru)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def evento(nome, nivel="info", ruidoso=False, **campos):
    """Registra um evento. Ex.: evento("resgate_pago", sats=10, taxa=0).

    ruidoso=True: evento que qualquer visitante gera à vontade (um por pedido); passa pelo
    teto de linhas por segundo. Os descartados aparecem em `suprimidos` na próxima linha."""
    global _suprimidos
    if gancho:
        try:
            gancho(nome, nivel, campos)
        except Exception:  # contar nunca pode derrubar o que está sendo registrado
            pass
    if ruidoso:
        if not _permitir_ruidoso():
            return
        if _suprimidos:
            campos["suprimidos"], _suprimidos = _suprimidos, 0
    _log.log(getattr(logging, nivel.upper(), logging.INFO), nome,
             extra={"dados": {"evento": nome, **{k: v for k, v in campos.items() if v is not None}}})


def aplicar_retencao(retencao_dias):
    """Um arquivo por dia (rotaciona na virada UTC) e apaga os que passaram da retenção.
    Chamada pela limpeza periódica. Devolve quantos arquivos apagou."""
    global _aberto_em
    if _arquivo is None:
        return 0
    hoje = time.strftime("%Y-%m-%d", time.gmtime())
    if _aberto_em != hoje:
        _arquivo.doRollover()
        _aberto_em = hoje
    pasta = os.path.dirname(_arquivo.baseFilename)
    limite, apagados = time.time() - max(1, retencao_dias) * 86400, 0
    for nome in os.listdir(pasta):
        caminho = os.path.join(pasta, nome)
        if nome.startswith("eventos.jsonl.") and os.path.getmtime(caminho) < limite:
            os.remove(caminho)
            apagados += 1
    return apagados


def mascarar_endereco(endereco):
    """ana.silva@coinos.io -> an***@coinos.io"""
    if not isinstance(endereco, str) or "@" not in endereco:
        return None
    usuario, dominio = endereco.rsplit("@", 1)
    return f"{usuario[:2]}***@{dominio[:80]}"
