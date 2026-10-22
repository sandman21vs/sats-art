"use strict";
const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("pt-BR");

function mostrar(logado) {
  $("entrar").hidden = logado;
  $("painel").hidden = !logado;
  if (!logado) $("token").focus();
}

function barras(alvo, dias, campo, classe) {
  const max = Math.max(1, ...dias.map((d) => d[campo]));
  alvo.replaceChildren();
  const colunas = document.createElement("div");
  colunas.className = "barras";
  const eixo = document.createElement("div");
  eixo.className = "eixo";
  for (const d of dias) {
    const col = document.createElement("div");
    col.className = "col";
    col.title = `${d.dia}: ${fmt.format(d[campo])}`;
    const v = document.createElement("span");
    v.className = "v";
    v.textContent = fmt.format(d[campo]);
    const b = document.createElement("div");
    b.className = "b " + classe;
    b.style.height = Math.round((d[campo] / max) * 90) + "%";
    col.append(v, b);
    colunas.append(col);
    const e = document.createElement("span");
    e.textContent = d.dia.slice(8);
    eixo.append(e);
  }
  alvo.append(colunas, eixo);
}

function lista(alvo, itens, vazio, montar) {
  alvo.replaceChildren();
  if (!itens.length) {
    const p = document.createElement("div");
    p.className = "muted";
    p.textContent = vazio;
    alvo.append(p);
    return;
  }
  for (const i of itens) alvo.append(montar(i));
}

async function carregar() {
  const r = await fetch("/api/admin/resumo", { credentials: "same-origin" });
  if (r.status === 401) return mostrar(false);
  if (!r.ok) return;
  const d = await r.json();
  mostrar(true);
  const pote = $("pote");
  pote.textContent = d.pote == null ? "–" : fmt.format(d.pote) + " sats";
  pote.classList.toggle("baixo", d.pote != null && d.alerta_pote_sats > 0 && d.pote < d.alerta_pote_sats);
  $("poteNota").textContent = d.recompensas ? (d.alerta_pote_sats ? `alerta abaixo de ${fmt.format(d.alerta_pote_sats)}` : "") : "recompensas desligadas";
  $("totalArtes").textContent = fmt.format(d.total_artes);
  $("totalResgates").textContent = fmt.format(d.total_resgates);
  $("totalSats").textContent = fmt.format(d.total_sats);
  $("pausa").textContent = d.pausadas ? "Recompensas pausadas hoje" : "";
  barras($("gArtes"), d.dias, "artes", "");
  barras($("gSats"), d.dias, "sats", "sats");
  lista($("erros"), d.erros, "Nenhum erro nas últimas 24 h.", (e) => {
    const t = document.createElement("table");
    const tr = t.insertRow();
    tr.insertCell().textContent = e.erro;
    const n = tr.insertCell();
    n.className = "n";
    n.textContent = fmt.format(e.n) + "×";
    return t;
  });
  lista($("relatos"), d.relatos, "Nenhum relato guardado.", (x) => {
    const el = document.createElement("div");
    el.className = "relato";
    const q = document.createElement("div");
    q.className = "muted";
    q.textContent = x.quando;
    const t = document.createElement("div");
    t.textContent = x.texto;
    el.append(q, t);
    return el;
  });
}

$("formEntrar").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("erroEntrar").textContent = "";
  try {
    const r = await fetch("/api/admin/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: $("token").value }),
    });
    if (r.ok) {
      $("token").value = "";
      return carregar();
    }
    $("erroEntrar").textContent = (await r.json().catch(() => ({}))).erro || "Não consegui entrar.";
  } catch {
    $("erroEntrar").textContent = "Sem conexão com o servidor.";
  }
});
$("atualizar").addEventListener("click", carregar);
$("sair").addEventListener("click", async () => {
  await fetch("/api/admin/sair", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: "{}" });
  mostrar(false);
});
carregar();
