"use strict";
// Moderação da galeria: ver artes recentes/denunciadas e esconder, mostrar ou apagar.
const $ = (id) => document.getElementById(id);
const post = (url, corpo) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corpo) });
let filtro = "denunciadas", antes = 0;

const quando = (ts) => new Date(ts * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
function msg(el, texto, erro = false) { el.textContent = texto; el.className = "msg" + (erro ? " erro" : ""); }

function tag(texto, tipo = "") {
  return Object.assign(document.createElement("span"), { className: "tag " + tipo, textContent: texto });
}

function cartao(i, max) {
  const div = document.createElement("div");
  div.className = "item" + (i.galeria && !i.visivel ? " escondida" : "");
  const a = Object.assign(document.createElement("a"), { href: "/a/" + i.id, target: "_blank", rel: "noopener" });
  a.append(Object.assign(document.createElement("img"), { src: `/a/${i.id}/mini.webp`, alt: "", loading: "lazy" }));
  const corpo = document.createElement("div");
  corpo.className = "corpo";
  const linha = document.createElement("div");
  linha.className = "linha";
  if (i.galeria) linha.append(i.visivel ? tag("visível na galeria", "ok") : tag("escondida da galeria", "ruim"));
  else linha.append(tag("fora da galeria"));
  if (i.denuncias) linha.append(tag(`${i.denuncias} denúncia(s)` + (i.denuncias >= max && !i.aprovado ? " · auto" : ""), "ruim"));
  if (i.oculto) linha.append(tag("escondida por você", "ruim"));
  if (i.aprovado) linha.append(tag("aprovada", "ok"));
  linha.append(tag(i.fonte === "imagem" ? "logo enviada" : "símbolo pronto", "accent"));
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${i.id} · criada ${quando(i.criado)} · último acesso ${quando(i.acesso)}` + (i.nome ? ` · nome: “${i.nome}”` : "");
  const acoes = document.createElement("div");
  acoes.className = "acoes";
  const botao = (texto, acao, classe = "") => {
    const b = Object.assign(document.createElement("button"), { textContent: texto, className: classe });
    b.addEventListener("click", () => moderar(i.id, acao, b));
    return b;
  };
  acoes.append(botao("Esconder", "esconder"), botao("Mostrar", "mostrar"), botao("Apagar", "apagar", "apagar"));
  corpo.append(linha, meta, acoes);
  div.append(a, corpo);
  return div;
}

async function carregar(primeira) {
  if (primeira) { $("grade").innerHTML = ""; antes = 0; }
  msg($("msg"), "Carregando…");
  const r = await fetch(`/admin/galeria/itens?filtro=${filtro}${antes ? "&antes=" + antes : ""}`);
  if (r.status === 403) { mostrarLogin(); return; }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { msg($("msg"), j.erro || "Erro ao carregar.", true); return; }
  $("login").hidden = true; $("painel").hidden = false; $("filtros").hidden = false;
  document.querySelectorAll("[data-filtro]").forEach((b) => b.classList.toggle("on", b.dataset.filtro === filtro));
  $("info").textContent = `Disco: ${j.total_mb} de ${j.teto_mb} MB · ${j.max_denuncias} denúncias escondem uma arte sozinhas (até você mostrar de novo).`;
  for (const i of j.itens) $("grade").append(cartao(i, j.max_denuncias));
  antes = j.itens.length ? j.itens[j.itens.length - 1].criado : 0;
  $("mais").hidden = j.itens.length < 60;
  msg($("msg"), $("grade").children.length ? "" : "Nada por aqui.");
}

async function moderar(id, acao, botao) {
  if (acao === "apagar" && !confirm("Apagar esta arte de vez? O link dela para de funcionar.")) return;
  botao.disabled = true;
  const r = await post("/admin/galeria/moderar", { id, acao });
  const j = await r.json().catch(() => ({}));
  if (r.status === 403) { mostrarLogin(); return; }
  if (!r.ok) { msg($("msg"), j.erro || "Não deu certo.", true); botao.disabled = false; return; }
  carregar(true);
}

function mostrarLogin() {
  $("painel").hidden = true; $("filtros").hidden = true; $("login").hidden = false;
  $("token").focus();
}

$("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await post("/admin/galeria/entrar", { token: $("token").value });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { msg($("msgLogin"), j.erro || "Não deu certo.", true); return; }
  $("token").value = "";
  msg($("msgLogin"), "");
  carregar(true);
});
$("sair").addEventListener("click", async () => { await post("/admin/galeria/sair", {}); mostrarLogin(); });
document.querySelectorAll("[data-filtro]").forEach((b) => b.addEventListener("click", () => { filtro = b.dataset.filtro; carregar(true); }));
$("mais").addEventListener("click", () => carregar(false));
carregar(true);
