# Segurança e bug bounty

Este projeto é open source e roda **de propósito** com pouco dinheiro no pote. Ataques são bem-vindos: é assim que ele fica seguro.

## A recompensa

**Conseguiu esvaziar o pote? Fique com 70% do que pegou.**

1. Mande um e-mail para **sandmann@sandlabs.store** explicando como fez, passo a passo se puder, para a falha ser corrigida.
2. Devolva os outros 30% para o endereço de doação do pote, o que aparece no botão "Doar" do site, para o projeto continuar pagando outras pessoas.
3. Achou uma falha sem conseguir tirar sats (receber além do limite, travar o servidor, vazar dados, XSS, SSRF…)? Mande também. Ela entra nos créditos do projeto.

O pote sempre tem pouco dinheiro. O valor é simbólico; o objetivo é segurança.

## Escopo

**Vale:**
- a instância oficial do site e o pote dela;
- o código deste repositório (rodando na sua própria máquina, à vontade).

**Não vale (e não conta como bug bounty):**
- atacar a Coinos, carteiras Lightning ou serviços de terceiros;
- atacar outros visitantes ou usar dados deles;
- derrubar a hospedagem ou a rede com tráfego em massa (DDoS volumétrico);
- engenharia social, phishing ou acesso físico.

Se o seu ataque precisar de algo fora do escopo para funcionar, descreva por e-mail em vez de executar.

## Boas práticas para testar

- Para testar à vontade, rode o projeto localmente com uma Coinos falsa. A suíte de testes (`tests/`) tem uma pronta (`CoinosFalsa`).
- O projeto passou por várias rodadas de auditoria de segurança. Cada correção tem um teste de regressão em `tests/` (`test_auditoria_*.py`, `test_reauditoria.py`). Contornar uma correção existente é um ótimo achado.
- Todo acesso ao site é registrado (veja "Registro de eventos" no README). IPs ficam só como hash salgado e endereços Lightning ficam mascarados.

## Contato

**sandmann@sandlabs.store**. Resposta em português ou inglês.
