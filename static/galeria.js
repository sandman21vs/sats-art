"use strict";
// Link da própria arte (/a/<id>) e galeria pública. Roda depois do app.js e usa o que ele
// define (estado, sincronizar, renderizar, abrir, marcar, t...).

// ---------- link da arte ----------
// O link vale para a arte como está agora: mudou qualquer ajuste, o próximo compartilhamento
// cria outro (o servidor devolve o mesmo link para a mesma arte da mesma pessoa).
const CAMPOS_ARTE = ["fonte", "imagem_id", "limiar", "inverter", "cores_off", "angulo", "seed", "circulo", "tamanho",
  "raio", "zoom", "malha", "poeira_longe", "halo", "retrato", "cor", "brilho", "fundo", "multicor", "cores_arte"];
const ARTE_NA_URL = location.pathname.match(/^\/a\/([A-Za-z0-9_-]{16})\/?$/);
let linkArte = null;      // { id, chave, url, assinatura, galeria }
let pedidoLink = null;    // Promise do link sendo criado agora
let querGaleria = false;  // opt-in: desmarcado por padrão, nunca guardado entre visitas

function assinaturaArte() {
  const p = {};
  for (const k of CAMPOS_ARTE) if (k in estado) p[k] = estado[k];
  if (p.fonte !== "imagem") { delete p.imagem_id; delete p.limiar; delete p.inverter; delete p.cores_off; }
  return JSON.stringify([p, $("nome").value.trim()]);
}
// usado pelo convite (app.js): o link da arte, se ela ainda é a que está na tela
function linkAtual() {
  return linkArte && linkArte.assinatura === assinaturaArte() ? linkArte.url : null;
}

async function garantirLink() {
  const assinatura = assinaturaArte();
  if (linkArte && linkArte.assinatura === assinatura) {
    if (querGaleria !== linkArte.galeria) await definirGaleria(querGaleria);
    return linkArte;
  }
  if (pedidoLink && pedidoLink.assinatura === assinatura) return pedidoLink.promessa;
  const promessa = (async () => {
    const r = await postJSON("/api/arte", { ...estado, nome: $("nome").value.trim(), galeria: querGaleria });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.erro || t("arte.link_falha"));
    // o endereço que a pessoa está vendo (PUBLIC_URL pode estar diferente)
    linkArte = { ...j, url: location.origin + "/a/" + j.id, assinatura, galeria: querGaleria };
    return linkArte;
  })();
  pedidoLink = { assinatura, promessa };
  try { return await promessa; } finally { if (pedidoLink?.promessa === promessa) pedidoLink = null; }
}

async function definirGaleria(ligar) {
  if (!linkArte?.chave) return;
  const r = await postJSON(`/api/arte/${linkArte.id}/galeria`, { chave: linkArte.chave, galeria: ligar });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.erro || t("arte.link_falha"));
  linkArte.galeria = ligar;
}

// ---------- no modal de compartilhar ----------
function mostrarLink(estadoLink, texto) {
  const code = $("linkArte");
  $("blocoLinkArte").hidden = false;
  $("copiarLinkArte").disabled = estadoLink !== "ok";
  code.classList.toggle("erro", estadoLink === "erro");
  if (estadoLink === "ok") escrever(code, texto); else marcar(code, texto);
}

async function prepararLinkNoConvite() {
  if (!ultimaPrevia && !ultimoArquivo) { $("blocoLinkArte").hidden = true; return; }
  sincronizarOptIn();
  marcar($("copiarLinkArte"), "arte.copiar");
  if (linkAtual()) { mostrarLink("ok", linkAtual()); return; }
  mostrarLink("criando", "arte.criando");
  try {
    const l = await garantirLink();
    mostrarLink("ok", l.url);
    if (!$("modalCompartilhar").hidden) textoConvite();  // o convite passa a levar o link da arte
  } catch (e) {
    mostrarLink("erro", "arte.link_falha");
  }
}
$("compartilhar").addEventListener("click", prepararLinkNoConvite);
$("compartilharTopo").addEventListener("click", prepararLinkNoConvite);

$("copiarLinkArte").addEventListener("click", async () => {
  const b = $("copiarLinkArte"), url = linkAtual();
  if (!url) return;
  try { await navigator.clipboard.writeText(url); marcar(b, "arte.copiado"); }
  catch (e) {
    const sel = getSelection(), range = document.createRange();
    range.selectNodeContents($("linkArte")); sel.removeAllRanges(); sel.addRange(range);
    marcar(b, "share.selecione");
  }
  setTimeout(() => marcar(b, "arte.copiar"), 1800);
});

// ---------- opt-in da galeria (no convite e no passo 3) ----------
function sincronizarOptIn() {
  $("galeriaShare").checked = querGaleria;
  $("galeriaBaixar").checked = querGaleria;
}
async function trocarOptIn(ligar, msgEl) {
  querGaleria = ligar; sincronizarOptIn();
  escrever(msgEl, "");
  if (!linkAtual()) return;  // sem link ainda: vale para o próximo
  try {
    await definirGaleria(ligar);
    if (ligar) marcar(msgEl, "gal.entrou");
  } catch (e) { msgEl.className = "msg erro"; escrever(msgEl, e.message); }
}
$("galeriaShare").addEventListener("change", (e) => trocarOptIn(e.target.checked, $("msgGaleriaShare")));
$("galeriaBaixar").addEventListener("change", (e) => trocarOptIn(e.target.checked, $("msgGaleriaBaixar")));

// baixou com a caixa marcada: a arte entra na galeria (app.js avisa com "arte-baixada")
document.addEventListener("arte-baixada", async () => {
  const msg = $("msgGaleriaBaixar");
  msg.className = "msg"; escrever(msg, "");
  if (!querGaleria) return;
  marcar(msg, "gal.enviando");
  try {
    await garantirLink();
    msg.className = "msg sucesso"; marcar(msg, "gal.entrou");
  } catch (e) { msg.className = "msg erro"; escrever(msg, e.message); }
});

// ---------- abrir um link /a/<id> ----------
async function abrirArteDoLink(id) {
  const st = $("status");
  st.className = "status busy"; marcar(st, "arte.abrindo");
  let j;
  try {
    const r = await fetch("/api/arte/" + id, { headers: { "X-Idioma": idioma } });
    j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.erro || "");
  } catch (e) {
    history.replaceState(null, "", "/");
    renderizar();
    erroNoStatus(K("arte.expirou"));
    return;
  }
  // a arte de quem compartilhou, sem apagar o que a visita tinha salvo até ela mexer em algo
  estado = { ...PADRAO, ...j.estado };
  paletaLogo = Array.isArray(j.cores) ? j.cores.slice(0, 6) : [];
  desenharCoresLogo();
  if (j.logo) {
    $("thumb").src = `/a/${id}/logo.png`; $("thumb").hidden = false;
    marcar($("dropTxt"), "p1.drop_trocar");
    document.querySelector('[data-fonte="imagem"]').disabled = false;
  }
  if (j.nome) $("nome").value = j.nome;
  sincronizar();
  atualizarForma();
  renderizar();
  $("blocoArteAberta").hidden = false;
  $("dica").hidden = true;
}

$("fazerMinha").addEventListener("click", () => {
  $("blocoArteAberta").hidden = true;
  history.replaceState(null, "", "/");
  salvar();
  const drop = $("drop");
  drop.scrollIntoView({ behavior: "smooth", block: "center" });
  drop.classList.add("hover");
  setTimeout(() => drop.classList.remove("hover"), 1600);
});

// ---------- galeria ----------
let galeriaMais = 0, carregandoGaleria = false;

async function carregarGaleria(primeira) {
  if (carregandoGaleria) return;
  carregandoGaleria = true;
  const grade = $("gradeGaleria"), msg = $("msgGaleria"), mais = $("maisGaleria");
  if (primeira) { grade.innerHTML = ""; galeriaMais = 0; }
  marcar(msg, "gal.carregando"); msg.className = "msg";
  mais.disabled = true;
  try {
    const r = await fetch(`/api/galeria?n=18${galeriaMais ? "&antes=" + galeriaMais : ""}`, { headers: { "X-Idioma": idioma } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.erro || t("gal.falha"));
    for (const item of j.itens) grade.append(itemGaleria(item));
    galeriaMais = j.mais || 0;
    mais.hidden = !j.mais;
    if (grade.children.length) escrever(msg, ""); else marcar(msg, "gal.vazia");
  } catch (e) { msg.className = "msg erro"; escrever(msg, e.message); }
  mais.disabled = false;
  carregandoGaleria = false;
}

function itemGaleria({ id }) {
  const fig = document.createElement("figure");
  fig.className = "item-galeria";
  const a = Object.assign(document.createElement("a"), { href: "/a/" + id });
  a.setAttribute("data-i18n-aria-label", "gal.abrir"); aplicarEm(a);
  const img = Object.assign(document.createElement("img"), { src: `/a/${id}/mini.webp`, alt: "", loading: "lazy", decoding: "async", width: 480, height: 300 });
  a.append(img);
  const d = Object.assign(document.createElement("button"), { type: "button", className: "denunciar", textContent: "⚑" });
  d.setAttribute("data-i18n-title", "gal.denunciar"); d.setAttribute("data-i18n-aria-label", "gal.denunciar"); aplicarEm(d);
  d.addEventListener("click", () => denunciar(fig, d, id));
  fig.append(a, d);
  return fig;
}

async function denunciar(fig, botao, id) {
  // dois toques: o primeiro pergunta, o segundo manda
  if (!botao.classList.contains("confirmar")) {
    botao.classList.add("confirmar"); marcar(botao, "gal.denunciar_conf");
    setTimeout(() => { if (botao.isConnected && !botao.disabled) { botao.classList.remove("confirmar"); escrever(botao, "⚑"); } }, 4000);
    return;
  }
  botao.disabled = true;
  try {
    const r = await postJSON("/api/galeria/denunciar", { id });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.erro || t("gal.falha"));
    fig.classList.add("denunciada");
    marcar(botao, "gal.denunciada");
  } catch (e) {
    botao.disabled = false; botao.classList.remove("confirmar"); escrever(botao, "⚑");
    $("msgGaleria").className = "msg erro"; escrever($("msgGaleria"), e.message);
  }
}

function abrirGaleria() { abrir("modalGaleria"); carregarGaleria(true); }
$("abrirGaleria").addEventListener("click", abrirGaleria);
$("abrirGaleria2").addEventListener("click", abrirGaleria);
$("maisGaleria").addEventListener("click", () => carregarGaleria(false));

// ---------- início ----------
if (ARTE_NA_URL) abrirArteDoLink(ARTE_NA_URL[1]);
