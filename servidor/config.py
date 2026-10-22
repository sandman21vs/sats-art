"""Configuração lida das variáveis de ambiente (veja .env.example)."""
import os
import secrets
from dataclasses import dataclass, field


def _int(nome, padrao):
    return int(os.environ.get(nome) or padrao)


def _bool(nome, padrao="0"):
    return (os.environ.get(nome) or padrao).strip().lower() in ("1", "true", "yes", "sim")


@dataclass
class Config:
    coinos_token: str = field(default_factory=lambda: os.environ.get("COINOS_TOKEN", "").strip())
    coinos_url: str = field(default_factory=lambda: os.environ.get("COINOS_URL", "https://coinos.io/api"))
    public_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_URL", "").rstrip("/"))
    doacao_endereco: str = field(default_factory=lambda: os.environ.get("DONATION_LN_ADDRESS", "").strip())

    # sats de cada resgate do dia, em ordem: 1ª imagem, 2ª imagem... (a soma é o limite diário)
    recompensas_seq: list = field(default_factory=lambda: [
        int(x) for x in (os.environ.get("REWARD_SCHEDULE") or "10,11").split(",") if x.strip()])
    limite_dia_global: int = field(default_factory=lambda: _int("DAILY_GLOBAL_SATS", 1000))
    reserva_pote: int = field(default_factory=lambda: _int("POT_RESERVE_SATS", 500))
    taxa_max: int = field(default_factory=lambda: _int("MAX_FEE_SATS", 2))
    intervalo_min_s: int = field(default_factory=lambda: _int("MIN_CLAIM_INTERVAL_S", 20))
    token_validade_s: int = field(default_factory=lambda: _int("CLAIM_TOKEN_TTL_S", 1800))
    # Pagar endereços fora da Coinos (via LNURL). Desligado por padrão: a API da
    # Coinos não aceita teto de taxa, então uma invoice com rota cara poderia
    # custar ao pote muito mais que a recompensa.
    pagamento_externo: bool = field(default_factory=lambda: _bool("ALLOW_EXTERNAL_PAYOUTS"))
    # acima disso, num mesmo dia, pagamentos sem confirmação pausam as recompensas até o dia seguinte
    max_erros_dia: int = field(default_factory=lambda: _int("MAX_PAYOUT_ERRORS_PER_DAY", 5))
    # dias que os resgates concluídos (endereço + hash do IP) ficam guardados
    retencao_dias: int = field(default_factory=lambda: _int("RETENTION_DAYS", 30))

    max_export: int = field(default_factory=lambda: _int("MAX_EXPORT_WIDTH", 3840))
    preview_w: int = field(default_factory=lambda: _int("PREVIEW_WIDTH", 1280))
    render_workers: int = field(default_factory=lambda: _int("RENDER_WORKERS", 1))
    max_fila: int = field(default_factory=lambda: _int("MAX_RENDER_QUEUE", 8))
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 8))
    exports_por_10min: int = field(default_factory=lambda: _int("EXPORTS_PER_10MIN", 10))

    pow_bits: int = field(default_factory=lambda: _int("POW_BITS", 19))
    hmac_key: str = field(default_factory=lambda: os.environ.get("POW_HMAC_KEY") or secrets.token_hex(32))
    ip_salt: str = field(default_factory=lambda: os.environ.get("IP_SALT") or secrets.token_hex(16))
    trust_proxy: bool = field(default_factory=lambda: _bool("TRUST_PROXY"))
    # de onde o proxy reverso se conecta ao container (rede padrão do Docker + localhost)
    proxies_confiaveis: str = field(default_factory=lambda: os.environ.get(
        "TRUSTED_PROXIES", "127.0.0.0/8,::1/128,172.16.0.0/12"))
    # segredo que o proxy manda no cabeçalho X-Proxy-Secret; sem ele o X-Forwarded-For é ignorado
    proxy_secret: str = field(default_factory=lambda: os.environ.get("PROXY_SECRET", "").strip())

    # página: link do repositório e programa de bug bounty
    repo_url: str = field(default_factory=lambda: os.environ.get("REPO_URL", "").strip())
    bounty_email: str = field(default_factory=lambda: os.environ.get(
        "BOUNTY_EMAIL", "sandmann@sandlabs.store").strip())
    bounty_percentual: int = field(default_factory=lambda: _int("BOUNTY_PERCENT", 70))
    # botão "Reportar problema": os relatos chegam por um bot do Telegram (vazio = botão escondido)
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    telegram_chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    reports_por_hora: int = field(default_factory=lambda: _int("REPORTS_PER_HOUR", 30))
    # aviso no Telegram quando o pote cai abaixo disso (0 = desligado) e intervalo mínimo entre avisos
    alerta_pote_sats: int = field(default_factory=lambda: _int("ALERTA_POTE_SATS", 500))
    alerta_pote_horas: int = field(default_factory=lambda: _int("ALERTA_POTE_HORAS", 6))
    # painel /admin: sem token, a rota responde 404
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", "").strip())

    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR", "/data"))
    # modo local (app.py no seu computador): só nele as exportações são gravadas em disco
    modo_local: bool = field(default_factory=lambda: _bool("LOCAL_MODE"))
    salvar_exports_em: str = field(default_factory=lambda: os.environ.get("SAVE_EXPORTS_TO", ""))

    @property
    def limite_dia_usuario(self):
        return sum(self.recompensas_seq)

    @property
    def recompensas_ativas(self):
        return bool(self.coinos_token)

    @property
    def reports_ativos(self):
        return bool(self.telegram_token and self.telegram_chat_id)

    def validar(self):
        """Recusa iniciar com configuração insegura (a maioria das regras vale com recompensas)."""
        if self.trust_proxy and len(self.proxy_secret) < 32:
            raise RuntimeError(
                "TRUST_PROXY=1 exige PROXY_SECRET (32+ caracteres) no .env, repassado pelo proxy "
                "no cabeçalho X-Proxy-Secret. Sem ele, qualquer conexão da rede do Docker poderia "
                "escolher o próprio IP. Veja o README.")
        if self.admin_token and len(self.admin_token) < 12:
            raise RuntimeError("ADMIN_TOKEN precisa de 12+ caracteres. Gere com: openssl rand -hex 24")
        if not self.recompensas_ativas:
            return
        faltando = [n for n in ("POW_HMAC_KEY", "IP_SALT") if len(os.environ.get(n, "")) < 32]
        if faltando:
            raise RuntimeError(
                f"Recompensas ligadas exigem {', '.join(faltando)} fixos (32+ caracteres) no .env. "
                "Gere com: openssl rand -hex 32")
        if not self.recompensas_seq or min(self.recompensas_seq) <= 0:
            raise RuntimeError("REWARD_SCHEDULE precisa de valores positivos, ex.: 10,11")
        if self.modo_local:
            raise RuntimeError("LOCAL_MODE é só para o app no computador, sem recompensas.")
