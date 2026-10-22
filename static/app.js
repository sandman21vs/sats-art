"use strict";
const SLIDERS = {
  "sliders-basico": [
    ["tamanho", "sl.tamanho", 0.3, 2.0, 0.01],
    ["brilho", "sl.brilho", 0.3, 2, 0.01],
  ],
  "sliders-avancado": [
    ["angulo", "sl.angulo", -90, 90, 1],
    ["zoom", "sl.zoom", 1, 6, 0.05],
    ["raio", "sl.raio", 150, 450, 1],
    ["malha", "sl.malha", 8, 30, 0.5],
    ["halo", "sl.halo", 40, 300, 1],
    ["poeira_longe", "sl.poeira_longe", 0, 0.5, 0.01],
    ["fundo", "sl.fundo", 0, 2.5, 0.01],
  ],
};
const CORES = ["#ff6600", "#f7b500", "#ff3d6e", "#9b6bff", "#2fb4ff", "#21d39b", "#f2efe9"];
// [lado maior em px, nome]; as dimensões do rótulo seguem o formato (paisagem ou retrato)
const RESOLUCOES = [[1920, "Full HD"], [3840, "4K"], [5120, "5K"], [7680, "8K"]];
const dimensoes = (lado) => (estado.retrato ? [lado * 9 / 16, lado] : [lado, lado * 9 / 16]);
const rotulo = (lado, nome) => `${nome} · ${dimensoes(lado).join("×")}`;
const ANGULO_PADRAO = { sat: -21, imagem: 0 };
const PADRAO = { fonte: "sat", retrato: false, angulo: -21, seed: 21, circulo: true, tamanho: 1, raio: 300, zoom: 2.7,
  malha: 14, poeira_longe: 0.22, halo: 110, cor: "#ff6600", brilho: 1, fundo: 1, limiar: 60, inverter: false,
  cores_off: [], multicor: false, cores_arte: [] };

const guardado = (k, padrao) => { try { return JSON.parse(localStorage.getItem(k)) ?? padrao; } catch (e) { return padrao; } };
const guardar = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };

idioma = detectarIdioma(guardado("idioma", null));
let estado = { ...PADRAO, ...guardado("rede-nos", {}) };
// primeira visita pelo celular em pé: já começa no formato da tela do celular
if (guardado("rede-nos", null) === null && matchMedia("(orientation: portrait) and (max-width: 760px)").matches) {
  estado.retrato = true;
}
if (!(estado.fonte in ANGULO_PADRAO) || estado.fonte === "imagem") {  // a imagem não fica salva entre sessões
  Object.assign(estado, { fonte: "sat", angulo: ANGULO_PADRAO.sat });
}
delete estado.imagem_id;
// as cores da logo valem só para a imagem enviada nesta sessão (estados antigos não têm estes campos)
Object.assign(estado, { cores_off: [], cores_arte: [], multicor: estado.multicor === true });
let paletaLogo = [];  // [{ hex, parte, arte }] da imagem enviada, na ordem do servidor
let INFO = { recompensas: false, max_export: 3840, recompensas_seq: [], limite_dia: 0 };
let DIA = null;  // { feitos, ganho_hoje, restante_hoje, proxima }

const $ = (id) => document.getElementById(id);
const fmt = (v, step) => (step < 1 ? Number(v).toFixed(step < 0.1 ? 2 : 1) : String(v));
const num = (n) => Number(n).toLocaleString(LOCAIS[idioma]);
// JSON com Content-Type explícito: o servidor recusa POST sem ele (proteção contra CSRF)
const postJSON = (url, corpo) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Idioma": idioma }, body: JSON.stringify(corpo) });
// o último erro mostrado vai junto, se a pessoa reportar um problema
let ultimoErro = "";
const textoDe = (v) => (v && typeof v === "object" ? t(v.k, v.v) : String(v));  // texto simples ou K(chave, vars)
const mensagem = (el, texto, tipo = "") => {
  el.className = "msg " + tipo; escrever(el, texto);
  if (tipo === "erro" && texto) ultimoErro = textoDe(texto);
};
const erroNoStatus = (texto) => {
  $("status").className = "status erro"; escrever($("status"), texto);
  if (texto) ultimoErro = textoDe(texto);
};
const enderecoValido = (e) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e.replace(/^lightning:/i, ""));
// sem pagamentos externos liberados no servidor, só endereços @coinos.io recebem
const enderecoAceito = (e) => enderecoValido(e) && (INFO.pagamento_externo || /@coinos\.io$/i.test(e.trim()));

// ---------- controles ----------
for (const [box, list] of Object.entries(SLIDERS)) {
  for (const [key, label, min, max, step] of list) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<label><span class="t" data-i18n="${label}"></span> <span id="v-${key}"></span></label>
      <input type="range" id="${key}" min="${min}" max="${max}" step="${step}">`;
    $(box).appendChild(row);
    const inp = row.querySelector("input");
    inp.addEventListener("input", () => { estado[key] = parseFloat(inp.value); $("v-" + key).textContent = fmt(inp.value, step); mudou(); });
  }
}
for (const c of CORES) {
  const b = document.createElement("button");
  b.style.background = c; b.dataset.cor = c; b.title = c;
  b.dataset.i18nVars = JSON.stringify({ cor: c }); b.setAttribute("data-i18n-aria-label", "p2.cor_aria");
  b.addEventListener("click", () => { estado.cor = c; estado.multicor = false; sincronizar(); mudou(); });
  $("cores").insertBefore(b, $("cores").querySelector(".outra"));
}

function sincronizar() {
  for (const list of Object.values(SLIDERS)) for (const [key, , , , step] of list) {
    $(key).value = estado[key]; $("v-" + key).textContent = fmt(estado[key], step);
  }
  $("seed").value = estado.seed;
  $("circulo").checked = estado.circulo;
  $("cor").value = estado.cor;
  document.querySelectorAll("[data-cor]").forEach((b) => b.classList.toggle("on", !usaCoresLogo() && b.dataset.cor === estado.cor));
  $("limiar").value = estado.limiar; $("v-limiar").textContent = estado.limiar;
  $("inverter").checked = estado.inverter;
  document.querySelectorAll("[data-fonte]").forEach((b) => b.classList.toggle("on", b.dataset.fonte === estado.fonte));
  $("imgopts").hidden = estado.fonte !== "imagem";
  document.querySelectorAll("[data-formato]").forEach((b) => b.classList.toggle("on", (b.dataset.formato === "retrato") === estado.retrato));
  document.body.classList.toggle("retrato", estado.retrato);
  for (const o of $("largura").options) o.textContent = rotulo(Number(o.value), RESOLUCOES.find(([w]) => w === Number(o.value))[1]);
  sincronizarCoresLogo();
}

function salvar() { const { imagem_id, ...resto } = estado; guardar("rede-nos", resto); }

// ---------- prévia ----------
let timer, ocupado = false, pendente = false, primeiraPrevia = true, ultimaPrevia = null;
function mudou() {
  salvar(); clearTimeout(timer); timer = setTimeout(renderizar, 220);
  if (!primeiraPrevia) $("dica").hidden = true;
}

async function renderizar() {
  if (ocupado) { pendente = true; return; }
  ocupado = true;
  const st = $("status");
  st.className = "status busy"; escrever(st, K("status.desenhando"));
  const t0 = performance.now();
  try {
    const r = await postJSON("/api/preview", estado);
    if (!r.ok) throw new Error((await r.json()).erro || r.statusText);
    ultimaPrevia = await r.blob();
    const url = URL.createObjectURL(ultimaPrevia);
    const img = $("preview"), velho = img.src;
    img.src = url; img.hidden = false;
    if (velho.startsWith("blob:")) URL.revokeObjectURL(velho);
    st.className = "status"; escrever(st, K("status.pronto", { s: ((performance.now() - t0) / 1000).toFixed(1) }));
    primeiraPrevia = false;
  } catch (e) {
    erroNoStatus(e.message);
  }
  ocupado = false;
  if (pendente) { pendente = false; renderizar(); }
}

// ---------- símbolo ----------
document.querySelectorAll("[data-fonte]").forEach((b) => b.addEventListener("click", () => {
  if (b.disabled) return;
  estado.fonte = b.dataset.fonte;
  estado.angulo = ANGULO_PADRAO[estado.fonte];
  sincronizar(); mudou(); atualizarForma();
}));

async function carregarImagem(blob) {
  if (!blob || !blob.type.startsWith("image/")) return;
  const r = await fetch("/api/imagem", { method: "POST", headers: { "Content-Type": blob.type, "X-Idioma": idioma }, body: blob });
  const j = await r.json();
  if (!r.ok) { erroNoStatus(j.erro); return; }
  $("thumb").src = URL.createObjectURL(blob); $("thumb").hidden = false;
  marcar($("dropTxt"), "p1.drop_trocar");
  estado.imagem_id = j.id;
  estado.fonte = "imagem"; estado.angulo = 0;
  novaPaleta(j.cores);
  document.querySelector('[data-fonte="imagem"]').disabled = false;
  sincronizar(); atualizarForma(); mudou();
}

let pedidoForma = 0;  // só a resposta do último pedido vale (cliques rápidos nas cores chegam fora de ordem)
async function atualizarForma() {
  if (estado.fonte !== "imagem") return;
  const n = ++pedidoForma;
  const r = await postJSON("/api/forma", estado);
  const png = r.ok ? await r.blob() : null;
  if (n !== pedidoForma) return;
  if (r.ok) $("shape").src = URL.createObjectURL(png);
  else erroNoStatus((await r.json()).erro);
}

const drop = $("drop");
drop.addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => carregarImagem(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("hover"); }));
["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "drop" || e.target === document.documentElement) drop.classList.remove("hover"); }));
document.addEventListener("drop", (e) => carregarImagem(e.dataTransfer.files[0]));
document.addEventListener("paste", (e) => {
  if (e.target.matches?.("input[type=text], input[type=number]")) return;
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
  if (item) carregarImagem(item.getAsFile());
});

$("limiar").addEventListener("input", (e) => { estado.limiar = parseInt(e.target.value); $("v-limiar").textContent = estado.limiar; atualizarForma(); mudou(); });
$("inverter").addEventListener("change", (e) => { estado.inverter = e.target.checked; atualizarForma(); mudou(); });
document.querySelectorAll("[data-formato]").forEach((b) => b.addEventListener("click", () => {
  estado.retrato = b.dataset.formato === "retrato";
  sincronizar(); mudou();
}));
$("circulo").addEventListener("change", (e) => { estado.circulo = e.target.checked; mudou(); });
$("seed").addEventListener("change", (e) => { estado.seed = parseInt(e.target.value) || 0; mudou(); });
$("sortear").addEventListener("click", () => { estado.seed = Math.floor(Math.random() * 100000); $("seed").value = estado.seed; mudou(); });
$("cor").addEventListener("input", (e) => { estado.cor = e.target.value; estado.multicor = false; sincronizar(); mudou(); });
$("reset").addEventListener("click", () => {
  const { fonte, imagem_id, limiar, inverter, retrato, cores_off } = estado;  // o formato escolhido fica
  estado = { ...PADRAO, fonte, imagem_id, limiar, inverter, retrato, cores_off, angulo: ANGULO_PADRAO[fonte],
    cores_arte: paletaLogo.map((c) => c.arte) };
  sincronizar(); mudou();
});

// ---------- cores da logo ----------
// Passo 1: cada cor dominante da imagem vira um chip; desligar tira aquela parte do símbolo.
// Passo 2: "Cores da logo" faz cada parte acender os nós em volta com a sua cor (trocável).
// Os controles só aparecem quando a imagem tem mais de uma cor relevante.
const temCoresLogo = () => estado.fonte === "imagem" && paletaLogo.length > 1;
const usaCoresLogo = () => temCoresLogo() && estado.multicor;
// cor de brilho da parte i: a escolhida, a sugerida pelo servidor ou (cores neutras) a cor principal
const corDaParte = (i) => estado.cores_arte[i] || estado.cor;

function novaPaleta(cores) {
  paletaLogo = Array.isArray(cores) ? cores.slice(0, 6) : [];
  estado.cores_off = [];
  estado.cores_arte = paletaLogo.map((c) => c.arte);
  if (paletaLogo.length < 2) estado.multicor = false;
  desenharCoresLogo();
}

function desenharCoresLogo() {
  const chips = $("chipsLogo"), regioes = $("regioesArte"), mini = $("miniPaleta");
  chips.innerHTML = ""; regioes.innerHTML = ""; mini.innerHTML = "";
  paletaLogo.forEach((c, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip-cor"; b.dataset.parte = i;
    const bola = document.createElement("i");
    bola.style.background = c.hex;
    b.append(bola, `${Math.max(1, Math.round(c.parte * 100))}%`);
    b.addEventListener("click", () => alternarCorLogo(i));
    chips.append(b);

    const r = document.createElement("label");
    r.className = "regiao"; r.dataset.parte = i; r.setAttribute("data-i18n-title", "p2.regiao_title"); aplicarEm(r);
    const orig = document.createElement("i"), seta = document.createElement("span"), brilho = document.createElement("span");
    orig.className = "orig"; orig.style.background = c.hex;
    seta.className = "seta"; seta.textContent = "→";
    brilho.className = "brilho";
    const inp = Object.assign(document.createElement("input"), { type: "color" });
    inp.dataset.i18nVars = JSON.stringify({ hex: c.hex }); inp.setAttribute("data-i18n-aria-label", "p2.regiao_aria"); aplicarEm(inp);
    inp.addEventListener("input", () => { estado.cores_arte[i] = inp.value; sincronizarCoresLogo(); mudou(); });
    brilho.append(inp);
    r.append(orig, seta, brilho);
    regioes.append(r);

    const m = document.createElement("i");
    m.style.background = c.arte || c.hex;
    mini.append(m);
  });
  sincronizarCoresLogo();
}

function sincronizarCoresLogo() {
  const tem = temCoresLogo(), usa = usaCoresLogo();
  $("coresLogo").hidden = !tem;
  $("corLogo").hidden = !tem;
  $("corLogo").classList.toggle("on", usa);
  $("corLogo").setAttribute("aria-pressed", String(usa));
  $("coresArte").hidden = !usa;
  $("chipsLogo").querySelectorAll(".chip-cor").forEach((b) => {
    const i = Number(b.dataset.parte), ligada = !estado.cores_off.includes(i);
    b.classList.toggle("on", ligada);
    b.setAttribute("aria-pressed", String(ligada));
    b.dataset.i18nVars = JSON.stringify({ hex: paletaLogo[i].hex });
    b.setAttribute("data-i18n-aria-label", ligada ? "p1.chip_on" : "p1.chip_off"); aplicarEm(b);
  });
  $("regioesArte").querySelectorAll(".regiao").forEach((r) => {
    const i = Number(r.dataset.parte), cor = corDaParte(i);
    r.hidden = estado.cores_off.includes(i);
    const brilho = r.querySelector(".brilho");
    brilho.style.background = cor; brilho.style.color = cor;
    r.querySelector("input").value = cor;
  });
}

let timerDicaCores;
function alternarCorLogo(i) {
  const off = new Set(estado.cores_off);
  if (off.has(i)) off.delete(i);
  else if (off.size >= paletaLogo.length - 1) {  // sem nenhuma cor não sobra símbolo
    const dica = $("dicaCoresLogo");
    clearTimeout(timerDicaCores);
    marcar(dica, "p1.cores_min");
    timerDicaCores = setTimeout(() => marcar(dica, "p1.cores_dica"), 2500);
    return;
  } else off.add(i);
  estado.cores_off = [...off].sort((a, b) => a - b);
  sincronizarCoresLogo(); atualizarForma(); mudou();
}

$("corLogo").addEventListener("click", () => { estado.multicor = !estado.multicor; sincronizar(); mudou(); });

// ---------- ganhe sats ----------
async function carregarInfo() {
  try {
    const r = await fetch("/api/info");
    if (!r.ok) return;  // mantém a informação anterior (ex.: 429 por excesso de pedidos)
    INFO = await r.json();
  } catch (e) { return; }
  const sel = $("largura"), atual = sel.value;
  sel.innerHTML = "";
  for (const [w, nome] of RESOLUCOES.filter(([w]) => w <= INFO.max_export)) sel.add(new Option(rotulo(w, nome), w));
  sel.value = atual && [...sel.options].some((o) => o.value === atual) ? atual : sel.options[sel.options.length - 1].value;

  configurarLinks();
  configurarReport();
  $("blocoGanhe").hidden = !INFO.recompensas;
  $("blocoEndereco").hidden = !INFO.recompensas;
  $("blocoApoie").hidden = INFO.recompensas || !INFO.doacao_endereco;
  $("tutGanhe").hidden = !INFO.recompensas;
  $("abrirBounty3").hidden = !INFO.recompensas;  // sem recompensas não há pote para roubar
  $("desafio").classList.toggle("sozinho", !INFO.recompensas);
  textosInfo();
  if (INFO.recompensas) await carregarDia();
  atualizarBotao();
}
// textos que dependem do que o servidor informou (refeitos quando o idioma muda)
function textosInfo() {
  marcar($("tutTitulo"), INFO.recompensas ? "tut.titulo" : "tut.titulo_sem");
  if (!INFO.recompensas) return;
  const seq = INFO.recompensas_seq;
  marcar($("ganheTitulo"), "ganhe.titulo", { n: INFO.limite_dia });
  $("ganheSub").textContent = t("ganhe.sub", { lista: seq.map((s, i) => t("ganhe.item", { sats: s, n: ordinal(i) })).join(" + ") });
  $("tutGanheTxt").textContent = t("tut.3d_lista", { lista: seq.map((s, i) => t("ganhe.item_curto", { sats: s, n: ordinal(i) })).join(", ") });
  $("poteSaldo").textContent = INFO.pote == null ? "…" : t("pote.saldo", { n: num(INFO.pote) });
  marcar($("tituloPasso3"), "p3.titulo_ganhe");
  if (!INFO.pagamento_externo) {
    $("ln").setAttribute("data-i18n-placeholder", "p3.ph_coinos"); aplicarEm($("ln"));
    marcar($("hintLn"), "p3.hint_coinos", null, true);
  }
  if (DIA) textoDia();
}
setInterval(carregarInfo, 60000);

async function carregarDia() {
  if (!INFO.recompensas) return;
  try {
    const r = await fetch("/api/meu-dia");
    if (!r.ok) return;
    DIA = await r.json();
  } catch (err) { return; }
  const pct = INFO.limite_dia ? (100 * DIA.ganho_hoje / INFO.limite_dia) : 0;
  $("barra").style.width = pct + "%";
  textoDia();
  atualizarBotao();
}
function textoDia() {
  if (DIA.proxima) marcar($("txtProgresso"), "ganhe.progresso", { ganho: DIA.ganho_hoje, limite: INFO.limite_dia, prox: DIA.proxima });
  else marcar($("txtProgresso"), "ganhe.completo", { limite: INFO.limite_dia });
}

function atualizarBotao() {
  const b = $("exportar"), e = $("ln").value.trim();
  if (!INFO.recompensas) { marcar(b, "btn.baixar"); return; }
  const sats = DIA?.proxima || INFO.recompensas_seq[0];
  if (DIA && !DIA.proxima) marcar(b, "btn.hoje_ja");
  else if (!enderecoValido(e)) marcar(b, "btn.ponha", { sats });
  else if (!enderecoAceito(e)) marcar(b, "btn.coinos");
  else marcar(b, "btn.ganhar", { sats });
}
$("ln").value = guardado("ln-endereco", "");
let timerLn;
$("ln").addEventListener("input", () => { clearTimeout(timerLn); timerLn = setTimeout(atualizarBotao, 150); });

// ---------- exportar e receber ----------
let ultimoArquivo = null, ultimoToken = null;

function baixar(arquivo) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(arquivo); a.download = arquivo.name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60000);
}

$("exportar").addEventListener("click", async () => {
  const btn = $("exportar"), msg = $("msgExport");
  btn.disabled = true; $("dica").hidden = true; mensagem(msg, K("exp.desenhando"));
  try {
    const corpo = { ...estado, largura: parseInt($("largura").value), nome: $("nome").value.trim() };
    const r = await postJSON("/api/exportar", corpo);
    if (!r.ok) throw new Error((await r.json()).erro || r.statusText);
    const nome = r.headers.get("X-Nome-Arquivo") || "rede.png";
    ultimoArquivo = new File([await r.blob()], nome, { type: "image/png" });
    ultimoToken = r.headers.get("X-Token-Resgate");
    baixar(ultimoArquivo);
    document.dispatchEvent(new CustomEvent("arte-baixada"));  // galeria.js: opt-in da galeria
    mensagem(msg, "");
    $("cartao").hidden = false;
    marcar($("resultado"), "res.baixada");
    mensagem($("msgResgate"), "");
    $("blocoReceber").hidden = true;
    $("cartao").scrollIntoView({ behavior: "smooth", block: "nearest" });

    const e = $("ln").value.trim();
    if (INFO.recompensas && ultimoToken && (!DIA || DIA.proxima)) {
      if (enderecoAceito(e)) await receber(e);
      else {
        const sats = DIA?.proxima || "";
        mensagem($("msgResgate"), K(INFO.pagamento_externo || !enderecoValido(e) ? "res.coloque" : "res.use_coinos", { sats }));
        $("blocoReceber").hidden = false;
        $("receber").disabled = false;
        marcar($("receber"), "res.receber_n", { sats });
      }
    }
  } catch (e) { mensagem(msg, e.message, "erro"); }
  btn.disabled = false;
});
$("baixarDeNovo").addEventListener("click", (e) => { e.preventDefault(); if (ultimoArquivo) baixar(ultimoArquivo); });

$("receber").addEventListener("click", () => {
  const e = $("ln").value.trim();
  if (!enderecoAceito(e)) {
    $("ln").focus();
    mensagem($("msgResgate"), K(INFO.pagamento_externo ? "res.digite" : "res.so_coinos"), "erro");
    return;
  }
  receber(e);
});

async function receber(endereco) {
  const msg = $("msgResgate");
  guardar("ln-endereco", endereco);
  $("blocoReceber").hidden = true;
  try {
    mensagem(msg, K("res.humano"));
    const prova = await provaDeTrabalho();
    mensagem(msg, K("res.enviando"));
    const r = await postJSON("/api/resgatar", { token: ultimoToken, endereco, prova });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro);
    marcar($("resultado"), "res.ganhou", { sats: j.sats });
    mensagem(msg, j.proxima ? K("res.ok_mais", { sats: j.proxima }) : K("res.ok_fim", { limite: INFO.limite_dia }), "sucesso");
    chuvaDeSats(j.sats);
    DIA = j; carregarInfo();
  } catch (e) {
    mensagem(msg, e.message, "erro");
    $("blocoReceber").hidden = false;
    $("receber").disabled = false;
    marcar($("receber"), "res.tentar");
  }
}

function chuvaDeSats(n) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  for (let i = 0; i < Math.min(n * 2, 30); i++) {
    const s = document.createElement("span");
    s.className = "cai"; s.textContent = "⚡";
    s.style.left = Math.random() * 100 + "vw";
    s.style.animationDelay = Math.random() * 0.8 + "s";
    s.style.fontSize = 14 + Math.random() * 18 + "px";
    document.body.appendChild(s);
    setTimeout(() => s.remove(), 3200);
  }
}

// ---------- prova de trabalho (anti-robô), roda num worker ----------
const POW_WORKER = `
const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
const ror = (x, n) => (x >>> n) | (x << (32 - n));
const w = new Int32Array(64);
function sha256(s) {  // s: texto ASCII
  const l = s.length, nW = (((l + 8) >> 6) + 1) * 16, m = new Int32Array(nW);
  for (let i = 0; i < l; i++) m[i >> 2] |= s.charCodeAt(i) << (24 - (i % 4) * 8);
  m[l >> 2] |= 0x80 << (24 - (l % 4) * 8);
  m[nW - 1] = l * 8;
  let H = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  for (let j = 0; j < nW; j += 16) {
    let [a, b, c, d, e, f, g, h] = H;
    for (let i = 0; i < 64; i++) {
      if (i < 16) w[i] = m[j + i];
      else {
        const x = w[i - 15], y = w[i - 2];
        w[i] = (w[i - 16] + (ror(x, 7) ^ ror(x, 18) ^ (x >>> 3)) + w[i - 7] + (ror(y, 17) ^ ror(y, 19) ^ (y >>> 10))) | 0;
      }
      const t1 = (h + (ror(e, 6) ^ ror(e, 11) ^ ror(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
      const t2 = ((ror(a, 2) ^ ror(a, 13) ^ ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    H = [H[0] + a | 0, H[1] + b | 0, H[2] + c | 0, H[3] + d | 0, H[4] + e | 0, H[5] + f | 0, H[6] + g | 0, H[7] + h | 0];
  }
  return H;
}
function zeros(H) { let n = 0; for (const x of H) { const z = Math.clz32(x); n += z; if (z < 32) break; } return n; }
onmessage = (ev) => {
  const { salt, bits } = ev.data;
  for (let n = 0; ; n++) if (zeros(sha256(salt + ":" + n)) >= bits) { postMessage(String(n)); return; }
};`;

async function provaDeTrabalho() {
  const rd = await fetch("/api/desafio", { headers: { "X-Idioma": idioma } });
  const d = await rd.json();
  if (!rd.ok) throw new Error(d.erro || t("res.desafio_falha"));
  const worker = new Worker(URL.createObjectURL(new Blob([POW_WORKER], { type: "text/javascript" })));
  const nonce = await new Promise((ok) => { worker.onmessage = (e) => ok(e.data); worker.postMessage(d); });
  worker.terminate();
  return { ...d, nonce };
}

// ---------- compartilhar ----------
// Convite com os dois jeitos de "jogar": criar e ganhar sats, ou atacar o site e roubar o pote.
function convite(site) {
  if (!INFO.recompensas) return t(ultimoArquivo || site !== location.origin ? "conv.sem_pote_arte" : "conv.sem_pote", { site });
  const pote = INFO.pote == null ? "" : t("conv.pote", { n: num(INFO.pote) });
  return [
    t(ultimoArquivo ? "conv.l1_arte" : "conv.l1"),
    "",
    t("conv.l2", { limite: INFO.limite_dia }),
    t("conv.l3", { pote }),
    "",
    site,
  ].join("\n");  // cabe nos 280 caracteres do X (o link conta como 23)
}

// Popup com o convite e a arte. Os links de X, Telegram e WhatsApp só levam texto (nenhum site
// consegue anexar imagem por eles), então:
// - no celular, "Compartilhar com a arte" usa o compartilhamento do sistema, que leva a imagem;
// - no computador, clicar numa rede copia a arte para a área de transferência: é só colar no post.
// A arte é a exportada ou, antes de exportar, a prévia que está na tela.
// o link da arte (galeria.js), se houver; senão o endereço que a pessoa está vendo (PUBLIC_URL pode estar errado)
const siteAtual = () => (typeof linkAtual === "function" && linkAtual()) || location.origin;
let arteConvite = null;  // { jpg: File, png: Blob, url }

async function prepararArte() {
  const fonte = ultimoArquivo || ultimaPrevia;
  if (!fonte) return null;
  const bmp = await createImageBitmap(fonte);
  const k = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));  // leve: cabe no limite das redes
  const c = Object.assign(document.createElement("canvas"),
    { width: Math.round(bmp.width * k), height: Math.round(bmp.height * k) });
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  const blob = (tipo, q) => new Promise((ok) => c.toBlob(ok, tipo, q));
  const [png, jpg] = await Promise.all([blob("image/png"), blob("image/jpeg", 0.92)]);
  const arquivo = new File([jpg], "rede-de-nos.jpg", { type: "image/jpeg" });
  return { png, jpg: arquivo, url: URL.createObjectURL(arquivo) };
}

function copiarArte() {
  // começa a escrita na hora do clique (a página ainda tem foco), antes de abrir a rede
  if (!arteConvite || !window.ClipboardItem || !navigator.clipboard?.write) return Promise.reject();
  return navigator.clipboard.write([new ClipboardItem({ "image/png": arteConvite.png })]);
}

function textoConvite() {
  const site = siteAtual();
  const texto = convite(site);
  $("textoConvite").value = texto;
  const cod = encodeURIComponent(texto);
  $("shareX").href = `https://x.com/intent/post?text=${cod}`;
  $("shareTelegram").href = `https://t.me/share/url?url=${encodeURIComponent(site)}&text=${encodeURIComponent(texto.replace(site, "").trim())}`;
  $("shareWhatsapp").href = `https://wa.me/?text=${cod}`;
}

async function abrirCompartilhar() {
  textoConvite();
  marcar($("copiarConvite"), "share.copiar");
  marcar($("copiarArte"), "share.copiar_arte");
  $("avisoArte").hidden = true;
  $("arteNoConvite").hidden = true; $("compartilharArte").hidden = true; $("copiarArte").hidden = true;
  abrir("modalCompartilhar");
  try {
    if (arteConvite) URL.revokeObjectURL(arteConvite.url);
    arteConvite = await prepararArte();
  } catch (e) { arteConvite = null; }
  if (!arteConvite) return;
  $("arteNoConvite").src = arteConvite.url; $("arteNoConvite").hidden = false;
  $("compartilharArte").hidden = !navigator.canShare?.({ files: [arteConvite.jpg] });
  $("copiarArte").hidden = !window.ClipboardItem;
}
$("compartilhar").addEventListener("click", abrirCompartilhar);
$("compartilharTopo").addEventListener("click", abrirCompartilhar);
$("copiarConvite").addEventListener("click", async () => {
  const b = $("copiarConvite");
  try { await navigator.clipboard.writeText($("textoConvite").value); marcar(b, "share.copiado"); }
  catch (e) { $("textoConvite").select(); marcar(b, "share.selecione"); }
});
$("copiarArte").addEventListener("click", () => {
  copiarArte().then(() => marcar($("copiarArte"), "share.arte_copiada"),
    () => marcar($("copiarArte"), "share.arte_falha"));
});
$("compartilharArte").addEventListener("click", async () => {
  try {
    await navigator.share({ files: [arteConvite.jpg], text: $("textoConvite").value, title: t("marca") });
    fechar("modalCompartilhar");
  } catch (e) {}
});
document.querySelectorAll("#modalCompartilhar .rede").forEach((a) => a.addEventListener("click", () => {
  // o link abre normalmente; antes, a arte vai para a área de transferência
  copiarArte().then(() => {
    marcar($("avisoArte"), "share.aviso_colar");
    $("avisoArte").hidden = false;
  }, () => {
    if (!arteConvite) return;
    marcar($("avisoArte"), "share.aviso_anexe");
    $("avisoArte").hidden = false;
  });
}));

// ---------- GitHub e bug bounty ----------
function configurarLinks() {
  document.querySelectorAll("a.github").forEach((a) => {
    if (INFO.repo_url) { a.href = INFO.repo_url; a.removeAttribute("aria-disabled"); a.setAttribute("data-i18n-title", "gh.titulo"); }
    else { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); a.setAttribute("data-i18n-title", "gh.breve"); }
    aplicarEm(a);
  });
  const email = INFO.bounty_email || "";
  $("bountyEmail").textContent = email;
  $("bountyEmail").href = "mailto:" + email + "?subject=" + encodeURIComponent("Bug bounty - Rede de Nós");
  $("bountyPct").textContent = (INFO.bounty_percentual || 70) + "%";
  $("bountyRepo").hidden = !INFO.repo_url;
  if (INFO.repo_url) $("bountyRepo").querySelector("a").href = INFO.repo_url.replace(/\/$/, "") + "/blob/main/SECURITY.md";
}
$("abrirBounty").addEventListener("click", () => abrir("modalBounty"));
$("abrirBounty2").addEventListener("click", () => abrir("modalBounty"));
$("abrirBounty3").addEventListener("click", () => abrir("modalBounty"));

// ---------- reportar problema ----------
function configurarReport() {
  const tipos = INFO.report_tipos || [];
  for (const id of ["abrirReport", "abrirReport2", "reportCartao"]) $(id).hidden = !tipos.length;
  const box = $("reportTipos");
  if (box.dataset.tipos === JSON.stringify(tipos)) return;  // mantém o que a pessoa já marcou
  box.dataset.tipos = JSON.stringify(tipos);
  box.innerHTML = "";
  for (const [chave, rotulo] of tipos) {
    const b = Object.assign(document.createElement("button"), { textContent: rotulo, type: "button" });
    b.dataset.tipo = chave;
    if (temTexto("rep." + chave)) marcar(b, "rep." + chave);  // o rótulo do servidor fica de reserva
    b.addEventListener("click", () => b.classList.toggle("on"));
    box.append(b);
  }
}
function abrirReport() {
  $("reportErro").hidden = !ultimoErro;
  if (ultimoErro) marcar($("reportErro"), "report.erro_junto", { erro: ultimoErro }); else escrever($("reportErro"), "");
  mensagem($("msgReport"), "");
  $("enviarReport").disabled = false;
  abrir("modalReport");
}
for (const id of ["abrirReport", "abrirReport2", "abrirReport3"]) $(id).addEventListener("click", abrirReport);
$("enviarReport").addEventListener("click", async () => {
  const tipos = [...$("reportTipos").querySelectorAll("button.on")].map((b) => b.dataset.tipo);
  const texto = $("reportTexto").value.trim();
  if (!tipos.length && !texto) {
    mensagem($("msgReport"), K("report.marque"), "erro");
    return;
  }
  const b = $("enviarReport");
  b.disabled = true;
  mensagem($("msgReport"), K("report.enviando"));
  try {
    const r = await postJSON("/api/reportar", {
      tipos, mensagem: texto, contato: $("reportContato").value.trim(), erro: ultimoErro,
      retrato: estado.retrato, tela: `${innerWidth}x${innerHeight}`,
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.erro || t("report.falha"));
    $("reportTexto").value = "";
    $("reportTipos").querySelectorAll("button.on").forEach((x) => x.classList.remove("on"));
    mensagem($("msgReport"), K("report.ok"), "sucesso");
  } catch (e) {
    // o erro do próprio envio não deve virar o "último erro" do próximo relato
    $("msgReport").className = "msg erro"; escrever($("msgReport"), e.message);
    b.disabled = false;
  }
});

// ---------- modais ----------
function abrir(id) { $(id).hidden = false; }
function fechar(id) { $(id).hidden = true; if (id === "modalDoar") clearInterval(pollDoacao); if (id === "tutorial") guardar("viu-tutorial", true); }
document.querySelectorAll("[data-fechar]").forEach((b) => b.addEventListener("click", () => fechar(b.dataset.fechar)));
document.querySelectorAll(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) fechar(m.id); }));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".modal:not([hidden])").forEach((m) => fechar(m.id)); });
$("abrirTutorial").addEventListener("click", () => {
  // reinicia a animação das etapas
  $("tutorial").querySelectorAll(".etapas li").forEach((li) => { li.style.animation = "none"; li.offsetHeight; li.style.animation = ""; });
  abrir("tutorial");
});
$("comecar").addEventListener("click", () => fechar("tutorial"));

// ---------- tutorial da logo ----------
const telasLogo = [...document.querySelectorAll("#modalLogo [data-tela]")];
let telaLogo = 0;
telasLogo.forEach((_, i) => {
  const b = document.createElement("button");
  b.dataset.i18nVars = JSON.stringify({ n: i + 1 }); b.setAttribute("data-i18n-aria-label", "logo.passo");
  b.addEventListener("click", () => mostrarTelaLogo(i));
  $("pontosLogo").appendChild(b);
});
function mostrarTelaLogo(i) {
  telaLogo = i;
  telasLogo.forEach((t, j) => { t.hidden = j !== i; });
  [...$("pontosLogo").children].forEach((b, j) => b.classList.toggle("on", j === i));
  $("logoVoltar").disabled = i === 0;
  marcar($("logoAvancar"), i === telasLogo.length - 1 ? "logo.entendi" : "logo.prox");
}
document.querySelectorAll("[data-tutorial-logo]").forEach((b) => b.addEventListener("click", (e) => {
  e.stopPropagation();
  if (!$("tutorial").hidden) fechar("tutorial");
  mostrarTelaLogo(parseInt(b.dataset.tutorialLogo)); abrir("modalLogo");
}));
$("logoVoltar").addEventListener("click", () => mostrarTelaLogo(telaLogo - 1));
$("logoAvancar").addEventListener("click", () => {
  if (telaLogo < telasLogo.length - 1) mostrarTelaLogo(telaLogo + 1); else fechar("modalLogo");
});

// ---------- doações ----------
let pollDoacao = null;
function abrirDoar() {
  $("doarEndereco").hidden = !INFO.doacao_endereco;
  if (INFO.doacao_endereco) { $("qrEndereco").innerHTML = INFO.doacao_qr; $("txtEndereco").textContent = INFO.doacao_endereco; }
  $("doarValores").hidden = !INFO.recompensas;
  $("doarInvoice").hidden = true;
  mensagem($("msgDoar"), "");
  abrir("modalDoar");
}
$("abrirDoar").addEventListener("click", abrirDoar);
$("abrirDoar2").addEventListener("click", abrirDoar);

async function gerarDoacao(sats) {
  if (!(sats >= 1)) return;
  clearInterval(pollDoacao);
  mensagem($("msgDoar"), K("doar.gerando"));
  try {
    const r = await postJSON("/api/doar", { sats });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro);
    $("qrInvoice").innerHTML = j.qr;
    $("txtInvoice").textContent = j.bolt11;
    $("abrirCarteira").href = "lightning:" + j.bolt11;
    $("doarInvoice").hidden = false;
    $("doarEndereco").hidden = true;  // um QR por vez, para não confundir
    mensagem($("msgDoar"), K("doar.aguardando", { n: num(sats) }));
    let tentativas = 0;
    pollDoacao = setInterval(async () => {
      if (++tentativas > 120) { clearInterval(pollDoacao); return; }
      try {
        const s = await (await fetch("/api/doar/" + encodeURIComponent(j.hash))).json();
        if (s.pago) {
          clearInterval(pollDoacao);
          $("doarInvoice").hidden = true;
          mensagem($("msgDoar"), K("doar.obrigado", { n: num(sats) }), "sucesso");
          chuvaDeSats(10);
          carregarInfo();
        }
      } catch (e) {}
    }, 5000);
  } catch (e) { mensagem($("msgDoar"), e.message, "erro"); }
}
document.querySelectorAll("[data-doar]").forEach((b) => b.addEventListener("click", () => gerarDoacao(parseInt(b.dataset.doar))));
$("doarOutroBtn").addEventListener("click", () => gerarDoacao(parseInt($("doarOutro").value)));
document.querySelectorAll("[data-copiar]").forEach((b) => b.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($(b.dataset.copiar).textContent); marcar(b, "doar.copiado"); }
  catch (e) { marcar(b, "doar.selecione"); }
  setTimeout(() => marcar(b, "doar.copiar"), 1500);
}));

// ---------- idioma ----------
function trocarIdioma(l) {
  guardar("idioma", l); definirIdioma(l);
  textosInfo(); atualizarBotao();
  if (!$("modalCompartilhar").hidden) textoConvite();
}
document.querySelectorAll("[data-idioma]").forEach((b) => b.addEventListener("click", () => trocarIdioma(b.dataset.idioma)));

// ---------- início ----------
definirIdioma(idioma);
sincronizar();
const abriuArte = location.pathname.startsWith("/a/");  // link de uma arte: galeria.js carrega e desenha
carregarInfo().then(() => { if (!guardado("viu-tutorial", false) && !abriuArte) abrir("tutorial"); });
if (!abriuArte) renderizar();
