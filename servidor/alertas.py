"""Alerta de pote baixo no Telegram: no máximo um a cada ALERTA_POTE_HORAS e um aviso quando o pote volta."""
import time

from .eventos import evento
from .telegram import TelegramErro

NOVA_TENTATIVA_S = 600  # se o Telegram falhar, só tenta de novo depois disso


class AlertaPote:
    def __init__(self, cfg, db, telegram):
        self.cfg, self.db, self.telegram = cfg, db, telegram
        self._tentativa = 0.0

    async def verificar(self, saldo):
        limite = self.cfg.alerta_pote_sats
        if not self.telegram or limite <= 0 or saldo is None:
            return
        agora = time.time()
        baixo = self.db.ler_estado("alerta_pote_baixo") == "1"
        if saldo < limite:
            ultimo = float(self.db.ler_estado("alerta_pote_ultimo") or 0)
            if agora - ultimo < self.cfg.alerta_pote_horas * 3600 or agora - self._tentativa < NOVA_TENTATIVA_S:
                return
            texto = (f"⚠️ Pote baixo · Rede de Nós\nSaldo: {saldo} sats (limite de alerta: {limite}).\n"
                     "Reabasteça a conta da Coinos ou espere as doações.")
            if await self._enviar(texto):
                self.db.gravar_estado("alerta_pote_ultimo", agora)
                self.db.gravar_estado("alerta_pote_baixo", "1")
                evento("alerta_pote_baixo", "warning", saldo=saldo, limite=limite)
            else:
                self._tentativa = agora
        elif baixo and agora - self._tentativa >= NOVA_TENTATIVA_S:
            if await self._enviar(f"✅ Pote reabastecido · Rede de Nós\nSaldo: {saldo} sats."):
                self.db.gravar_estado("alerta_pote_baixo", "0")
                evento("alerta_pote_reabastecido", saldo=saldo)
            else:
                self._tentativa = agora

    async def _enviar(self, texto):
        try:
            await self.telegram.enviar(texto)
            return True
        except TelegramErro as e:
            evento("alerta_pote_falhou", "error", motivo=str(e))
            return False
