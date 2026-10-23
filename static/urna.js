// Urna dos Sats: vote '13 ou '22 e receba esse tanto de sats. O placar só vem do servidor
// depois do voto (veja servidor/urna.py).
"use strict";
const $ = (id) => document.getElementById(id);
const postJSON = (url, corpo) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Idioma": "pt" }, body: JSON.stringify(corpo) });
const guardar = (k, v) => { try { localStorage.setItem(k, v); } catch { /* sem armazenamento */ } };
const lembrar = (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
const mensagem = (el, texto, tipo = "") => { el.className = "msg " + tipo; el.textContent = texto; };
const NUMEROS = ["13", "22"];

let digitado = "", branco = false, votou = false, enviando = false, novo = -1, meuNumero = "";
let ESTADO = null, token = null;

// ---------- som (bipe das teclas e o "pi-pi-pi-piii" do fim) ----------
let audio = null;
function bipe(freq, inicio, dur) {
  try {
    audio = audio || new AudioContext();
    const o = audio.createOscillator(), g = audio.createGain(), t0 = audio.currentTime + inicio;
    o.type = "square"; o.frequency.value = freq;
    g.gain.setValueAtTime(0.04, t0); g.gain.setValueAtTime(0, t0 + dur);
    o.connect(g).connect(audio.destination); o.start(t0); o.stop(t0 + dur + 0.02);
  } catch { /* sem áudio */ }
}
const somTecla = () => bipe(1100, 0, 0.07);
const somFim = () => { for (let i = 0; i < 4; i++) bipe(1150, i * 0.14, 0.1); bipe(1150, 0.56, 0.6); };

// ---------- tela ----------
function el(tag, cls, texto) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (texto != null) e.textContent = texto;
  return e;
}

function desenhar() {
  const tela = $("tela");
  tela.replaceChildren();
  if (votou) {
    tela.append(el("div", "fim", "FIM"));
    if (meuNumero) {
      const c = el("div", "seu-voto");
      c.append(el("small", "", "SEU VOTO"), el("b", "", "'" + meuNumero));
      tela.append(c);
    }
    return;
  }
  tela.append(el("div", "cargo-topo", "SEU VOTO PARA"), el("div", "cargo", "PRESIDENTE DOS SATS"));
  if (branco) {
    tela.append(el("div", "aviso", "VOTO EM BRANCO"), el("div", "sub", "Voto em branco não rende sats."));
  } else {
    const linha = el("div", "linha");
    linha.append(el("span", "", "Número:"));
    const caixas = el("div", "digitos");
    for (let i = 0; i < 2; i++) {
      caixas.append(el("div", "digito" + (i === digitado.length ? " atual" : "") + (i === novo ? " novo" : ""), digitado[i] || ""));
    }
    novo = -1;
    linha.append(caixas);
    tela.append(linha);
    if (digitado.length === 2) {
      if (NUMEROS.includes(digitado)) {
        tela.append(el("div", "linha", `Nome: Candidato '${digitado}`), el("div", "linha", `Você ganha: ${digitado} sats`));
      } else {
        tela.append(el("div", "aviso", "NÚMERO ERRADO"), el("div", "sub", "Só vale '13 ou '22. Voto nulo não rende sats."));
      }
    }
  }
  if (digitado.length === 2 || branco) {
    const r = el("div", "rodape");
    r.append(el("div", "", "Aperte a tecla:"), el("div", "", "VERDE para CONFIRMAR este voto"),
      el("div", "", "LARANJA para REINICIAR este voto"));
    tela.append(r);
  }
  $("confirma").disabled = !(digitado.length === 2 && NUMEROS.includes(digitado)) || enviando;
}

// ---------- teclas: afundam, acendem e soltam uma onda de onde foram tocadas ----------
function apertar(botao, ev) {
  if (!botao || botao.disabled) return;
  botao.classList.remove("apertada");
  void botao.offsetWidth;  // reinicia a animação se a tecla for apertada de novo logo em seguida
  botao.classList.add("apertada");
  setTimeout(() => botao.classList.remove("apertada"), 160);
  const r = botao.getBoundingClientRect(), lado = Math.max(r.width, r.height);
  const x = ev && ev.clientX ? ev.clientX - r.left : r.width / 2, y = ev && ev.clientY ? ev.clientY - r.top : r.height / 2;
  const o = el("span", "onda");
  Object.assign(o.style, { width: lado + "px", height: lado + "px", left: x - lado / 2 + "px", top: y - lado / 2 + "px" });
  botao.append(o);
  setTimeout(() => o.remove(), 520);
}
function efeitoTela(classe) {
  const tela = $("tela");
  tela.classList.remove(classe);
  void tela.offsetWidth;
  tela.classList.add(classe);
}

function tecla(n) {
  if (votou || branco || digitado.length >= 2) return;
  somTecla(); novo = digitado.length; digitado += n; desenhar();
}
function corrige() {
  if (votou) return;
  somTecla();
  if (digitado || branco) efeitoTela("treme");
  digitado = ""; branco = false; desenhar();
}
function emBranco() { if (votou || digitado) return; somTecla(); branco = true; desenhar(); }

async function confirma() {
  if (votou || enviando || !NUMEROS.includes(digitado)) return;
  enviando = true; desenhar();
  try {
    const r = await postJSON("/api/urna/votar", { numero: digitado });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || "Não consegui registrar o voto.");
    somFim();
    efeitoTela("flash");
    mostrarVoto(j);
  } catch (e) {
    alert(e.message);
  } finally {
    enviando = false; desenhar();
  }
}

$("numeros").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) { apertar(b, e); tecla(b.dataset.n); }
});
$("corrige").addEventListener("click", (e) => { apertar($("corrige"), e); corrige(); });
$("branco").addEventListener("click", (e) => { apertar($("branco"), e); emBranco(); });
$("confirma").addEventListener("click", (e) => { apertar($("confirma"), e); confirma(); });
// o teclado do computador também "aperta" a tecla da urna na tela
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.metaKey || e.ctrlKey || e.altKey || e.repeat) return;
  if (/^[0-9]$/.test(e.key)) { if (!votou) apertar(document.querySelector(`#numeros [data-n="${e.key}"]`)); tecla(e.key); }
  else if (e.key === "Backspace") { if (!votou) apertar($("corrige")); corrige(); }
  else if (e.key === "Enter") { e.preventDefault(); apertar($("confirma")); confirma(); }
});

// ---------- depois do voto ----------
function mostrarVoto(j) {
  votou = true; ESTADO = { ...ESTADO, ...j }; token = j.token; meuNumero = j.numero;
  desenhar();
  $("numeroVoto").textContent = "'" + j.numero;
  $("textoVoto").textContent = "'" + j.numero;
  $("cardVoto").hidden = false;
  for (const b of document.querySelectorAll(".teclado button")) b.disabled = true;
  placar(j.placar, j.numero);
  if (token) {
    $("valorVoto").textContent = j.sats;
    $("receber").textContent = `Receber ${j.sats} sats`;
    $("ln").value = lembrar("ln-endereco");
    $("cardResgate").hidden = false;
    $("cardSemSats").hidden = true;
  } else {
    $("cardResgate").hidden = true;
    $("textoSemSats").textContent = ESTADO.recompensas
      ? "Os sats deste voto já foram resgatados (ou o tempo para resgatar acabou)."
      : "Os sats estão desligados neste servidor, mas seu voto entrou na apuração.";
    $("cardSemSats").hidden = false;
  }
}

function placar(p, meu) {
  if (!p) return;
  $("apuracao").classList.remove("travada");
  $("cadeado").hidden = true;
  const total = p.total || 0;
  $("total").textContent = `${total} ${total === 1 ? "voto apurado" : "votos apurados"}`;
  const lider = p["13"] === p["22"] ? null : (p["13"] > p["22"] ? "13" : "22");
  for (const n of NUMEROS) {
    const pct = total ? (100 * p[n]) / total : 0;
    $("pct" + n).textContent = pct.toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + "%";
    $("barra" + n).style.width = pct + "%";
    $("votos" + n).textContent = `${p[n]} ${p[n] === 1 ? "voto" : "votos"}`;
    $("c" + n).classList.toggle("lider", lider === n);
    $("c" + n).classList.toggle("meu", meu === n);
  }
  if (meu) $("meuVoto").textContent = `Seu voto: '${meu}`;
}

// a apuração continua andando enquanto a página estiver aberta
setInterval(async () => {
  if (!votou || document.hidden) return;
  try {
    const r = await fetch("/api/urna");
    if (r.ok) { const j = await r.json(); if (j.votou) placar(j.placar, j.numero); }
  } catch { /* tenta de novo no próximo ciclo */ }
}, 20000);

// ---------- resgate ----------
$("receber").addEventListener("click", receber);
$("ln").addEventListener("keydown", (e) => { if (e.key === "Enter") receber(); });

async function receber() {
  const endereco = $("ln").value.trim(), msg = $("msgResgate");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(endereco)) {
    $("ln").focus(); mensagem(msg, "Digite seu endereço Lightning, por exemplo voce@coinos.io.", "erro");
    return;
  }
  guardar("ln-endereco", endereco);
  $("receber").disabled = true;
  try {
    mensagem(msg, "Conferindo que você é humano…");
    const prova = await provaDeTrabalho();
    mensagem(msg, "Enviando os sats…");
    const r = await postJSON("/api/urna/resgatar", { token, endereco, prova });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || "Não consegui enviar os sats.");
    $("formResgate").hidden = true;
    mensagem(msg, `Pronto! ${j.sats} sats a caminho da sua carteira.`, "sucesso");
    chuvaDeSats(j.sats);
  } catch (e) {
    mensagem(msg, e.message, "erro");
    $("receber").disabled = false;
  }
}

function chuvaDeSats(n) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  for (let i = 0; i < Math.min(n * 2, 30); i++) {
    const s = el("span", "cai", "⚡");
    s.style.left = Math.random() * 100 + "vw";
    s.style.animationDelay = Math.random() * 0.8 + "s";
    s.style.fontSize = 14 + Math.random() * 18 + "px";
    document.body.appendChild(s);
    setTimeout(() => s.remove(), 3600);
  }
}

// ---------- prova de trabalho (anti-robô), roda num worker ----------
// Cópia da de static/app.js: a urna é temporária e fica toda em arquivos próprios.
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
  const rd = await fetch("/api/desafio", { headers: { "X-Idioma": "pt" } });
  const d = await rd.json();
  if (!rd.ok) throw new Error(d.erro || "Não consegui fazer a verificação anti-robô.");
  const worker = new Worker(URL.createObjectURL(new Blob([POW_WORKER], { type: "text/javascript" })));
  const nonce = await new Promise((ok) => { worker.onmessage = (e) => ok(e.data); worker.postMessage(d); });
  worker.terminate();
  return { ...d, nonce };
}

// ---------- início ----------
(async () => {
  desenhar();
  try {
    const r = await fetch("/api/urna");
    ESTADO = await r.json();
    if (ESTADO.votou) mostrarVoto(ESTADO);
  } catch { ESTADO = { recompensas: false }; }
})();
