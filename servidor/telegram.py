"""Cliente mínimo da API de bots do Telegram: só manda mensagens de texto para um chat.

O token do bot vai no caminho da URL. Por isso nenhum erro daqui carrega a URL nem a
mensagem original do httpx (que traz a URL): só o tipo do erro ou o status HTTP."""
import httpx

LIMITE_TEXTO = 4000  # o Telegram aceita até 4096 caracteres por mensagem


class TelegramErro(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id, transport=None, timeout=10):
        self.chat_id = chat_id
        self._http = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/", timeout=timeout,
                                       transport=transport, follow_redirects=False)

    async def enviar(self, texto):
        """Texto puro (sem parse_mode): nada do que o visitante escreve vira formatação ou link clicável."""
        corpo = {"chat_id": self.chat_id, "text": texto[:LIMITE_TEXTO], "disable_web_page_preview": True}
        try:
            r = await self._http.post("sendMessage", json=corpo)
        except httpx.HTTPError as e:
            raise TelegramErro(type(e).__name__) from None
        if r.status_code != 200:
            raise TelegramErro(f"status {r.status_code}")
        try:
            ok = r.json().get("ok")
        except ValueError:
            ok = False
        if not ok:
            raise TelegramErro("resposta sem ok")

    async def fechar(self):
        await self._http.aclose()
