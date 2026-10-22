"""Link da própria arte (/a/<id>) e galeria pública com moderação (/admin/galeria).

- POST /api/arte guarda a arte (logo reduzida + parâmetros validados pelo mesmo render da
  prévia) e devolve um link curto. A página /a/<id> é o index.html com Open Graph/Twitter Card
  apontando para /a/<id>/og.jpg (1200×630), então o link vira um cartão bonito ao ser colado.
- A galeria mostra só artes com opt-in explícito. Denúncias (uma por pessoa) escondem a arte
  sozinhas; a moderação esconde, mostra ou apaga.
- Artes sem acesso há ARTES_EXPIRA_DIAS dias são apagadas; ARTES_MAX_MB limita o disco.

Registrado em main.py por `criar_router(...)`, que recebe as peças do servidor (render,
limitadores, leitura de JSON...) para este arquivo não importar main.py.
"""
import asyncio
import hashlib
import html
import io
import json
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image

from . import admin_auth, antiabuso
from .artes import Artes, id_valido
from .eventos import evento
from .i18n import idioma_de
from .render import Excesso, ImagemInvalida, Ocupado, ParametroInvalido, dimensoes

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(RAIZ, "static")

OG_W, OG_H = 1200, 630
MINI_W, MINI_H = 480, 300
POR_PAGINA, MAX_POR_PAGINA = 18, 36
PROTEGIDO_S = 7 * 86400   # quando o disco enche, artes acessadas na última semana nunca saem
LIMPEZA_S = 3600
MAX_NOME = 60
CHAVES_SIMBOLO = {"angulo", "seed", "circulo", "tamanho", "raio", "zoom", "malha", "poeira_longe", "halo",
                  "retrato", "cor", "brilho", "fundo", "multicor", "cores_arte"}
CHAVES_IMAGEM = {"imagem_id", "limiar", "inverter", "cores_off"}
_CONTROLE = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")


def _env_int(nome, padrao):
    try:
        return int(os.environ.get(nome) or padrao)
    except ValueError:
        return padrao


# Textos da pré-visualização do link (Open Graph), no idioma de quem pede (Accept-Language)
OG_TEXTOS = {
    "pt": ("Rede de Nós", "Uma arte da Rede de Nós",
           "A logo vira o vazio no meio de uma rede de nós de luz. Abra, ajuste e faça a sua."),
    "en": ("Node Network", "A Node Network artwork",
           "The logo becomes the void in the middle of a network of glowing nodes. Open it, tweak it and make yours."),
    "de": ("Knotennetz", "Ein Knotennetz-Kunstwerk",
           "Das Logo wird zur Leere inmitten eines Netzes aus leuchtenden Knoten. Öffne es, pass es an und mach dein eigenes."),
}


def _cobrir(im, w, h):
    """Recorte central com a proporção w:h, redimensionado (a logo fica no centro da arte)."""
    iw, ih = im.size
    if iw / ih > w / h:
        nw = round(ih * w / h)
        im = im.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
    else:
        nh = round(iw * h / w)
        im = im.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
    return im.resize((w, h), Image.LANCZOS)


def _bytes(im, fmt, **kw):
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def limpar_nome(v):
    """Nome do arquivo que a pessoa digitou: vira o título do link (sempre escapado no HTML)."""
    if not isinstance(v, str):
        return ""
    return " ".join(_CONTROLE.sub("", v).split())[:MAX_NOME]


def idioma_aceito(req):
    primeiro = (req.headers.get("accept-language") or "").split(",")[0].split(";")[0].strip()
    return idioma_de(primeiro[:20]) if primeiro else "pt"


def meta_og(pagina, *, base, arte_id, nome, idioma):
    """index.html com título, descrição e as tags Open Graph/Twitter Card da arte."""
    marca, titulo_padrao, descricao = OG_TEXTOS[idioma]
    titulo = f"{nome} · {marca}" if nome else titulo_padrao
    url = f"{base}/a/{arte_id}"
    e = lambda s: html.escape(s, quote=True)  # noqa: E731
    tags = "\n".join([
        f'<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{e(marca)}">',
        f'<meta property="og:title" content="{e(titulo)}">',
        f'<meta property="og:description" content="{e(descricao)}">',
        f'<meta property="og:url" content="{e(url)}">',
        f'<meta property="og:image" content="{e(url)}/og.jpg">',
        f'<meta property="og:image:type" content="image/jpeg">',
        f'<meta property="og:image:width" content="{OG_W}">',
        f'<meta property="og:image:height" content="{OG_H}">',
        f'<meta property="og:image:alt" content="{e(titulo)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{e(titulo)}">',
        f'<meta name="twitter:description" content="{e(descricao)}">',
        f'<meta name="twitter:image" content="{e(url)}/og.jpg">',
        f'<link rel="canonical" href="{e(url)}">',
    ])
    pagina = re.sub(r"<title>.*?</title>", lambda _: f"<title>{e(titulo)}</title>", pagina, count=1, flags=re.S)
    pagina = re.sub(r'<meta name="description" content="[^"]*">',
                    lambda _: f'<meta name="description" content="{e(descricao)}">', pagina, count=1)
    return pagina.replace("</head>", tags + "\n</head>", 1)


def criar_router(*, cfg, render, ip_de, limitar, ler_json, Recusado, limites_render, pagina, versao):  # noqa: N803
    """Rotas do link da arte, da galeria e da moderação. Os argumentos vêm de main.py."""
    r = APIRouter()
    artes = Artes(cfg.data_dir)
    expira_s = max(1, _env_int("ARTES_EXPIRA_DIAS", 90)) * 86400
    teto = max(10, _env_int("ARTES_MAX_MB", 1024)) * 1024 * 1024
    max_denuncias = max(1, _env_int("GALERIA_DENUNCIAS_OCULTAR", 3))
    token = admin_auth.token_admin(cfg)
    lim = {
        "criar": antiabuso.Limitador(max(1, _env_int("ARTES_POR_HORA", 20)), 3600),
        "criar_global": antiabuso.Limitador(600, 3600),
        "ler": antiabuso.Limitador(120, 60),
        "arquivo": antiabuso.Limitador(300, 60),
        "galeria": antiabuso.Limitador(60, 60),
        "opt": antiabuso.Limitador(30, 600),
        "denuncia": antiabuso.Limitador(10, 3600),
        "denuncia_global": antiabuso.Limitador(300, 3600),
        "entrar": antiabuso.Limitador(10, 600),
    }
    estado = {"limpo_em": float("-inf")}
    r.artes, r.lim = artes, lim  # para os testes

    def base_url(req):
        return cfg.public_url if cfg.public_url.startswith(("https://", "http://")) else str(req.base_url).rstrip("/")

    async def talvez_limpar():
        if time.monotonic() - estado["limpo_em"] < LIMPEZA_S:
            return
        estado["limpo_em"] = time.monotonic()
        try:
            n = await asyncio.to_thread(artes.limpar, expira_s)
            if n:
                evento("artes_expiradas", quantidade=n)
        except Exception:
            evento("artes_limpeza_falhou", "error")

    r.talvez_limpar = talvez_limpar

    def pegar_ou_404(arte_id):
        a = artes.pegar(arte_id)
        if not a:
            raise Recusado(404, "Essa arte não existe mais.")
        return a

    def garantir_logo(a):
        """Põe a logo guardada de volta no cache do render (quem abre o link pode ajustar)."""
        if a["img_id"] and render.imagens.pegar(a["img_id"]) is None:
            dados = artes.ler_logo(a["img_id"])
            if dados:
                render.imagens.por(a["img_id"], dados)

    # ---------- criar ----------
    def parametros(p):
        """Só os campos que desenham a arte (o render valida cada valor ao desenhar)."""
        fonte = p.get("fonte", "sat")
        chaves = CHAVES_SIMBOLO | (CHAVES_IMAGEM if fonte == "imagem" else set())
        params = {k: v for k, v in p.items() if k in chaves}
        params["fonte"] = fonte
        if fonte == "imagem" and not (isinstance(params.get("imagem_id"), str)
                                      and re.fullmatch(r"[0-9a-f]{24}", params["imagem_id"])):
            raise Recusado(400, "A imagem expirou no servidor. Envie de novo.")
        return params

    def desenhar(params):
        """Na fila do render: a arte uma vez, recortada para o link e para a galeria."""
        retrato = params.get("retrato") is True
        lado = 1920 if retrato else 1280  # o recorte central 1200×630 sai nítido nos dois formatos
        im, _ = render._gerar(params, lado)
        assert im.size == dimensoes(lado, retrato)
        og = _bytes(_cobrir(im, OG_W, OG_H), "JPEG", quality=86, optimize=True, progressive=True)
        mini = _bytes(_cobrir(im, MINI_W, MINI_H), "WEBP", quality=72, method=4)
        logo, cores = None, []
        if params["fonte"] == "imagem":
            logo = render.imagens.pegar(params["imagem_id"])
            if logo is None:
                raise ParametroInvalido("A imagem expirou no servidor. Envie de novo.")
            cores = render.analise(params["imagem_id"], logo).cores()
        return og, mini, logo, cores, retrato

    @r.post("/api/arte")
    async def criar(req: Request):
        ip = ip_de(req)
        limitar(lim["criar"], ip, "Muitos links criados. Espere um pouco.")
        p = await ler_json(req)
        galeria = p.get("galeria", False)
        if not isinstance(galeria, bool):
            raise Recusado(400, "Pedido inválido.")
        params = parametros(p)
        nome = limpar_nome(p.get("nome"))
        ip_hash = antiabuso.hash_ip(cfg.ip_salt, ip)
        assinatura = hashlib.sha256(
            (ip_hash + "|" + nome + "|" + json.dumps(params, sort_keys=True)).encode()).hexdigest()
        existente = artes.por_assinatura(assinatura)
        if existente:  # a mesma pessoa pedindo o link da mesma arte: o mesmo link
            if galeria:
                artes.definir_galeria(existente["id"], existente["chave"], True)
            return {"id": existente["id"], "chave": existente["chave"], "url": f"{base_url(req)}/a/{existente['id']}"}
        limitar(lim["criar_global"], "todos", "Muitos links criados agora. Tente de novo em alguns minutos.")
        await talvez_limpar()
        try:
            og, mini, logo, cores, retrato = await render._na_fila(desenhar, params, limites=limites_render(ip))
        except (ParametroInvalido, ImagemInvalida) as e:
            raise Recusado(400, str(e)) from e
        except Excesso as e:
            raise Recusado(429, "Você já tem desenhos em andamento ou pediu muitos em pouco tempo. "
                                "Espere um pouco.") from e
        except Ocupado as e:
            raise Recusado(503, "Muita gente criando agora. Tente de novo em alguns segundos.") from e
        precisa = len(og) + len(mini) + (len(logo) if logo else 0)
        if not await asyncio.to_thread(artes.liberar, precisa, teto, PROTEGIDO_S):
            evento("artes_disco_cheio", "warning", total=artes.total_bytes(), teto=teto)
            raise Recusado(507, "O espaço para links está cheio agora. Tente mais tarde.")
        arte_id, chave = await asyncio.to_thread(
            lambda: artes.criar(params=params, img_id=params.get("imagem_id"), logo=logo, cores=cores, nome=nome,
                                retrato=retrato, og=og, mini=mini, ip_hash=ip_hash, assinatura=assinatura,
                                galeria=galeria))
        evento("arte_link_criado", id=arte_id[:6], galeria=galeria, bytes=precisa, ip=ip_hash[:12])
        return {"id": arte_id, "chave": chave, "url": f"{base_url(req)}/a/{arte_id}"}

    @r.post("/api/arte/{arte_id}/galeria")
    async def opt_galeria(arte_id: str, req: Request):
        limitar(lim["opt"], ip_de(req))
        p = await ler_json(req)
        ligar = p.get("galeria")
        if not isinstance(ligar, bool) or not id_valido(arte_id):
            raise Recusado(400, "Pedido inválido.")
        if not artes.definir_galeria(arte_id, p.get("chave"), ligar):
            raise Recusado(404, "Essa arte não existe mais.")
        evento("arte_galeria", id=arte_id[:6], galeria=ligar)
        return {"ok": True, "galeria": ligar}

    # ---------- abrir ----------
    @r.get("/api/arte/{arte_id}")
    async def ler(arte_id: str, req: Request):
        limitar(lim["ler"], ip_de(req))
        a = pegar_ou_404(arte_id)
        garantir_logo(a)
        artes.tocar(arte_id)
        return JSONResponse({"id": arte_id, "estado": a["params"], "cores": a["cores"], "nome": a["nome"],
                             "logo": bool(a["img_id"])}, headers={"Cache-Control": "no-store"})

    @r.get("/a/{arte_id}")
    async def pagina_arte(arte_id: str, req: Request):
        limitar(lim["ler"], ip_de(req))
        a = artes.pegar(arte_id)
        texto = pagina()
        if not a:  # a página abre normalmente e avisa que o link expirou
            return HTMLResponse(texto, status_code=404, headers={"Cache-Control": "no-store"})
        artes.tocar(arte_id)
        texto = meta_og(texto, base=base_url(req), arte_id=arte_id, nome=a["nome"], idioma=idioma_aceito(req))
        return HTMLResponse(texto, headers={"Cache-Control": "no-store"})

    def arquivo(arte_id, req, tipo):
        limitar(lim["arquivo"], ip_de(req))
        a = artes.pegar(arte_id)
        if not a:
            raise HTTPException(404)
        if tipo == "logo":
            if not a["img_id"]:
                raise HTTPException(404)
            caminho, mime = artes.caminho_logo(a["img_id"]), "image/png"
        else:
            caminho, mime = artes.caminho(arte_id, tipo), {"og": "image/jpeg", "mini": "image/webp"}[tipo]
        if not os.path.isfile(caminho):
            raise HTTPException(404)
        return FileResponse(caminho, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})

    @r.get("/a/{arte_id}/og.jpg")
    async def og_jpg(arte_id: str, req: Request):
        resp = arquivo(arte_id, req, "og")
        artes.tocar(arte_id)  # quem busca a pré-visualização também mantém o link vivo
        return resp

    @r.get("/a/{arte_id}/mini.webp")
    async def mini_webp(arte_id: str, req: Request):
        return arquivo(arte_id, req, "mini")

    @r.get("/a/{arte_id}/logo.png")
    async def logo_png(arte_id: str, req: Request):
        return arquivo(arte_id, req, "logo")

    @r.get("/galeria.js")
    async def galeria_js():
        return FileResponse(os.path.join(STATIC, "galeria.js"), media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    # ---------- galeria ----------
    @r.get("/api/galeria")
    async def listar(req: Request, antes: float = 0, n: int = POR_PAGINA):
        limitar(lim["galeria"], ip_de(req))
        n = max(1, min(n, MAX_POR_PAGINA))
        antes = antes if 0 < antes < 1e11 else 1e11
        itens = await asyncio.to_thread(artes.galeria, antes, n + 1, max_denuncias)
        mais = itens[n - 1]["galeria_em"] if len(itens) > n else None
        await talvez_limpar()
        return JSONResponse({"itens": [{"id": i["id"], "retrato": bool(i["retrato"])} for i in itens[:n]],
                             "mais": mais}, headers={"Cache-Control": "no-store"})

    @r.post("/api/galeria/denunciar")
    async def denunciar(req: Request):
        ip = ip_de(req)
        limitar(lim["denuncia"], ip, "Você já mandou várias denúncias. Espere um pouco.")
        p = await ler_json(req)
        arte_id = p.get("id")
        if not id_valido(arte_id):
            raise Recusado(400, "Pedido inválido.")
        limitar(lim["denuncia_global"], "todos", "Muitas denúncias agora. Tente de novo mais tarde.")
        ip_hash = antiabuso.hash_ip(cfg.ip_salt, ip)
        nova = artes.denunciar(arte_id, ip_hash)
        if nova is None:
            raise Recusado(404, "Essa arte não existe mais.")
        if nova:
            evento("arte_denunciada", "warning", id=arte_id[:6], ip=ip_hash[:12])
        return {"ok": True}

    # ---------- moderação (/admin/galeria) ----------
    def exigir_token():
        if not token:  # sem ADMIN_TOKEN a moderação nem existe
            raise HTTPException(404)

    def exigir_admin(req):
        exigir_token()
        if not admin_auth.admin_ok(req, token):
            raise Recusado(403, "Entre com o token de admin.")

    sem_cache = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}

    @r.get("/admin/galeria")
    async def pagina_admin():
        exigir_token()
        with open(os.path.join(STATIC, "admin-galeria.html"), encoding="utf-8") as f:
            texto = f.read().replace('src="/admin/galeria.js"', f'src="/admin/galeria.js?v={versao("admin-galeria.js")}"')
        return HTMLResponse(texto, headers=sem_cache)

    @r.get("/admin/galeria.js")
    async def admin_js():
        exigir_token()
        return FileResponse(os.path.join(STATIC, "admin-galeria.js"), media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @r.post("/admin/galeria/entrar")
    async def entrar(req: Request):
        exigir_token()
        ip = ip_de(req)
        limitar(lim["entrar"], ip, "Muitas tentativas. Espere alguns minutos.")
        p = await ler_json(req)
        if not admin_auth.confere(p.get("token"), token):
            evento("admin_galeria_login_falhou", "warning", ip=antiabuso.hash_ip(cfg.ip_salt, ip)[:12])
            raise Recusado(403, "Token errado.")
        resp = JSONResponse({"ok": True}, headers=sem_cache)
        resp.headers["Set-Cookie"] = admin_auth.cabecalho_cookie(token, base_url(req).startswith("https://"))
        return resp

    @r.post("/admin/galeria/sair")
    async def sair():
        exigir_token()
        resp = JSONResponse({"ok": True}, headers=sem_cache)
        resp.headers["Set-Cookie"] = "admin=; Path=/admin; HttpOnly; SameSite=Strict; Max-Age=0"
        return resp

    @r.get("/admin/galeria/itens")
    async def itens_admin(req: Request, filtro: str = "recentes", antes: float = 0):
        exigir_admin(req)
        if filtro not in ("recentes", "galeria", "denunciadas", "ocultas"):
            raise Recusado(400, "Pedido inválido.")
        antes = antes if 0 < antes < 1e11 else 1e11
        itens = await asyncio.to_thread(artes.listar_admin, filtro, antes, 60, max_denuncias)
        for i in itens:
            i["visivel"] = bool(i["galeria"] and not i["oculto"] and (i["aprovado"] or i["denuncias"] < max_denuncias))
            i["fonte"] = json.loads(i.pop("params")).get("fonte")
        return JSONResponse({"itens": itens, "max_denuncias": max_denuncias, "total_mb": round(artes.total_bytes() / 2**20, 1),
                             "teto_mb": teto // 2**20}, headers=sem_cache)

    @r.post("/admin/galeria/moderar")
    async def moderar(req: Request):
        exigir_admin(req)
        p = await ler_json(req)
        acao, arte_id = p.get("acao"), p.get("id")
        if acao not in ("esconder", "mostrar", "apagar") or not id_valido(arte_id):
            raise Recusado(400, "Pedido inválido.")
        if not await asyncio.to_thread(artes.moderar, arte_id, acao):
            raise Recusado(404, "Essa arte não existe mais.")
        evento("admin_galeria", acao=acao, id=arte_id[:6])
        return {"ok": True}

    return r
