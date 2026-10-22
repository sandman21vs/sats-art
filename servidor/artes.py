"""Artes guardadas para o link curto /a/<id> e a galeria pública.

Tudo fica em DATA_DIR:
- artes.db: uma linha por arte (parâmetros já validados, quando foi criada e acessada,
  se está na galeria, denúncias e moderação);
- artes/<id>.jpg: imagem 1200×630 para a pré-visualização do link (Open Graph);
- artes/<id>.webp: miniatura leve da galeria;
- artes/logos/<img_id>.png: a logo enviada, já reduzida pelo servidor (uma por imagem,
  compartilhada pelas artes que usam a mesma logo).

Os ids são aleatórios (96 bits) e só entram em caminhos depois de conferidos por regex.
"""
import json
import os
import re
import secrets
import sqlite3
import threading
import time

ID = re.compile(r"[A-Za-z0-9_-]{16}")
IMG_ID = re.compile(r"[0-9a-f]{24}")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS artes (
    id TEXT PRIMARY KEY,
    chave TEXT NOT NULL,              -- segredo de quem criou (liga/desliga a galeria)
    criado REAL NOT NULL, acesso REAL NOT NULL,
    img_id TEXT,                      -- logo enviada (NULL = símbolo pronto)
    params TEXT NOT NULL, cores TEXT NOT NULL DEFAULT '[]', nome TEXT NOT NULL DEFAULT '',
    retrato INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
    ip_hash TEXT NOT NULL, assinatura TEXT NOT NULL,
    galeria INTEGER NOT NULL DEFAULT 0, galeria_em REAL,
    oculto INTEGER NOT NULL DEFAULT 0,   -- escondida pela moderação
    aprovado INTEGER NOT NULL DEFAULT 0, -- mostrada pela moderação: denúncias não escondem mais
    denuncias INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS artes_acesso ON artes(acesso);
CREATE INDEX IF NOT EXISTS artes_galeria ON artes(galeria, galeria_em);
CREATE UNIQUE INDEX IF NOT EXISTS artes_assinatura ON artes(assinatura);
CREATE TABLE IF NOT EXISTS denuncias (
    arte TEXT NOT NULL, ip_hash TEXT NOT NULL, criado REAL NOT NULL, PRIMARY KEY (arte, ip_hash)
);
"""

TOQUE_S = 86400  # o último acesso é gravado no máximo uma vez por dia por arte


def id_valido(v):
    return isinstance(v, str) and ID.fullmatch(v) is not None


def _gravar(caminho, dados):
    tmp = f"{caminho}.{secrets.token_hex(4)}.tmp"
    with open(tmp, "wb") as f:
        f.write(dados)
    os.replace(tmp, caminho)


def _apagar(caminho):
    try:
        os.remove(caminho)
    except FileNotFoundError:
        pass


class Artes:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "artes")
        self.dir_logos = os.path.join(self.dir, "logos")
        os.makedirs(self.dir_logos, exist_ok=True)
        self.con = sqlite3.connect(os.path.join(data_dir, "artes.db"), check_same_thread=False,
                                   isolation_level=None, timeout=15)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=15000")
        self.con.executescript(ESQUEMA)
        self.trava = threading.Lock()

    def _q(self, sql, *args):
        with self.trava:
            return self.con.execute(sql, args)

    def _um(self, sql, *args):
        with self.trava:
            c = self.con.execute(sql, args)
            linha = c.fetchone()
            return dict(zip([d[0] for d in c.description], linha)) if linha else None

    def _todos(self, sql, *args):
        with self.trava:
            c = self.con.execute(sql, args)
            nomes = [d[0] for d in c.description]
            return [dict(zip(nomes, linha)) for linha in c.fetchall()]

    # ---------- arquivos ----------
    def caminho(self, arte_id, tipo):
        """tipo: 'og' (JPEG 1200×630) ou 'mini' (WebP da galeria)."""
        if not id_valido(arte_id):
            raise ValueError("id inválido")
        return os.path.join(self.dir, arte_id + {"og": ".jpg", "mini": ".webp"}[tipo])

    def caminho_logo(self, img_id):
        if not (isinstance(img_id, str) and IMG_ID.fullmatch(img_id)):
            raise ValueError("img_id inválido")
        return os.path.join(self.dir_logos, img_id + ".png")

    def ler_logo(self, img_id):
        try:
            with open(self.caminho_logo(img_id), "rb") as f:
                return f.read()
        except (OSError, ValueError):
            return None

    # ---------- criar e ler ----------
    def por_assinatura(self, assinatura):
        return self._um("SELECT id, chave FROM artes WHERE assinatura = ?", assinatura)

    def criar(self, *, params, img_id, logo, cores, nome, retrato, og, mini, ip_hash, assinatura, galeria):
        """Grava os arquivos e a linha. Devolve (id, chave)."""
        arte_id, chave = secrets.token_urlsafe(12), secrets.token_urlsafe(18)
        if img_id is not None:
            caminho_logo = self.caminho_logo(img_id)
            if not os.path.exists(caminho_logo):
                _gravar(caminho_logo, logo)
        _gravar(self.caminho(arte_id, "og"), og)
        _gravar(self.caminho(arte_id, "mini"), mini)
        agora = time.time()
        tam = len(og) + len(mini) + (len(logo) if logo else 0)
        try:
            self._q("INSERT INTO artes(id, chave, criado, acesso, img_id, params, cores, nome, retrato, bytes, "
                    "ip_hash, assinatura, galeria, galeria_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    arte_id, chave, agora, agora, img_id, json.dumps(params, separators=(",", ":")),
                    json.dumps(cores, separators=(",", ":")), nome, int(retrato), tam, ip_hash, assinatura,
                    int(galeria), agora if galeria else None)
        except sqlite3.IntegrityError:  # o mesmo pedido chegou duas vezes ao mesmo tempo
            _apagar(self.caminho(arte_id, "og"))
            _apagar(self.caminho(arte_id, "mini"))
            existente = self.por_assinatura(assinatura)
            return existente["id"], existente["chave"]
        return arte_id, chave

    def pegar(self, arte_id):
        if not id_valido(arte_id):
            return None
        a = self._um("SELECT * FROM artes WHERE id = ?", arte_id)
        if a:
            a["params"] = json.loads(a["params"])
            a["cores"] = json.loads(a["cores"])
        return a

    def tocar(self, arte_id):
        """Marca o acesso (a expiração conta a partir do último acesso)."""
        agora = time.time()
        self._q("UPDATE artes SET acesso = ? WHERE id = ? AND acesso < ?", agora, arte_id, agora - TOQUE_S)

    def definir_galeria(self, arte_id, chave, ligar):
        """Só quem criou (tem a chave) liga ou desliga. Devolve False se a chave não confere."""
        a = self.pegar(arte_id)
        if not a or not isinstance(chave, str) or not secrets.compare_digest(a["chave"].encode(), chave.encode()):
            return False
        if bool(a["galeria"]) != ligar:
            self._q("UPDATE artes SET galeria = ?, galeria_em = ? WHERE id = ?",
                    int(ligar), time.time() if ligar else None, arte_id)
        return True

    # ---------- galeria ----------
    def galeria(self, antes, n, max_denuncias):
        """Itens visíveis, do mais novo para o mais antigo: opt-in, não escondidos e (sem
        aprovação da moderação) com menos de `max_denuncias` denúncias."""
        return self._todos(
            "SELECT id, retrato, galeria_em FROM artes WHERE galeria = 1 AND oculto = 0 "
            "AND (aprovado = 1 OR denuncias < ?) AND galeria_em < ? ORDER BY galeria_em DESC LIMIT ?",
            max_denuncias, antes, n)

    def denunciar(self, arte_id, ip_hash):
        """Uma denúncia por pessoa por arte. Devolve None se a arte não existe."""
        if not self.pegar(arte_id):
            return None
        with self.trava:
            c = self.con
            c.execute("BEGIN IMMEDIATE")
            try:
                nova = c.execute("INSERT OR IGNORE INTO denuncias(arte, ip_hash, criado) VALUES (?,?,?)",
                                 (arte_id, ip_hash, time.time())).rowcount
                if nova:
                    c.execute("UPDATE artes SET denuncias = denuncias + 1 WHERE id = ?", (arte_id,))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return bool(nova)

    # ---------- moderação ----------
    def listar_admin(self, filtro, antes, n, max_denuncias):
        onde = {
            "recentes": "1",
            "galeria": "galeria = 1",
            "denunciadas": "denuncias > 0",
            "ocultas": f"galeria = 1 AND (oculto = 1 OR (aprovado = 0 AND denuncias >= {int(max_denuncias)}))",
        }[filtro]
        return self._todos(
            f"SELECT id, criado, acesso, nome, retrato, galeria, oculto, aprovado, denuncias, img_id, "
            f"params FROM artes WHERE {onde} AND criado < ? ORDER BY criado DESC LIMIT ?", antes, n)

    def moderar(self, arte_id, acao):
        if acao == "apagar":
            return self.apagar(arte_id)
        sql = {"esconder": "UPDATE artes SET oculto = 1, aprovado = 0 WHERE id = ?",
               "mostrar": "UPDATE artes SET oculto = 0, aprovado = 1 WHERE id = ?"}[acao]
        return self._q(sql, arte_id).rowcount > 0

    def apagar(self, arte_id):
        a = self.pegar(arte_id)
        if not a:
            return False
        self._q("DELETE FROM artes WHERE id = ?", arte_id)
        self._q("DELETE FROM denuncias WHERE arte = ?", arte_id)
        _apagar(self.caminho(arte_id, "og"))
        _apagar(self.caminho(arte_id, "mini"))
        if a["img_id"] and not self._um("SELECT 1 AS x FROM artes WHERE img_id = ?", a["img_id"]):
            _apagar(self.caminho_logo(a["img_id"]))
        return True

    # ---------- espaço e expiração ----------
    def total_bytes(self):
        return self._um("SELECT COALESCE(SUM(bytes), 0) AS t FROM artes")["t"]

    def limpar(self, expira_s):
        """Apaga as artes sem acesso há mais de `expira_s` e arquivos órfãos. Devolve quantas."""
        velhas = [a["id"] for a in self._todos("SELECT id FROM artes WHERE acesso < ?", time.time() - expira_s)]
        for arte_id in velhas:
            self.apagar(arte_id)
        self._orfaos()
        return len(velhas)

    def liberar(self, precisa, teto, protegido_s):
        """Abre espaço apagando as artes acessadas há mais tempo, mas nunca as acessadas nos
        últimos `protegido_s`. Devolve True se couber `precisa` bytes abaixo do `teto`."""
        while self.total_bytes() + precisa > teto:
            a = self._um("SELECT id FROM artes WHERE acesso < ? ORDER BY acesso LIMIT 1", time.time() - protegido_s)
            if not a:
                return False
            self.apagar(a["id"])
        return True

    def _orfaos(self):
        """Arquivos sem linha no banco (ex.: o processo caiu entre gravar o arquivo e a linha)."""
        limite = time.time() - 3600
        ids = {a["id"] for a in self._todos("SELECT id FROM artes")}
        logos = {a["img_id"] for a in self._todos("SELECT DISTINCT img_id FROM artes WHERE img_id IS NOT NULL")}
        for pasta, conhecidos in ((self.dir, ids), (self.dir_logos, logos)):
            for nome in os.listdir(pasta):
                caminho = os.path.join(pasta, nome)
                if not os.path.isfile(caminho):
                    continue
                base = nome.split(".")[0]
                orfao = base not in conhecidos or nome.endswith(".tmp")
                try:
                    if orfao and os.path.getmtime(caminho) < limite:
                        os.remove(caminho)
                except OSError:
                    pass
