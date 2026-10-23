"""Regras de recompensa: quem exporta uma imagem pode resgatar alguns sats.

Ordem de um resgate:
1. conferências baratas e sem efeito (endereço, token, prova de trabalho);
2. ir à rede sem segurar nada: confirmar que o usuário Coinos existe ou, se o
   endereço for de fora, pedir a invoice à carteira;
3. ler o saldo agora e fazer a reserva atômica no banco (limites + consumir token +
   gravar "pendente"); pagamentos externos passam por aqui um de cada vez;
4. pagar. Qualquer falha depois de o pedido poder ter chegado à Coinos deixa o
   resgate como "erro": conta nos limites e não é repetido automaticamente.
"""
import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone

from . import antiabuso, lnurl
from .coinos import CoinosErro
from .db import ERRO_DESTINO, Recusa
from .eventos import evento, mascarar_endereco

log = logging.getLogger("recompensas")
COINOS_DOMINIO = "coinos.io"
VERIFICACAO_S = 60          # quanto tempo vale a confirmação de que um usuário Coinos existe
# Respostas 4xx do /send que dizem respeito ao NOSSO acesso (token, limite de uso): problema da
# Coinos/conta, contam para a pausa geral. As demais 4xx são sobre o destinatário.
STATUS_DA_CONTA = {401, 403, 429}

RECUSAS = {
    "limite_usuario": "Você já recebeu o limite de sats de hoje. Volte amanhã!",
    "intervalo": "Calma! Espere alguns segundos entre um resgate e outro.",
    "limite_global": "O limite de recompensas do site para hoje acabou. Volte amanhã!",
    "pote": "O pote está quase vazio. Que tal doar uns sats para ele voltar a pagar?",
    "token": "Esta imagem já foi resgatada ou o tempo para resgatar acabou. Exporte outra imagem.",
}
# a urna troca só as mensagens que falam de imagem ou de "limite do dia"
RECUSAS_URNA = {
    **RECUSAS,
    "limite_usuario": "Os sats do seu voto já foram resgatados. Obrigado por votar!",
    "limite_global": "Os sats da urna de hoje acabaram. Volte amanhã!",
    "token": "O tempo para resgatar os sats do seu voto acabou.",
}


class ResgateErro(Exception):
    pass


def hoje():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Recompensas:
    def __init__(self, cfg, db, coinos, invoice_para=lnurl.invoice_para):
        self.cfg, self.db, self.coinos = cfg, db, coinos
        self.invoice_para = invoice_para
        self._saldo = (0.0, None)              # (quando, valor)
        self._trava_saldo = asyncio.Lock()     # uma consulta de saldo por vez
        self._trava_externos = asyncio.Lock()  # pagamentos externos um de cada vez
        self._status_doacao = OrderedDict()    # hash -> (quando, pago)
        self._enderecos_ok = OrderedDict()     # endereço Coinos -> quando foi confirmado
        self._tarefas = set()                  # pagamentos em andamento (referência forte)
        self._travas_doacao = {}               # hash -> Lock (uma consulta por doação por vez)
        self.ao_saldo = None                   # corrotina(saldo) chamada a cada leitura nova do saldo

    # ---------- pote ----------
    async def saldo(self, max_idade_s=60):
        """Saldo com cache; pedidos simultâneos esperam uma única consulta à Coinos."""
        def recente():
            quando, valor = self._saldo
            return valor is not None and time.time() - quando <= max_idade_s
        if recente():
            return self._saldo[1]
        async with self._trava_saldo:
            if not recente():
                valor = await self.coinos.saldo()
                self._saldo = (time.time(), valor)
                await self._avisar_saldo(valor)
            return self._saldo[1]

    async def saldo_agora(self):
        """Saldo lido agora (sem cache) e o instante da leitura."""
        async with self._trava_saldo:
            antes = time.time()
            valor = await self.coinos.saldo()
            self._saldo = (time.time(), valor)
            await self._avisar_saldo(valor)
            return valor, antes

    async def _avisar_saldo(self, valor):
        """Alerta de pote baixo em segundo plano: o Telegram lento não segura a leitura do saldo."""
        if not self.ao_saldo:
            return

        async def avisar():
            try:
                await self.ao_saldo(valor)
            except Exception:
                log.exception("falha no alerta de pote")
        t = asyncio.create_task(avisar())
        self._tarefas.add(t)
        t.add_done_callback(self._tarefas.discard)

    def esquecer_saldo(self):
        self._saldo = (0.0, None)

    def externos_liberados(self):
        return self.cfg.pagamento_externo and self.db.ler_estado("pausa_externos") is None

    def recompensas_pausadas(self):
        return self.db.ler_estado(f"pausa_recompensas:{hoje()}") is not None

    def meu_dia(self, endereco, ip_hash, tipo="arte", seq=None):
        seq, dia = self.cfg.recompensas_seq if seq is None else seq, hoje()
        n = max(self.db.contar_dia(dia, "endereco", endereco, tipo) if endereco else 0,
                self.db.contar_dia(dia, "ip_hash", ip_hash, tipo))
        return {"feitos": n, "ganho_hoje": sum(seq[:n]), "restante_hoje": sum(seq[n:]),
                "proxima": seq[n] if n < len(seq) else 0}

    def restante_hoje(self, endereco, ip_hash):
        return self.meu_dia(endereco, ip_hash)["restante_hoje"]

    # ---------- tokens ----------
    def emitir_token(self, dono=None, tipo="arte", sats=None):
        """`dono` = hash da cota (antiabuso.hash_cota) de quem exportou: só ele pode resgatar.
        Tokens da urna levam o valor fixo do voto em `sats`."""
        token = secrets.token_urlsafe(18)
        self.db.criar_token(token, dono, tipo, sats)
        return token

    def _regras(self, tipo, token):
        """(sats de cada resgate do dia, limite global do dia, mensagens de recusa) do tipo."""
        if tipo == "urna":
            sats = self.db.sats_do_token(token)
            return [sats] if sats else [], self.cfg.urna_limite_dia_global, RECUSAS_URNA
        return self.cfg.recompensas_seq, self.cfg.limite_dia_global, RECUSAS

    # ---------- resgate ----------
    async def _confirmar_coinos(self, endereco):
        """Usuário Coinos inexistente nunca chega ao /send (onde um erro queimaria orçamento)."""
        visto = self._enderecos_ok.get(endereco)
        if visto and time.time() - visto < VERIFICACAO_S:  # curto: conta renomeada/apagada não passa
            return
        try:
            await lnurl.verificar_endereco(endereco)
        except lnurl.LnurlErro:
            raise
        except Exception as e:
            raise lnurl.LnurlErro("Não consegui confirmar esse endereço agora. Tente de novo.") from e
        self._enderecos_ok[endereco] = time.time()
        self._enderecos_ok.move_to_end(endereco)
        while len(self._enderecos_ok) > 5000:
            self._enderecos_ok.popitem(last=False)

    async def resgatar(self, token, endereco, ip_hash, prova, antes_da_rede=None, tipo="arte"):
        """`ip_hash` = hash da cota (antiabuso.hash_cota). `antes_da_rede()` é chamado depois das
        conferências baratas (token, prova de trabalho, cota) e antes de qualquer acesso à rede:
        é onde entram limites globais, que assim só contam pedidos com token e prova válidos."""
        try:
            res = await self._resgatar(token, endereco, ip_hash, prova, antes_da_rede, tipo)
        except (ResgateErro, lnurl.LnurlErro) as e:
            evento("resgate_recusado", endereco=mascarar_endereco(endereco if isinstance(endereco, str) else None),
                   ip=str(ip_hash)[:12], motivo=str(e))
            raise
        except asyncio.CancelledError:
            evento("resgate_cancelado", "warning", endereco=mascarar_endereco(endereco), ip=str(ip_hash)[:12])
            raise
        evento("resgate_pago", endereco=mascarar_endereco(endereco), ip=str(ip_hash)[:12],
               sats=res["sats"], taxa=res["taxa"], ganho_hoje=res["ganho_hoje"], tipo=tipo)
        return res

    def endereco_do_pote(self, endereco):
        try:
            return bool(self.cfg.doacao_endereco) and endereco == lnurl.normalizar(self.cfg.doacao_endereco)
        except lnurl.LnurlErro:
            return False

    def endereco_bloqueado(self, endereco):
        return self.db.ler_estado(f"bloqueio:{hoje()}:{endereco}") is not None

    async def _resgatar(self, token, endereco, ip_hash, prova, antes_da_rede=None, tipo="arte"):
        cfg = self.cfg
        endereco = lnurl.normalizar(endereco)  # LnurlErro se inválido
        usuario, dominio = endereco.split("@")
        if self.endereco_do_pote(endereco):
            raise ResgateErro("Esse é o endereço do próprio pote. Use o seu endereço Lightning.")
        externo = dominio != COINOS_DOMINIO
        evento("resgate_tentativa", endereco=mascarar_endereco(endereco), ip=str(ip_hash)[:12], externo=externo)
        if self.recompensas_pausadas():
            raise ResgateErro("As recompensas estão pausadas hoje para conferência. Volte amanhã!")
        if externo and not self.externos_liberados():
            raise ResgateErro("Por enquanto só pagamos para endereços @coinos.io. "
                              "Crie o seu grátis em coinos.io e use aqui.")
        if self.endereco_bloqueado(endereco):
            raise ResgateErro("A Coinos recusou pagamentos para esse endereço hoje. Confira o endereço "
                              "ou tente amanhã.")
        if not isinstance(token, str) or len(token) > 64 or \
                not self.db.token_valido(token, cfg.token_validade_s, ip_hash, tipo):
            raise ResgateErro((RECUSAS_URNA if tipo == "urna" else RECUSAS)["token"])
        if not antiabuso.verificar(cfg.hmac_key, cfg.pow_bits, prova, self.db.queimar_salt):
            raise ResgateErro("A verificação anti-robô falhou. Tente de novo.")

        regras = self._regras(tipo, token)
        previa = self.meu_dia(endereco, ip_hash, tipo, regras[0])  # só para não ir à rede à toa
        if not previa["proxima"]:
            raise ResgateErro(regras[2]["limite_usuario"])
        if antes_da_rede:
            antes_da_rede()

        # rede antes da reserva: carteira lenta, inexistente ou malformada não consome nada
        bolt11 = None
        try:
            if externo:
                bolt11 = await self.invoice_para(endereco, previa["proxima"], taxa_max_sats=cfg.taxa_max)
            else:
                await self._confirmar_coinos(endereco)
        except lnurl.LnurlErro:
            raise
        except Exception as e:
            log.warning("LNURL %s: %s", dominio, type(e).__name__)
            raise lnurl.LnurlErro("Não consegui falar com a carteira desse endereço.") from e

        # Daqui em diante roda numa tarefa própria: se o pedido HTTP for cancelado (cliente
        # fechou a aba, timeout), a reserva, o pagamento e o fechamento terminam mesmo assim.
        trabalho = asyncio.create_task(self._concluir(token, endereco, usuario, ip_hash, bolt11, previa,
                                                      tipo, regras))
        self._tarefas.add(trabalho)
        trabalho.add_done_callback(self._tarefas.discard)
        return await asyncio.shield(trabalho)

    async def _concluir(self, token, endereco, usuario, ip_hash, bolt11, previa, tipo="arte", regras=None):
        regras = regras or self._regras(tipo, token)
        if bolt11 is None:
            return await self._reservar_e_pagar(token, endereco, usuario, ip_hash, None, previa, tipo, regras)
        async with self._trava_externos:  # um por vez: a pausa por taxa vale já para o próximo
            dono = f"{id(self)}:{token}"
            # a vez também é disputada no banco, para valer entre processos/containers
            if not self.cfg.pagamento_externo or not self.db.adquirir_vez_externo(dono):
                raise ResgateErro("Pagamentos para carteiras de fora estão indisponíveis agora. "
                                  "Use um endereço @coinos.io.")
            try:
                return await self._reservar_e_pagar(token, endereco, usuario, ip_hash, bolt11, previa,
                                                    tipo, regras)
            finally:
                self.db.liberar_vez_externo(dono)

    async def _reservar_e_pagar(self, token, endereco, usuario, ip_hash, bolt11, previa, tipo="arte",
                                regras=None):
        cfg, externo = self.cfg, bolt11 is not None
        seq, limite_global, recusas = regras or self._regras(tipo, token)
        try:
            saldo, saldo_em = await self.saldo_agora()  # lido logo antes de reservar
        except CoinosErro as e:
            raise ResgateErro("Não consegui consultar o pote agora. Tente daqui a pouco.") from e
        try:
            rid, sats = self.db.reservar_resgate(
                token=token, validade_s=cfg.token_validade_s, dia=hoje(), endereco=endereco,
                ip_hash=ip_hash, seq=seq, limite_global=limite_global,
                taxa_max=cfg.taxa_max, intervalo_min_s=cfg.intervalo_min_s, saldo=saldo,
                saldo_em=saldo_em, reserva_pote=cfg.reserva_pote, tipo=tipo)
        except Recusa as e:
            evento("reserva_recusada", motivo=e.codigo, ip=str(ip_hash)[:12], tipo=tipo)
            raise ResgateErro(recusas[e.codigo]) from e
        evento("reserva_feita", resgate=rid, sats=sats, externo=externo, saldo_lido=saldo, tipo=tipo)
        if externo and sats != previa["proxima"]:  # outro resgate entrou no meio
            self.db.liberar_resgate(rid, token, "valor mudou durante o resgate")
            raise ResgateErro("Tente de novo, por favor.")

        try:
            if externo:
                res = await self.coinos.pagar_bolt11(bolt11)
            else:
                res = await self.coinos.enviar_interno(usuario, sats)
        except CoinosErro as e:
            if not e.ambiguo:  # nem chegou a conectar: nada saiu da conta
                self.db.liberar_resgate(rid, token, str(e))
                evento("resgate_liberado", "warning", resgate=rid, motivo=str(e))
                raise ResgateErro("A Coinos está indisponível agora. Tente de novo em instantes.") from e
            destino = (not externo and e.status is not None and 400 <= e.status < 500
                       and e.status not in STATUS_DA_CONTA)
            self._registrar_erro(rid, str(e), externo, endereco if destino else None)
            raise ResgateErro("Não deu para confirmar o pagamento. Confira sua carteira "
                              "em alguns minutos.") from e
        except Exception as e:  # inesperado depois de enviar: tratar como ambíguo
            log.exception("resgate %s: erro inesperado no pagamento", rid)
            self._registrar_erro(rid, type(e).__name__, externo)
            raise ResgateErro("Não deu para confirmar o pagamento. Confira sua carteira "
                              "em alguns minutos.") from e
        except BaseException:  # cancelado (ex.: servidor desligando) no meio do pagamento
            self._registrar_erro(rid, "pagamento interrompido", externo)
            raise
        finally:
            self.esquecer_saldo()

        taxa = res["fee"]
        self.db.fechar_resgate(rid, "pago", ref=res["ref"], fee=taxa or 0)
        evento("pagamento_confirmado", resgate=rid, sats=sats, taxa=taxa, externo=externo, ref=res["ref"][:16])
        if externo and (taxa is None or taxa > cfg.taxa_max):
            self.db.gravar_estado("pausa_externos", f"taxa {taxa!r} no resgate {rid}")
            evento("pausa_externos", "error", resgate=rid, taxa=taxa, taxa_max=cfg.taxa_max,
                   motivo="taxa acima do máximo ou não informada")
        return {"sats": sats, "taxa": taxa or 0, **self.meu_dia(endereco, ip_hash, tipo, seq)}

    def _registrar_erro(self, rid, motivo, externo=False, destino=None):
        """O resgate fica 'erro' (token gasto, conta nos limites, conferir na Coinos).

        `destino`: a Coinos recusou por causa do destinatário (4xx que não é sobre a nossa conta).
        Quem controla o destinatário pode provocar isso de propósito, então só aquele endereço
        fica bloqueado no dia; a pausa geral só vem se isso acontecer demais."""
        dia = hoje()
        if destino:
            self.db.fechar_resgate(rid, "erro", erro=ERRO_DESTINO + motivo)
            self.db.gravar_estado(f"bloqueio:{dia}:{destino}", f"recusa da Coinos no resgate {rid}")
            evento("pagamento_recusado_destino", "error", resgate=rid, motivo=motivo,
                   endereco=mascarar_endereco(destino), acao="conferir na Coinos")
            if self.db.recusas_destino_dia(dia) >= 3 * self.cfg.max_erros_dia:
                self.db.gravar_estado(f"pausa_recompensas:{dia}", "recusas de destinatário demais")
                evento("pausa_recompensas", "error", dia=dia, motivo="recusas de destinatário demais")
            return
        self.db.fechar_resgate(rid, "erro", erro=motivo)
        evento("pagamento_sem_confirmacao", "error", resgate=rid, motivo=motivo, externo=externo,
               acao="conferir na Coinos")
        if externo:
            # quem controla a carteira externa pode provocar o erro: pausa só os externos,
            # nunca as recompensas de todo mundo
            self.db.gravar_estado("pausa_externos", f"pagamento externo sem confirmação (resgate {rid})")
            evento("pausa_externos", "error", resgate=rid, motivo="pagamento externo sem confirmação")
            return
        if self.db.erros_dia(dia) >= self.cfg.max_erros_dia:
            self.db.gravar_estado(f"pausa_recompensas:{dia}", "erros de pagamento demais")
            evento("pausa_recompensas", "error", dia=dia, erros=self.db.erros_dia(dia))

    # ---------- doações ----------
    async def criar_doacao(self, sats):
        inv = await self.coinos.criar_invoice(sats, memo="Doação para o pote de recompensas")
        self.db.nova_doacao(inv["hash"], sats)
        return inv

    async def doacao_paga(self, hash_, cache_s=4):
        estado = self.db.estado_doacao(hash_)
        if estado is None:
            raise ResgateErro("Doação não encontrada.")
        if estado[0]:
            return True  # já paga: não consulta a Coinos de novo
        visto = self._status_doacao.get(hash_)
        if visto and time.monotonic() - visto[0] < cache_s:
            return visto[1]
        trava = self._travas_doacao.setdefault(hash_, asyncio.Lock())
        try:
            async with trava:
                visto = self._status_doacao.get(hash_)  # outro pedido pode ter acabado de consultar
                if visto and time.monotonic() - visto[0] < cache_s:
                    return visto[1]
                pago = await self.coinos.invoice_paga(hash_)
        finally:
            if len(self._travas_doacao) > 2000:  # não deixa crescer: mantém só as em uso
                self._travas_doacao = {h: t for h, t in self._travas_doacao.items() if t.locked()}
        agora = time.monotonic()
        self._status_doacao[hash_] = (agora, pago)
        self._status_doacao.move_to_end(hash_)
        while len(self._status_doacao) > 1000:
            self._status_doacao.popitem(last=False)
        if pago:
            self.db.marcar_doacao_paga(hash_)
            self.esquecer_saldo()
            evento("doacao_paga", doacao=hash_[:40])
        return pago
