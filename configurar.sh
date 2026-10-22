#!/usr/bin/env bash
# Configura o .env do sats-art sem abrir o arquivo.
#
# Uso: ./configurar.sh            menu
#      ./configurar.sh ver        mostra a configuração (segredos mascarados)
#      ./configurar.sh mudar NOME muda uma variável
#      ./configurar.sh segredos   gera os segredos que estão vazios
#      ./configurar.sh telegram   liga o botão "Reportar problema"
#      ./configurar.sh admin      liga o painel /admin (gera o ADMIN_TOKEN)
#      ./configurar.sh aplicar    confere e reinicia o site com o .env novo
#
# Antes de mudar, guarda uma cópia em .env.bak-configurar-<data> (ficam as 5 últimas).
set -uo pipefail
cd "$(dirname "$0")" || exit 1

ENV_ARQ=.env
EXEMPLO=.env.example
MUDOU=0
FEZ_BACKUP=0

erro() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ok() { printf '\033[32m%s\033[0m\n' "$*"; }
aviso() { printf '\033[33m%s\033[0m\n' "$*"; }

preparar() {
  if [ ! -f "$EXEMPLO" ]; then
    erro "Não achei o $EXEMPLO. Rode o script dentro da pasta do sats-art."
    exit 1
  fi
  if [ ! -f "$ENV_ARQ" ]; then
    (umask 077 && cp "$EXEMPLO" "$ENV_ARQ")
    ok "Criei o $ENV_ARQ a partir do $EXEMPLO."
  fi
  chmod 600 "$ENV_ARQ"
}

# ---------- leitura ----------
nomes() {
  { grep -oE '^[A-Z][A-Z0-9_]*=' "$EXEMPLO"; grep -oE '^[A-Z][A-Z0-9_]*=' "$ENV_ARQ"; } \
    | sed 's/=$//' | awk '!visto[$0]++'
}
existe() { grep -q "^$1=" "$ENV_ARQ"; }
valor() { awk -v k="$1" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2) } END { print v }' "$ENV_ARQ"; }
padrao() { awk -v k="$1" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$EXEMPLO"; }
no_exemplo() { grep -q "^$1=" "$EXEMPLO"; }

# comentário que fica logo acima da variável no .env.example
comentario() {
  awk -v k="$1" '
    /^# -{3,}/ || /^$/ { buf = ""; next }
    /^#/ { buf = buf "  " substr($0, 3) "\n"; next }
    index($0, k "=") == 1 { printf "%s", buf; exit }
    { buf = "" }' "$EXEMPLO"
}

eh_segredo() { case "$1" in *TOKEN*|*SECRET*|*KEY*|*SALT*) return 0 ;; esac; return 1; }

mostrar_valor() {
  local v; v=$(valor "$1")
  if [ -z "$v" ]; then
    printf '(vazio)'
  elif eh_segredo "$1"; then
    printf '•••••• (%d caracteres)' "${#v}"
  else
    printf '%s' "$v"
  fi
}

ver() {
  local i=0 n extra
  echo
  for n in $(nomes); do
    i=$((i + 1))
    extra=""; no_exemplo "$n" || extra="  (fora do .env.example)"
    printf '%3d  %-26s %s%s\n' "$i" "$n" "$(mostrar_valor "$n")" "$extra"
  done
  echo
}

# ---------- validação ----------
validar() {
  local n=$1 v=$2 re
  case "$v" in
    *[[:space:]]*|*\"*|*\'*|*\#*|*\$*|*\\*|*\`*)
      echo "Sem espaços, aspas, #, \$, \\ ou crase: o Docker leria o valor errado."; return 1 ;;
  esac
  [ -z "$v" ] && return 0
  case "$n" in
    TRUST_PROXY|ALLOW_EXTERNAL_PAYOUTS)
      re='^[01]$'; [[ $v =~ $re ]] || { echo "Use 1 (ligado) ou 0 (desligado)."; return 1; } ;;
    REWARD_SCHEDULE)
      re='^[1-9][0-9]*(,[1-9][0-9]*)*$'
      [[ $v =~ $re ]] || { echo "Números positivos separados por vírgula, ex.: 10,11"; return 1; } ;;
    PUBLIC_URL|REPO_URL)
      re='^https?://[^/]+'; [[ $v =~ $re ]] || { echo "Comece com https://"; return 1; } ;;
    DONATION_LN_ADDRESS|BOUNTY_EMAIL)
      re='^[^@]+@[^@]+\.[^@]+$'; [[ $v =~ $re ]] || { echo "Formato nome@dominio, ex.: seuusuario@coinos.io"; return 1; } ;;
    TRUSTED_PROXIES)
      re='^[0-9a-fA-F:.,/]+$'; [[ $v =~ $re ]] || { echo "Redes separadas por vírgula, ex.: 127.0.0.0/8,172.16.0.0/12"; return 1; } ;;
    TELEGRAM_BOT_TOKEN)
      re='^[0-9]+:[A-Za-z0-9_-]+$'; [[ $v =~ $re ]] || { echo "O token do @BotFather tem o formato 123456:ABC..."; return 1; } ;;
    TELEGRAM_CHAT_ID)
      re='^-?[0-9]+$'; [[ $v =~ $re ]] || { echo "O chat id é um número (grupos começam com -)."; return 1; } ;;
    ADMIN_TOKEN)
      [ "${#v}" -ge 12 ] || { echo "Precisa de 12 caracteres ou mais (digite g para gerar)."; return 1; } ;;
    PROXY_SECRET|POW_HMAC_KEY|IP_SALT)
      [ "${#v}" -ge 32 ] || { echo "Precisa de 32 caracteres ou mais (digite g para gerar)."; return 1; } ;;
    *)
      re='^[0-9]+$'
      if [[ $(padrao "$n") =~ $re ]] && ! [[ $v =~ $re ]]; then
        echo "Esse valor é um número inteiro."; return 1
      fi ;;
  esac
}

# mesmas regras que fazem o servidor recusar iniciar (servidor/config.py)
checar_tudo() {
  local falhas=0 n v
  v=$(valor PROXY_SECRET)
  if [ "$(valor TRUST_PROXY)" = "1" ] && [ "${#v}" -lt 32 ]; then
    erro "TRUST_PROXY=1 exige PROXY_SECRET com 32+ caracteres (./configurar.sh segredos)."; falhas=1
  fi
  if [ -n "$(valor COINOS_TOKEN)" ]; then
    for n in POW_HMAC_KEY IP_SALT; do
      v=$(valor "$n")
      if [ "${#v}" -lt 32 ]; then
        erro "Com COINOS_TOKEN, $n precisa de 32+ caracteres (./configurar.sh segredos)."; falhas=1
      fi
    done
  fi
  if { [ -n "$(valor TELEGRAM_BOT_TOKEN)" ] && [ -z "$(valor TELEGRAM_CHAT_ID)" ]; } ||
     { [ -z "$(valor TELEGRAM_BOT_TOKEN)" ] && [ -n "$(valor TELEGRAM_CHAT_ID)" ]; }; then
    aviso "Aviso: o botão de reportar só aparece com TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID juntos."
  fi
  return $falhas
}

# ---------- escrita ----------
backup() {
  [ "$FEZ_BACKUP" = 1 ] && return
  local arq=".env.bak-configurar-$(date +%Y%m%d-%H%M%S)"
  (umask 077 && cp "$ENV_ARQ" "$arq")
  # guarda só as 5 cópias mais novas feitas por este script
  ls -1t .env.bak-configurar-* 2>/dev/null | tail -n +6 | while read -r velho; do rm -f -- "$velho"; done
  FEZ_BACKUP=1
  echo "Cópia do .env anterior: $arq"
}

gravar() {
  local tmp
  backup
  tmp=$(mktemp "$ENV_ARQ.XXXXXX") || { erro "Não consegui gravar."; return 1; }
  NOVO_VALOR=$2 awk -v k="$1" '
    BEGIN { v = ENVIRON["NOVO_VALOR"] }
    index($0, k "=") == 1 { if (!feito) print k "=" v; feito = 1; next }
    { print }
    END { if (!feito) print k "=" v }' "$ENV_ARQ" > "$tmp" &&
    cat "$tmp" > "$ENV_ARQ"   # cat mantém dono e permissões do .env
  rm -f "$tmp"
  MUDOU=1
}

gerar_segredo() {
  if command -v openssl >/dev/null; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

avisos_ao_mudar() {
  case "$1" in
    PROXY_SECRET) aviso "Lembre de colocar o mesmo valor na regra do Cloudflare (X-Proxy-Secret) ou no Caddy." ;;
    IP_SALT) aviso "Trocar o IP_SALT zera a contagem por pessoa do dia: todo mundo vira 'novo'." ;;
    COINOS_TOKEN) aviso "Com COINOS_TOKEN as recompensas ligam: confira POW_HMAC_KEY e IP_SALT." ;;
  esac
}

mudar() {
  local n=${1:-} entrada novo msg lista
  if [ -z "$n" ]; then
    ver
    read -rp "Número ou nome da variável (Enter volta): " entrada
    [ -z "$entrada" ] && return
    if [[ $entrada =~ ^[0-9]+$ ]]; then
      n=$(nomes | sed -n "${entrada}p")
    else
      n=$(printf '%s' "$entrada" | tr '[:lower:]' '[:upper:]')
    fi
  fi
  if ! [[ $n =~ ^[A-Z][A-Z0-9_]*$ ]]; then erro "Variável inválida."; return 1; fi
  if ! no_exemplo "$n" && ! existe "$n"; then
    read -rp "$n não existe no .env.example. Criar mesmo assim? [s/N] " entrada
    [[ $entrada =~ ^[sS] ]] || return
  fi

  echo; printf '\033[1m%s\033[0m\n' "$n"
  comentario "$n"
  echo "  Atual: $(mostrar_valor "$n")"
  no_exemplo "$n" && [ -n "$(padrao "$n")" ] && ! eh_segredo "$n" && echo "  Padrão: $(padrao "$n")"
  echo "  Enter mantém o valor atual; - deixa vazio."
  case "$n" in PROXY_SECRET|POW_HMAC_KEY|IP_SALT|ADMIN_TOKEN) echo "  g gera um segredo novo." ;; esac

  while true; do
    if eh_segredo "$n"; then
      read -rsp "Novo valor (não aparece na tela): " novo; echo
    else
      read -rp "Novo valor: " novo
    fi
    [ -z "$novo" ] && { echo "Nada mudou."; return; }
    [ "$novo" = "-" ] && novo=""
    case "$n" in PROXY_SECRET|POW_HMAC_KEY|IP_SALT|ADMIN_TOKEN) [ "$novo" = "g" ] && novo=$(gerar_segredo) ;; esac
    if msg=$(validar "$n" "$novo"); then break; fi
    erro "$msg"
  done
  gravar "$n" "$novo" && ok "$n atualizado: $(mostrar_valor "$n")"
  avisos_ao_mudar "$n"
}

segredos() {
  local n gerou=0
  for n in POW_HMAC_KEY IP_SALT PROXY_SECRET; do
    if [ -z "$(valor "$n")" ]; then
      gravar "$n" "$(gerar_segredo)" && ok "$n gerado."
      avisos_ao_mudar "$n"; gerou=1
    else
      echo "$n já existe, mantido (para trocar: ./configurar.sh mudar $n)."
    fi
  done
  [ $gerou = 0 ] && echo "Nenhum segredo vazio."
  return 0
}

# ---------- painel admin ----------
admin() {
  local atual novo r; atual=$(valor ADMIN_TOKEN)
  if [ -n "$atual" ]; then
    read -rp "O painel já está ligado. Gerar um token novo (o antigo deixa de valer)? [s/N] " r
    [[ $r =~ ^[sS] ]] || return
  fi
  novo=$(gerar_segredo)
  gravar ADMIN_TOKEN "$novo" || return 1
  ok "Painel ligado em /admin. Token de acesso (aparece só agora, guarde):"
  echo "  $novo"
}

# ---------- Telegram ----------
python_disponivel() {
  if command -v python3 >/dev/null; then echo "python3"
  elif docker compose exec -T sats-art true >/dev/null 2>&1; then echo "docker compose exec -T sats-art python"
  fi
}

# o token vai num arquivo de config lido pela entrada padrão, não na linha de comando (o ps mostraria)
api_telegram() {
  local token=$1 metodo=$2; shift 2
  printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$token" "$metodo" | curl -s --max-time 15 -K - "$@"
}

listar_chats() {
  local py; py=$(python_disponivel)
  [ -z "$py" ] && { aviso "Sem Python aqui para listar as conversas: digite o chat id."; return; }
  api_telegram "$1" getUpdates | $py -c '
import sys, json
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit("Não consegui falar com o Telegram.")
if not d.get("ok"):
    sys.exit("Telegram recusou: " + str(d.get("description")))
chats = {}
for u in d["result"]:
    for v in u.values():
        if isinstance(v, dict) and "chat" in v:
            c = v["chat"]
            chats[c["id"]] = c["type"] + " · " + (c.get("title") or c.get("first_name") or "")
            if v.get("migrate_to_chat_id"):
                chats[c["id"]] += "  (antigo, não use)"
                chats[v["migrate_to_chat_id"]] = "supergroup · " + (c.get("title") or "")
if chats:
    print("Conversas que o bot conhece:")
    for i, n in chats.items():
        print(f"  {i}   {n}")
else:
    print("O bot ainda não recebeu mensagens: mande /start para ele e tente de novo.")
'
}

telegram() {
  local token chat atual msg codigo
  echo
  echo "1. No Telegram, crie o bot com o @BotFather (/newbot)."
  echo "2. Mande /start para o bot (ou adicione ele num grupo e mande uma mensagem lá)."
  echo
  atual=$(valor TELEGRAM_BOT_TOKEN)
  while true; do
    if [ -n "$atual" ]; then
      read -rsp "Token do bot (Enter mantém o atual): " token; echo
      [ -z "$token" ] && token=$atual
    else
      read -rsp "Token do bot (não aparece na tela): " token; echo
    fi
    [ -z "$token" ] && return
    if msg=$(validar TELEGRAM_BOT_TOKEN "$token"); then break; fi
    erro "$msg"
  done

  listar_chats "$token" || { erro "Nada foi gravado."; return 1; }
  echo "Só você: use a linha 'private'. Grupo: use a 'supergroup' (começa com -100)."
  atual=$(valor TELEGRAM_CHAT_ID)
  while true; do
    read -rp "Chat id que vai receber os relatos${atual:+ (Enter mantém $atual)}: " chat
    [ -z "$chat" ] && chat=$atual
    [ -z "$chat" ] && return
    if msg=$(validar TELEGRAM_CHAT_ID "$chat"); then break; fi
    erro "$msg"
  done

  echo "Mandando uma mensagem de teste…"
  codigo=$(api_telegram "$token" sendMessage -o /dev/null -w '%{http_code}' \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=✅ sats-art conectado: os relatos do site chegam aqui.")
  if [ "$codigo" != "200" ]; then
    case "$codigo" in
      401) erro "Token recusado (401). Confira o token no @BotFather." ;;
      400|403) erro "Não consegui escrever nesse chat ($codigo): mande /start para o bot ou confira o chat id." ;;
      *) erro "O Telegram não respondeu como esperado ($codigo)." ;;
    esac
    erro "Nada foi gravado."
    return 1
  fi
  gravar TELEGRAM_BOT_TOKEN "$token" && gravar TELEGRAM_CHAT_ID "$chat"
  ok "A mensagem de teste chegou? Token e chat id gravados no .env."
}

# ---------- aplicar ----------
aplicar() {
  if ! checar_tudo; then
    erro "Corrija antes de reiniciar: com isso o servidor não inicia."
    return 1
  fi
  if ! command -v docker >/dev/null; then
    aviso "Sem Docker aqui: reinicie o app do seu jeito para ler o .env novo."
    return 0
  fi
  echo "Reiniciando o site…"
  docker compose up -d || return 1
  sleep 5
  docker compose ps
  MUDOU=0
}

perguntar_aplicar() {
  [ "$MUDOU" = 1 ] || return 0
  local r
  read -rp "Reiniciar o site agora para usar o .env novo? [S/n] " r
  if [[ $r =~ ^[nN] ]]; then
    echo "Ok. Quando quiser: ./configurar.sh aplicar"
  else
    aplicar
  fi
}

menu() {
  local op
  while true; do
    echo
    echo "sats-art · configuração (.env)"
    echo "  1) Ver a configuração"
    echo "  2) Mudar uma variável"
    echo "  3) Gerar os segredos que estão vazios"
    echo "  4) Ligar o botão de reportar (Telegram)"
    echo "  5) Aplicar: conferir e reiniciar o site"
    echo "  6) Ligar o painel admin (/admin)"
    echo "  0) Sair"
    read -rp "Escolha: " op || break
    case "$op" in
      1) ver ;;
      2) mudar ;;
      3) segredos ;;
      4) telegram ;;
      5) aplicar ;;
      6) admin ;;
      0|"") break ;;
      *) erro "Opção inválida." ;;
    esac
  done
  perguntar_aplicar
}

preparar
case "${1:-}" in
  "") menu ;;
  ver) ver ;;
  mudar) mudar "${2:-}"; perguntar_aplicar ;;
  segredos) segredos; perguntar_aplicar ;;
  telegram) telegram; perguntar_aplicar ;;
  admin) admin; perguntar_aplicar ;;
  aplicar) aplicar ;;
  -h|--help|ajuda) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) erro "Comando desconhecido: $1"; sed -n '4,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
