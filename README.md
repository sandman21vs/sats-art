# sats-art · Rede de Nós

Gerador de arte: qualquer símbolo vira o vazio no meio de uma rede orgânica de nós de luz. A ideia veio do logo da Monero em rabisco.

No servidor público vira um ecossistema de sats que se sustenta sozinho:

- **Quem cria ganha:** 10 sats na 1ª arte do dia e 11 na 2ª (21 por dia), enviados na hora para o endereço Lightning da pessoa.
- **Quem gosta compartilha:** o botão de compartilhar envia a imagem com um convite para o site.
- **Quem pode doa:** o pote recebe doações e paga os próximos criadores.

Os pagamentos passam por uma conta [Coinos](https://coinos.io) dedicada.

## Instalar no servidor (Docker)

1. **Conta Coinos só para o pote.** Crie uma conta nova em coinos.io e guarde nela só o que aceita perder.
2. **Token da API.** Em coinos.io, nas configurações da conta, copie o token de autenticação (auth token).
3. **Configuração:**
   ```bash
   cp .env.example .env
   ```
   Preencha:
   - `COINOS_TOKEN`: o token do passo 2.
   - `DONATION_LN_ADDRESS`: o endereço Lightning da conta, por exemplo `seuusuario@coinos.io`.
   - `PUBLIC_URL`: o endereço público do site.
   - `POW_HMAC_KEY`, `IP_SALT` e `PROXY_SECRET`: gere cada um com `openssl rand -hex 32`. São obrigatórios com recompensas ligadas: sem eles o servidor não inicia.
4. **Suba o serviço:**
   ```bash
   mkdir -p data && sudo chown 1000:1000 data   # só no Linux: o container roda como usuário 1000
   docker compose up -d --build
   ```
   O app fica em `127.0.0.1:8000`. Aponte o seu proxy (Caddy, Traefik ou Nginx) para ele com HTTPS. Compartilhar o arquivo pelo celular só funciona em HTTPS.

   Exemplo com Caddy. Ele substitui o `X-Forwarded-For` do visitante pelo IP real e manda o `PROXY_SECRET` (troque `COLE_O_PROXY_SECRET` pelo valor do `.env`):
   ```
   {
       servers {
           timeouts {
               read_header 10s
               read_body 30s
               idle 2m
           }
       }
   }

   arte.seudominio.com {
       request_body {
           max_size 8MB
       }
       reverse_proxy 127.0.0.1:8000 {
           # o Caddy recebe o corpo inteiro antes de repassar: envios lentos ficam nele, não no app
           request_buffers 8MB
           header_up X-Proxy-Secret COLE_O_PROXY_SECRET
       }
   }
   ```
   **Segurança do IP:**
   - mantenha a porta presa em `127.0.0.1`, como no `docker-compose.yml`;
   - o app só lê o `X-Forwarded-For` quando a conexão vem de `TRUSTED_PROXIES` **e** traz o `X-Proxy-Secret` certo, e usa o valor mais à direita, que é o que o proxy acrescenta;
   - mesmo que a porta fique exposta por engano, ninguém consegue falsificar o IP sem o segredo;
   - se o proxy roda em outra máquina, coloque o IP dela em `TRUSTED_PROXIES`;
   - rode sempre **um único processo** (o Dockerfile já faz isso).

   **Com Cloudflare Tunnel (cloudflared) em vez de Caddy:** o Cloudflare manda o IP do visitante no `X-Forwarded-For`, mas não manda o `X-Proxy-Secret`. Sem ele, o app enxerga todo mundo com o IP do cloudflared: o primeiro visitante que sacar esgota a cota de sats do dia **para o site inteiro**, e os limites de prévia e exportação também passam a valer para todos juntos. Para resolver:
   1. no servidor, copie o segredo: `grep PROXY_SECRET .env`;
   2. no painel do Cloudflare, abra o domínio e vá em **Rules → Transform Rules → Modify Request Header → Create rule**;
   3. filtro: **Hostname** *equals* o endereço do site;
   4. ação: **Set static**, cabeçalho `X-Proxy-Secret`, valor = o `PROXY_SECRET` (o *Set static* sobrescreve o que o visitante mandar, então ninguém consegue forjar);
   5. salve. Vale na hora, sem reiniciar o container.

   O IP de onde o cloudflared se conecta ao app precisa estar em `TRUSTED_PROXIES` (o padrão cobre `127.0.0.0/8` e as redes Docker `172.16.0.0/12`). Se ele estiver em outra rede (ex.: `192.168.x.x`), acrescente a faixa no `.env` e rode `docker compose up -d`.

   **Como conferir:** se o proxy estiver mal configurado, o log mostra a cada 10 minutos um evento de erro `proxy_sem_segredo` (falta o cabeçalho) ou `proxy_fora_da_lista` (falta a rede em `TRUSTED_PROXIES`); `segredo_proxy_invalido` indica segredo errado. Depois de configurar, os pedidos devem aparecer com hashes de IP diferentes:
   ```bash
   docker compose logs --since 10m | grep '"evento": "pedido"' | grep -o '"ip": "[^"]*"' | sort | uniq -c | sort -rn | head
   ```
5. **Coloque sats no pote:** mande alguns sats para o endereço de doação. As recompensas param sozinhas quando o pote chega em `POT_RESERVE_SATS`.

Os dados (resgates, doações e controle de abuso) ficam em `./data/app.db`. Para atualizar: `git pull && docker compose up -d --build`.

### Como as recompensas funcionam

- **Resgate:** cada exportação gera um token de uso único, válido por 30 minutos. Para resgatar, a pessoa informa o endereço Lightning e o navegador resolve uma prova de trabalho anti-robô de alguns segundos.
- **Limites:**
  - `REWARD_SCHEDULE` define quanto vale cada arte do dia, em ordem (padrão `10,11`); a soma é o máximo por pessoa por dia, contado por endereço **e** por IP (IPv4 por endereço; IPv6 por bloco /48, que uma pessoa consegue de graça);
  - o token de resgate só vale no mesmo IP (ou bloco /48) de quem exportou, e o site só aceita pedidos vindos da própria página: outro site não consegue usar o navegador dos visitantes dele para resgatar aqui;
  - `DAILY_GLOBAL_SATS` por dia no site todo, incluindo taxas;
  - uma reserva mínima no pote;
  - um intervalo mínimo entre resgates do mesmo IP.
- **Pagamento:** por padrão só endereços `@coinos.io` recebem, por transferência interna e sem taxa. A API da Coinos não aceita teto de taxa, então pagar carteiras de fora pode custar caro ao pote.
- **Carteiras de fora (opcional):** `ALLOW_EXTERNAL_PAYOUTS=1` libera pagamentos via LNURL. O servidor:
  - só fala com domínios públicos em HTTPS na porta 443, conectando no IP já verificado;
  - confere checksum, rede, valor, validade e a taxa das rotas privadas da invoice;
  - **pausa sozinho** os pagamentos externos se alguma taxa passar de `MAX_FEE_SATS`. Para liberar de novo, apague a linha `pausa_externos` da tabela `estado` em `data/app.db`.
- **Sem pagamento duplicado:** os limites são conferidos e o resgate é gravado numa única transação antes de pagar. Qualquer falha depois de o pedido poder ter chegado à Coinos fica como `erro`: conta nos limites e não é repetida automaticamente.
- **Endereço conferido antes:** um usuário `@coinos.io` só recebe se o endereço existir de verdade (verificado no LNURL público da Coinos), então endereços inventados não gastam o orçamento do dia.
- **Pausa de segurança:** se `MAX_PAYOUT_ERRORS_PER_DAY` pagamentos ficarem sem confirmação no mesmo dia (timeout, erro da Coinos), as recompensas pausam até o dia seguinte. Se a Coinos recusar o pagamento por causa do destinatário (ex.: usuário que mudou de nome), só aquele endereço fica bloqueado no dia; a pausa geral só vem com 3x esse número de recusas. O endereço do próprio pote não pode receber. Confira os resgates com status `erro` em `data/app.db` contra o extrato da Coinos.
- **Privacidade:** resgates concluídos (endereço Lightning e hash do IP) são apagados depois de `RETENTION_DAYS` dias. Os arquivos de log também: um por dia, apagados depois de `RETENTION_DAYS`.

Sem `COINOS_TOKEN`, o app roda só como gerador, sem recompensas e sem doações.

### Bug bounty

A página tem um botão **🐞 Bug bounty** e o arquivo [SECURITY.md](SECURITY.md) traz as regras: quem conseguir esvaziar o pote fica com `BOUNTY_PERCENT` (padrão 70%) do que pegou e relata a falha para `BOUNTY_EMAIL`. O ícone do GitHub no topo aponta para `REPO_URL`. Enquanto ela estiver vazia, o ícone aparece como "em breve".

### Registro de eventos

Tudo que acontece no sistema vira uma linha JSON, na saída padrão (`docker compose logs`, limitada a 5 x 20 MB pelo `docker-compose.yml`) e em `data/logs/eventos.jsonl`, com rotação diária e a cada 20 MB; arquivos com mais de `RETENTION_DAYS` dias são apagados. Os eventos de cada pedido têm teto de 10 linhas por segundo (os descartados são contados no campo `suprimidos`), para um flood não encher o disco.
- **O que entra:**
  - cada pedido: rota, status, tempo, hash do IP e navegador (com teto de linhas por segundo);
  - cada recusa e o motivo (limite, validação, anti-robô);
  - cada etapa do resgate: tentativa, reserva, pagamento confirmado, sem confirmação, liberado;
  - doações criadas e pagas;
  - pausas automáticas, fila cheia e exportações;
  - limpezas e pendentes órfãos;
  - sinais de ataque: segredo do proxy errado, LNURL apontando para rede interna;
  - erros internos com traceback;
  - início e fim do servidor.
- **O que nunca entra:** IP cru (só hash salgado, o mesmo do banco), endereço Lightning completo (fica mascarado, ex.: `an***@coinos.io`), tokens, chaves, segredos e corpos de resposta da Coinos.

Exemplos:
```bash
docker compose logs -f | grep '"nivel": "error"'          # só o que precisa de atenção
grep '"evento": "resgate_' data/logs/eventos.jsonl        # todos os resgates
grep '"evento": "pedido_recusado"' data/logs/eventos.jsonl | tail
```

## Usar no seu computador

Dê dois cliques em **`Abrir app.command`**. Na primeira vez ele instala o que precisa, depois abre o app no navegador. No modo local não há recompensas, a exportação vai até 8K e os arquivos são salvos também em `output/`.

- **Símbolo:** escolha o Sat ou arraste uma imagem, cole um print (⌘V) ou clique para escolher. O app detecta a forma. Se vier errada, ajuste o **limiar** ou marque **inverter**.
- **Formato:** deitada (16:9, para computador) ou em pé (9:16, para tela de celular). No celular em pé, o site já começa no formato em pé.
- **Ajustes:** tamanho, inclinação, zoom, círculo ligado/desligado, semente, densidade, halo, brilho, cor e fundo. A prévia é a imagem final em tamanho menor.

Pelo Terminal: `.venv/bin/python app.py`.

## Linha de comando (motor direto)

```bash
.venv/bin/python -m motor.rede_nos sat
.venv/bin/python -m motor.rede_nos sat -21 21 7680 2.7 --sem-circulo
.venv/bin/python -m motor.rede_nos sat -21 21 7680 2.7 --retrato   # em pé (4320x7680)
```

Os argumentos são `<simbolo> [angulo] [seed] [lado_px] [px_por_unidade] [--sem-circulo] [--retrato]`; `lado_px` é sempre o lado maior. A imagem vai para `output/`.

Para ter um símbolo fixo novo, crie uma função em `motor/simbolos.py` que devolva a geometria shapely centrada em `(cx, cy)` (o disco tem 300 unidades de raio) e registre a função em `SIMBOLOS`.

## Desenvolvimento

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Estrutura

- `motor/`: a arte.
  - `rede_nos.py`: rede de nós e pintura.
  - `forma_imagem.py`: transforma um print em forma.
  - `simbolos.py`: os presets.
- `servidor/`: o site (FastAPI).
  - `main.py`: rotas.
  - `render.py`: fila de renderização.
  - `recompensas.py`: regras de resgate.
  - `coinos.py`: cliente da API da Coinos.
  - `lnurl.py`: pagamento para endereço Lightning.
  - `antiabuso.py`: prova de trabalho e limites.
  - `db.py`: SQLite.
- `static/index.html`: a interface.
- `servidor/eventos.py`: registro de eventos em JSON.
- `SECURITY.md`: regras do bug bounty.
- `app.py` e `Abrir app.command`: modo local.
- `Dockerfile`, `docker-compose.yml` e `.env.example`: instalação no servidor.
- `tests/`: testes das regras de recompensa, do LNURL e de regressão das auditorias de segurança (cada ataque já corrigido tem um teste que o prova).
- `experimentos/`: versões antigas (rabisco, pincelada).

## Licença

[GPL-3.0](LICENSE). Você pode usar, estudar, modificar e redistribuir, desde que as versões distribuídas continuem sob a GPL-3.0 com o código-fonte disponível.
