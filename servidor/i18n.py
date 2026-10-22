"""Tradução das mensagens de erro devolvidas ao visitante ({"erro": "..."}).

O código do servidor continua escrevendo as mensagens em português; na hora de responder,
`traduzir(msg, idioma)` troca pela versão em inglês ou alemão (o idioma vem do cabeçalho
X-Idioma que a página manda). Mensagem sem tradução sai em português.
"""
import re

IDIOMAS = ("pt", "en", "de")

# português -> (inglês, alemão)
MENSAGENS = {
    # servidor/main.py
    "Pedido de outra origem.": ("Request from another origin.", "Anfrage von einer anderen Herkunft."),
    "Pedido inválido.": ("Invalid request.", "Ungültige Anfrage."),
    "Ajuste de cores da imagem inválido.": ("Invalid image color settings.", "Ungültige Farbeinstellungen für das Bild."),
    "Cores da arte inválidas.": ("Invalid art colors.", "Ungültige Farben für das Bild."),
    "Erro interno. Tente de novo.": ("Internal error. Please try again.", "Interner Fehler. Versuch es noch einmal."),
    "Muitos pedidos seguidos. Espere um pouquinho.": (
        "Too many requests in a row. Wait a moment.", "Zu viele Anfragen hintereinander. Warte einen Moment."),
    "Muitos envios ao mesmo tempo. Espere um pouquinho.": (
        "Too many uploads at once. Wait a moment.", "Zu viele Uploads gleichzeitig. Warte einen Moment."),
    "O envio demorou demais.": ("The upload took too long.", "Der Upload hat zu lange gedauert."),
    "Muita gente criando agora. Tente de novo em alguns segundos.": (
        "Lots of people are creating right now. Try again in a few seconds.",
        "Gerade erstellen viele Leute etwas. Versuch es in ein paar Sekunden noch einmal."),
    "Muita gente enviando imagens agora. Tente de novo em alguns segundos.": (
        "Lots of people are uploading images right now. Try again in a few seconds.",
        "Gerade laden viele Leute Bilder hoch. Versuch es in ein paar Sekunden noch einmal."),
    "Servidor ocupado. Tente de novo em alguns segundos.": (
        "Server busy. Try again in a few seconds.", "Server ausgelastet. Versuch es in ein paar Sekunden noch einmal."),
    "Você já tem desenhos em andamento ou pediu muitos em pouco tempo. Espere um pouco.": (
        "You already have drawings in progress, or asked for too many in a short time. Wait a bit.",
        "Du hast schon Zeichnungen in Arbeit oder zu viele in kurzer Zeit angefragt. Warte kurz."),
    "Muitas exportações em pouco tempo. Espere alguns minutos.": (
        "Too many exports in a short time. Wait a few minutes.", "Zu viele Exporte in kurzer Zeit. Warte ein paar Minuten."),
    "Muitas imagens enviadas. Espere alguns minutos.": (
        "Too many images uploaded. Wait a few minutes.", "Zu viele Bilder hochgeladen. Warte ein paar Minuten."),
    "Muitas prévias seguidas. Espere um pouquinho.": (
        "Too many previews in a row. Wait a moment.", "Zu viele Vorschauen hintereinander. Warte einen Moment."),
    "Largura inválida.": ("Invalid width.", "Ungültige Breite."),
    "Muitas tentativas de resgate. Espere alguns minutos.": (
        "Too many claim attempts. Wait a few minutes.", "Zu viele Einlöseversuche. Warte ein paar Minuten."),
    "Muitos resgates ao mesmo tempo. Tente de novo em alguns minutos.": (
        "Too many claims at the same time. Try again in a few minutes.",
        "Zu viele Einlösungen gleichzeitig. Versuch es in ein paar Minuten noch einmal."),
    "Muitos pedidos de doação. Espere alguns minutos.": (
        "Too many donation requests. Wait a few minutes.", "Zu viele Spendenanfragen. Warte ein paar Minuten."),
    "Valor inválido.": ("Invalid amount.", "Ungültiger Betrag."),
    "Muitas cobranças criadas agora. Tente de novo em alguns minutos.": (
        "Too many invoices created right now. Try again in a few minutes.",
        "Gerade wurden zu viele Rechnungen erstellt. Versuch es in ein paar Minuten noch einmal."),
    "Não consegui criar a cobrança agora. Tente de novo.": (
        "Couldn't create the invoice right now. Please try again.",
        "Die Rechnung konnte gerade nicht erstellt werden. Versuch es noch einmal."),
    "Doação não encontrada.": ("Donation not found.", "Spende nicht gefunden."),
    "Não consegui consultar agora.": ("Couldn't check right now.", "Konnte gerade nicht abgefragt werden."),
    "Você já mandou alguns relatos agora. Espere uns minutos.": (
        "You've already sent a few reports just now. Wait a few minutes.",
        "Du hast gerade schon einige Meldungen geschickt. Warte ein paar Minuten."),
    "Marque o que aconteceu ou escreva uma mensagem.": (
        "Tick what happened or write a message.", "Markiere, was passiert ist, oder schreib eine Nachricht."),
    "Muitos relatos agora. Tente de novo mais tarde.": (
        "Too many reports right now. Try again later.", "Gerade zu viele Meldungen. Versuch es später noch einmal."),
    "Não consegui enviar agora. Tente de novo em alguns minutos.": (
        "Couldn't send it right now. Try again in a few minutes.",
        "Das Senden hat gerade nicht geklappt. Versuch es in ein paar Minuten noch einmal."),
    "Recompensas desligadas neste servidor.": ("Rewards are turned off on this server.", "Belohnungen sind auf diesem Server deaktiviert."),
    "Relatos desligados neste servidor.": ("Reports are turned off on this server.", "Meldungen sind auf diesem Server deaktiviert."),
    # servidor/render.py
    "Cor inválida.": ("Invalid color.", "Ungültige Farbe."),
    "Semente inválida.": ("Invalid seed.", "Ungültiger Seed."),
    "A imagem expirou no servidor. Envie de novo.": (
        "The image expired on the server. Upload it again.", "Das Bild ist auf dem Server abgelaufen. Lade es erneut hoch."),
    "Ajuste de imagem inválido.": ("Invalid image setting.", "Ungültige Bildeinstellung."),
    "Ajuste de fundo da imagem inválido.": ("Invalid image background setting.", "Ungültige Hintergrundeinstellung für das Bild."),
    "Símbolo desconhecido.": ("Unknown symbol.", "Unbekanntes Symbol."),
    "Só há forma detectada para imagens enviadas.": (
        "A detected shape only exists for uploaded images.", "Eine erkannte Form gibt es nur für hochgeladene Bilder."),
    # motor/forma_imagem.py
    "Imagem grande demais em pixels (máximo 25 megapixels).": (
        "Image too large in pixels (25 megapixels max).", "Bild hat zu viele Pixel (maximal 25 Megapixel)."),
    "Não consegui abrir esse arquivo como imagem.": (
        "Couldn't open that file as an image.", "Die Datei ließ sich nicht als Bild öffnen."),
    "Imagem não reduzida.": ("Image was not downscaled.", "Bild wurde nicht verkleinert."),
    "Não encontrei nenhum símbolo na imagem. Tente mudar o limiar ou inverter.": (
        "I couldn't find any symbol in the image. Try changing the sensitivity or inverting.",
        "Ich habe kein Symbol im Bild gefunden. Ändere die Empfindlichkeit oder invertiere das Bild."),
    "Essa imagem tem detalhes demais. Use um logo mais simples ou ajuste a sensibilidade.": (
        "This image has too much detail. Use a simpler logo or adjust the sensitivity.",
        "Dieses Bild hat zu viele Details. Nimm ein einfacheres Logo oder ändere die Empfindlichkeit."),
    "Não encontrei um símbolo nítido na imagem. Tente mudar o limiar ou inverter.": (
        "I couldn't find a clear symbol in the image. Try changing the sensitivity or inverting.",
        "Ich habe kein klares Symbol im Bild gefunden. Ändere die Empfindlichkeit oder invertiere das Bild."),
    "Não encontrei um símbolo nítido na imagem.": (
        "I couldn't find a clear symbol in the image.", "Ich habe kein klares Symbol im Bild gefunden."),
    # servidor/recompensas.py
    "Você já recebeu o limite de sats de hoje. Volte amanhã!": (
        "You've already received today's sats limit. Come back tomorrow!", "Du hast das Sats-Limit für heute schon erreicht. Komm morgen wieder!"),
    "Calma! Espere alguns segundos entre um resgate e outro.": (
        "Easy! Wait a few seconds between claims.", "Ruhig! Warte ein paar Sekunden zwischen zwei Einlösungen."),
    "O limite de recompensas do site para hoje acabou. Volte amanhã!": (
        "The site's reward limit for today has run out. Come back tomorrow!",
        "Das Belohnungslimit der Seite für heute ist aufgebraucht. Komm morgen wieder!"),
    "O pote está quase vazio. Que tal doar uns sats para ele voltar a pagar?": (
        "The pot is almost empty. How about donating a few sats so it can pay again?",
        "Der Topf ist fast leer. Wie wär's mit ein paar gespendeten Sats, damit er wieder auszahlen kann?"),
    "Esta imagem já foi resgatada ou o tempo para resgatar acabou. Exporte outra imagem.": (
        "This image was already claimed or the time to claim it ran out. Export another image.",
        "Dieses Bild wurde schon eingelöst oder die Zeit dafür ist abgelaufen. Exportiere ein neues Bild."),
    "Esse é o endereço do próprio pote. Use o seu endereço Lightning.": (
        "That's the pot's own address. Use your own Lightning address.", "Das ist die Adresse des Topfs selbst. Nimm deine eigene Lightning-Adresse."),
    "As recompensas estão pausadas hoje para conferência. Volte amanhã!": (
        "Rewards are paused today for review. Come back tomorrow!", "Die Belohnungen sind heute zur Prüfung pausiert. Komm morgen wieder!"),
    "Por enquanto só pagamos para endereços @coinos.io. Crie o seu grátis em coinos.io e use aqui.": (
        "For now we only pay out to @coinos.io addresses. Create yours for free at coinos.io and use it here.",
        "Aktuell zahlen wir nur an @coinos.io-Adressen aus. Erstelle deine kostenlos auf coinos.io und nutze sie hier."),
    "A Coinos recusou pagamentos para esse endereço hoje. Confira o endereço ou tente amanhã.": (
        "Coinos refused payments to that address today. Check the address or try tomorrow.",
        "Coinos hat heute Zahlungen an diese Adresse abgelehnt. Prüfe die Adresse oder versuch es morgen."),
    "A verificação anti-robô falhou. Tente de novo.": (
        "The anti-bot check failed. Please try again.", "Die Anti-Roboter-Prüfung ist fehlgeschlagen. Versuch es noch einmal."),
    "Não consegui falar com a carteira desse endereço.": (
        "Couldn't reach the wallet behind that address.", "Die Wallet hinter dieser Adresse war nicht erreichbar."),
    "Pagamentos para carteiras de fora estão indisponíveis agora. Use um endereço @coinos.io.": (
        "Payments to outside wallets are unavailable right now. Use a @coinos.io address.",
        "Zahlungen an externe Wallets sind gerade nicht verfügbar. Nimm eine @coinos.io-Adresse."),
    "Não consegui consultar o pote agora. Tente daqui a pouco.": (
        "Couldn't check the pot right now. Try again shortly.", "Der Topf konnte gerade nicht abgefragt werden. Versuch es gleich noch einmal."),
    "Tente de novo, por favor.": ("Please try again.", "Bitte versuch es noch einmal."),
    "A Coinos está indisponível agora. Tente de novo em instantes.": (
        "Coinos is unavailable right now. Try again in a moment.", "Coinos ist gerade nicht erreichbar. Versuch es gleich noch einmal."),
    "Não deu para confirmar o pagamento. Confira sua carteira em alguns minutos.": (
        "Couldn't confirm the payment. Check your wallet in a few minutes.",
        "Die Zahlung ließ sich nicht bestätigen. Schau in ein paar Minuten in deine Wallet."),
    "Não consegui confirmar esse endereço agora. Tente de novo.": (
        "Couldn't verify that address right now. Please try again.", "Die Adresse ließ sich gerade nicht prüfen. Versuch es noch einmal."),
    # servidor/lnurl.py
    "Endereço Lightning inválido. Use o formato usuario@dominio.": (
        "Invalid Lightning address. Use the format user@domain.", "Ungültige Lightning-Adresse. Nutze das Format nutzer@domain."),
    "A carteira devolveu uma invoice inválida.": ("The wallet returned an invalid invoice.", "Die Wallet hat eine ungültige Rechnung geliefert."),
    "A invoice da carteira está corrompida (checksum).": (
        "The wallet's invoice is corrupted (checksum).", "Die Rechnung der Wallet ist beschädigt (Prüfsumme)."),
    "A invoice não é da rede principal do Bitcoin.": ("The invoice is not for the main Bitcoin network.", "Die Rechnung gehört nicht zum Bitcoin-Hauptnetz."),
    "A invoice tem um valor inválido.": ("The invoice has an invalid amount.", "Die Rechnung hat einen ungültigen Betrag."),
    "A carteira devolveu uma invoice malformada.": ("The wallet returned a malformed invoice.", "Die Wallet hat eine fehlerhafte Rechnung geliefert."),
    "A invoice da carteira veio com valor diferente do pedido.": (
        "The wallet's invoice has a different amount than requested.", "Die Rechnung der Wallet hat einen anderen Betrag als angefragt."),
    "A invoice da carteira já está vencida.": ("The wallet's invoice has already expired.", "Die Rechnung der Wallet ist bereits abgelaufen."),
    "A carteira desse endereço cobra uma taxa de rota alta demais. Use outro endereço Lightning.": (
        "The wallet behind that address charges too high a routing fee. Use another Lightning address.",
        "Die Wallet hinter dieser Adresse verlangt zu hohe Routing-Gebühren. Nimm eine andere Lightning-Adresse."),
    "Esse domínio não aponta para um endereço público.": (
        "That domain doesn't point to a public address.", "Diese Domain zeigt nicht auf eine öffentliche Adresse."),
    "O serviço da carteira precisa usar HTTPS padrão (porta 443).": (
        "The wallet service must use standard HTTPS (port 443).", "Der Wallet-Dienst muss Standard-HTTPS (Port 443) verwenden."),
    "A carteira respondeu comprimido; não aceitamos.": (
        "The wallet replied with compressed data; we don't accept that.", "Die Wallet hat komprimiert geantwortet; das akzeptieren wir nicht."),
    "Resposta grande demais da carteira.": ("The wallet's response was too large.", "Die Antwort der Wallet war zu groß."),
    "A carteira tentou redirecionar; não seguimos redirecionamentos.": (
        "The wallet tried to redirect; we don't follow redirects.", "Die Wallet wollte weiterleiten; Weiterleitungen folgen wir nicht."),
    "A carteira devolveu uma resposta inválida.": ("The wallet returned an invalid response.", "Die Wallet hat eine ungültige Antwort geliefert."),
    "A carteira recusou o pagamento.": ("The wallet refused the payment.", "Die Wallet hat die Zahlung abgelehnt."),
    "A carteira devolveu limites inválidos.": ("The wallet returned invalid limits.", "Die Wallet hat ungültige Limits geliefert."),
    "Esse endereço não aceita pagamentos Lightning (LNURL-pay).": (
        "That address doesn't accept Lightning payments (LNURL-pay).", "Diese Adresse akzeptiert keine Lightning-Zahlungen (LNURL-pay)."),
    "A carteira desse endereço não aceita esse valor.": (
        "The wallet behind that address doesn't accept this amount.", "Die Wallet hinter dieser Adresse akzeptiert diesen Betrag nicht."),
    "Esse endereço Lightning não existe ou não aceita pagamentos.": (
        "That Lightning address doesn't exist or doesn't accept payments.", "Diese Lightning-Adresse existiert nicht oder nimmt keine Zahlungen an."),
    "A carteira desse endereço demorou demais para responder.": (
        "The wallet behind that address took too long to respond.", "Die Wallet hinter dieser Adresse hat zu lange nicht geantwortet."),
}

# mensagens com valores no meio: (regex em português, modelo em inglês, modelo em alemão)
PADROES = [
    (r"Parâmetro (\w+) inválido\.", "Invalid parameter {0}.", "Ungültiger Parameter {0}."),
    (r"Parâmetro (\w+) fora da faixa \((.+) a (.+)\)\.", "Parameter {0} is out of range ({1} to {2}).",
     "Parameter {0} liegt außerhalb des Bereichs ({1} bis {2})."),
    (r"Envio grande demais \(máximo (\d+) KB\)\.", "Upload too large (maximum {0} KB).", "Upload zu groß (maximal {0} KB)."),
    (r"Não encontrei o domínio (.+)\.", "Couldn't find the domain {0}.", "Die Domain {0} wurde nicht gefunden."),
    (r"A carteira respondeu (\d+)\.", "The wallet replied {0}.", "Die Wallet hat mit {0} geantwortet."),
    (r"A carteira desse endereço só aceita a partir de (\d+) sats\.",
     "The wallet behind that address only accepts {0} sats or more.", "Die Wallet hinter dieser Adresse akzeptiert erst ab {0} Sats."),
]
_PADROES = [(re.compile(r"^" + p + r"$", re.S), en, de) for p, en, de in PADROES]
_POSICAO = {"en": 0, "de": 1}


def idioma_de(valor):
    """Idioma pedido pelo cabeçalho X-Idioma; qualquer outra coisa vira português."""
    v = (valor or "").strip().lower()[:2]
    return v if v in IDIOMAS else "pt"


def traduzir(msg, idioma):
    if idioma not in _POSICAO or not isinstance(msg, str):
        return msg
    i = _POSICAO[idioma]
    if msg in MENSAGENS:
        return MENSAGENS[msg][i]
    for rx, en, de in _PADROES:
        m = rx.match(msg)
        if m:
            return (en, de)[i].format(*m.groups())
    return msg
