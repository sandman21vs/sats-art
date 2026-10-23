"""SQLite: tokens de resgate, resgates, provas de trabalho usadas e doações.

A reserva de um resgate (conferir limites + consumir o token + gravar "pendente")
acontece numa única transação BEGIN IMMEDIATE, então vale mesmo com vários
processos ou containers usando o mesmo arquivo.
"""
import os
import sqlite3
import threading
import time

ESQUEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY, criado REAL NOT NULL, usado INTEGER NOT NULL DEFAULT 0,
    dono TEXT,  -- hash da cota de quem exportou: só essa "pessoa" pode resgatar
    tipo TEXT NOT NULL DEFAULT 'arte',  -- arte | urna: cada tipo tem limites próprios
    sats INTEGER                        -- valor fixo do resgate (urna); NULL = segue REWARD_SCHEDULE
);
CREATE TABLE IF NOT EXISTS resgates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dia TEXT NOT NULL, endereco TEXT NOT NULL, ip_hash TEXT NOT NULL,
    sats INTEGER NOT NULL, status TEXT NOT NULL,   -- pendente | pago | erro | falhou (não enviado)
    ref TEXT, fee INTEGER DEFAULT 0, erro TEXT, criado REAL NOT NULL, fechado REAL,
    tipo TEXT NOT NULL DEFAULT 'arte'
);
CREATE INDEX IF NOT EXISTS resgates_dia ON resgates(dia);
CREATE INDEX IF NOT EXISTS resgates_status ON resgates(status);
CREATE TABLE IF NOT EXISTS pow_usados (salt TEXT PRIMARY KEY, criado REAL NOT NULL);
CREATE TABLE IF NOT EXISTS doacoes (
    hash TEXT PRIMARY KEY, sats INTEGER NOT NULL, pago INTEGER NOT NULL DEFAULT 0, criado REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS estado (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
-- contadores baratos para o painel: artes por dia ("2026-01-31") e erros por hora ("2026-01-31 14")
CREATE TABLE IF NOT EXISTS contadores (
    periodo TEXT NOT NULL, nome TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (periodo, nome)
);
CREATE TABLE IF NOT EXISTS relatos (id INTEGER PRIMARY KEY AUTOINCREMENT, criado REAL NOT NULL, texto TEXT NOT NULL);
"""

# O saldo da Coinos pode chegar atrasado (cache do lado dela): na reserva, os pagamentos
# fechados até este tanto ANTES da leitura também são descontados. Se o saldo vier
# fresco, a conta só fica mais conservadora.
JANELA_SALDO_S = 120

# Prefixo de `erro` quando a Coinos recusou o pagamento por causa do DESTINATÁRIO (ex.: usuário
# que sumiu). Quem controla o destinatário pode provocar isso: não conta para a pausa geral.
ERRO_DESTINO = "destino: "

# status que contam para os limites ("falhou" = comprovadamente nada saiu da conta)
CONTA = ("pendente", "pago", "erro")
_EM = "status IN ('pendente', 'pago', 'erro')"


class Recusa(Exception):
    """Resgate recusado na reserva; `codigo` diz o motivo."""

    def __init__(self, codigo):
        super().__init__(codigo)
        self.codigo = codigo


class DB:
    def __init__(self, caminho):
        if caminho != ":memory:":
            os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
        self.con = sqlite3.connect(caminho, check_same_thread=False, isolation_level=None, timeout=15)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=15000")
        self.con.executescript(ESQUEMA)
        colunas = {r[1] for r in self.con.execute("PRAGMA table_info(resgates)")}
        if "fechado" not in colunas:  # bancos criados antes desta versão
            self.con.execute("ALTER TABLE resgates ADD COLUMN fechado REAL")
        if "tipo" not in colunas:
            self.con.execute("ALTER TABLE resgates ADD COLUMN tipo TEXT NOT NULL DEFAULT 'arte'")
        colunas = {r[1] for r in self.con.execute("PRAGMA table_info(tokens)")}
        if "dono" not in colunas:
            self.con.execute("ALTER TABLE tokens ADD COLUMN dono TEXT")
        if "tipo" not in colunas:
            self.con.execute("ALTER TABLE tokens ADD COLUMN tipo TEXT NOT NULL DEFAULT 'arte'")
            self.con.execute("ALTER TABLE tokens ADD COLUMN sats INTEGER")
        self.trava = threading.Lock()

    def ex(self, sql, *args):
        with self.trava:
            return self.con.execute(sql, args)

    def um(self, sql, *args):
        with self.trava:
            return self.con.execute(sql, args).fetchone()

    # ---------- tokens ----------
    def criar_token(self, token, dono=None, tipo="arte", sats=None):
        """`dono` = hash da cota de quem exportou. None só em testes (token sem vínculo)."""
        self.ex("INSERT INTO tokens(token, criado, dono, tipo, sats) VALUES (?, ?, ?, ?, ?)",
                token, time.time(), dono, tipo, sats)

    def token_valido(self, token, validade_s, dono=None, tipo="arte"):
        return self.um("SELECT 1 FROM tokens WHERE token = ? AND usado = 0 AND criado > ? "
                       "AND (dono IS NULL OR dono = ?) AND tipo = ?",
                       token, time.time() - validade_s, dono, tipo) is not None

    def sats_do_token(self, token):
        r = self.um("SELECT sats FROM tokens WHERE token = ?", token)
        return r[0] if r else None

    # ---------- resgates ----------
    def soma_dia(self, dia):
        """Total gasto no dia, incluindo taxas."""
        return self.um(f"SELECT COALESCE(SUM(sats + fee), 0) FROM resgates WHERE dia = ? AND {_EM}", dia)[0]

    def contar_dia(self, dia, coluna, valor, tipo="arte"):
        assert coluna in ("endereco", "ip_hash")
        return self.um(f"SELECT COUNT(*) FROM resgates WHERE dia = ? AND {_EM} AND {coluna} = ? AND tipo = ?",
                       dia, valor, tipo)[0]

    def fechar_pendentes_orfaos(self, idade_s=600):
        """Pendente velho = processo caiu no meio do pagamento: vira 'erro' (conferir na Coinos)."""
        return self.ex("UPDATE resgates SET status = 'erro', erro = 'pendente órfão (reinício no meio do "
                       "pagamento?)', fechado = ? WHERE status = 'pendente' AND criado < ?",
                       time.time(), time.time() - idade_s).rowcount

    def adquirir_vez_externo(self, dono):
        """Um pagamento externo por vez, entre TODOS os processos que usam este banco.

        Falha se os externos estão pausados ou se outro pagamento externo está em curso.
        Se um processo cair segurando a vez, ela fica presa até alguém conferir na Coinos
        e apagar a linha 'vez_externo' da tabela estado (de propósito: pode ter havido débito)."""
        with self.trava:
            c = self.con
            c.execute("BEGIN IMMEDIATE")
            try:
                ocupado = c.execute("SELECT 1 FROM estado WHERE chave IN ('pausa_externos', 'vez_externo')").fetchone()
                if ocupado:
                    c.execute("ROLLBACK")
                    return False
                c.execute("INSERT INTO estado(chave, valor) VALUES ('vez_externo', ?)", (str(dono),))
                c.execute("COMMIT")
                return True
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def liberar_vez_externo(self, dono):
        self.ex("DELETE FROM estado WHERE chave = 'vez_externo' AND valor = ?", str(dono))

    def erros_dia(self, dia):
        """Pagamentos sem confirmação do dia para endereços @coinos.io (os externos têm pausa própria),
        sem contar as recusas ligadas ao destinatário (ERRO_DESTINO)."""
        return self.um("SELECT COUNT(*) FROM resgates WHERE dia = ? AND status = 'erro' "
                       "AND endereco LIKE '%@coinos.io' AND COALESCE(erro, '') NOT LIKE ?",
                       dia, ERRO_DESTINO + "%")[0]

    def recusas_destino_dia(self, dia):
        return self.um("SELECT COUNT(*) FROM resgates WHERE dia = ? AND status = 'erro' AND erro LIKE ?",
                       dia, ERRO_DESTINO + "%")[0]

    def reservar_resgate(self, *, token, validade_s, dia, endereco, ip_hash, seq,
                         limite_global, taxa_max, intervalo_min_s, saldo, saldo_em, reserva_pote,
                         tipo="arte"):
        """Confere tudo e grava o resgate como 'pendente', atomicamente.

        `tipo` separa os limites por pessoa e o limite global do dia (arte e urna têm os seus);
        o intervalo entre resgates e o pote valem para todos os tipos juntos.

        `saldo` é o saldo da carteira lido no instante `saldo_em`; o que foi pago
        depois dessa leitura (ou ainda está em voo) é descontado aqui.
        Devolve (id, sats). Levanta Recusa(codigo) sem gravar nada se algo falhar.
        """
        agora = time.time()
        with self.trava:
            c = self.con
            c.execute("BEGIN IMMEDIATE")
            try:
                um = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
                feitos = max(um(f"SELECT COUNT(*) FROM resgates WHERE dia = ? AND {_EM} AND endereco = ? "
                                "AND tipo = ?", dia, endereco, tipo),
                             um(f"SELECT COUNT(*) FROM resgates WHERE dia = ? AND {_EM} AND ip_hash = ? "
                                "AND tipo = ?", dia, ip_hash, tipo))
                if feitos >= len(seq):
                    raise Recusa("limite_usuario")
                sats = seq[feitos]
                ultimo = um(f"SELECT COALESCE(MAX(criado), 0) FROM resgates WHERE ip_hash = ? AND {_EM}", ip_hash)
                if agora - ultimo < intervalo_min_s:
                    raise Recusa("intervalo")
                gasto = um(f"SELECT COALESCE(SUM(sats + fee), 0) FROM resgates WHERE dia = ? AND {_EM} "
                           "AND tipo = ?", dia, tipo)
                pendentes = um("SELECT COUNT(*) FROM resgates WHERE dia = ? AND status = 'pendente' AND tipo = ?",
                               dia, tipo)
                if gasto + (pendentes + 1) * taxa_max + sats > limite_global:
                    raise Recusa("limite_global")
                # pote: TODOS os pendentes (inclusive de dias anteriores) e a taxa de cada um
                em_voo, em_voo_n = c.execute("SELECT COALESCE(SUM(sats), 0), COUNT(*) FROM resgates "
                                             "WHERE status = 'pendente'").fetchone()
                pagos_depois = um("SELECT COALESCE(SUM(sats + fee), 0) FROM resgates "
                                  "WHERE status IN ('pago', 'erro') AND fechado >= ?", saldo_em - JANELA_SALDO_S)
                if saldo - em_voo - pagos_depois - (em_voo_n + 1) * taxa_max - sats < reserva_pote:
                    raise Recusa("pote")
                # o token só vale para quem exportou (dono = hash da cota do IP de quem exportou)
                if c.execute("UPDATE tokens SET usado = 1 WHERE token = ? AND usado = 0 AND criado > ? "
                             "AND (dono IS NULL OR dono = ?) AND tipo = ?",
                             (token, agora - validade_s, ip_hash, tipo)).rowcount != 1:
                    raise Recusa("token")
                rid = c.execute("INSERT INTO resgates(dia, endereco, ip_hash, sats, status, criado, tipo) "
                                "VALUES (?, ?, ?, ?, 'pendente', ?, ?)",
                                (dia, endereco, ip_hash, sats, agora, tipo)).lastrowid
                c.execute("COMMIT")
                return rid, sats
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def liberar_resgate(self, rid, token, erro):
        """Pagamento comprovadamente não enviado: não conta nos limites e o token volta a valer."""
        with self.trava:
            c = self.con
            c.execute("BEGIN IMMEDIATE")
            try:
                alterados = c.execute("UPDATE resgates SET status = 'falhou', erro = ? "
                                      "WHERE id = ? AND status = 'pendente'", ((erro or "")[:300], rid)).rowcount
                if alterados == 1:  # resgate já fechado (pago/erro) nunca devolve o token
                    c.execute("UPDATE tokens SET usado = 0 WHERE token = ? AND usado = 1", (token,))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def fechar_resgate(self, rid, status, ref=None, fee=0, erro=None):
        self.ex("UPDATE resgates SET status = ?, ref = ?, fee = ?, erro = ?, fechado = ? WHERE id = ?",
                status, ref, int(fee or 0), (erro or "")[:300] or None, time.time(), rid)

    # ---------- prova de trabalho ----------
    def queimar_salt(self, salt):
        """Registra o desafio como usado; False se já tinha sido usado."""
        try:
            self.ex("INSERT INTO pow_usados(salt, criado) VALUES (?, ?)", salt, time.time())
            return True
        except sqlite3.IntegrityError:
            return False

    # ---------- doações ----------
    def nova_doacao(self, hash_, sats):
        self.ex("INSERT OR IGNORE INTO doacoes(hash, sats, criado) VALUES (?, ?, ?)", hash_, sats, time.time())

    def marcar_doacao_paga(self, hash_):
        self.ex("UPDATE doacoes SET pago = 1 WHERE hash = ?", hash_)

    def estado_doacao(self, hash_):
        """None se não existe; senão (pago, criado)."""
        r = self.um("SELECT pago, criado FROM doacoes WHERE hash = ?", hash_)
        return None if r is None else (bool(r[0]), r[1])

    # ---------- estado persistente (ex.: pausa de pagamentos externos) ----------
    def ler_estado(self, chave):
        r = self.um("SELECT valor FROM estado WHERE chave = ?", chave)
        return r[0] if r else None

    def gravar_estado(self, chave, valor):
        self.ex("INSERT INTO estado(chave, valor) VALUES (?, ?) "
                "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor", chave, str(valor))

    # ---------- painel admin ----------
    def contar(self, periodo, nome):
        self.ex("INSERT INTO contadores(periodo, nome, n) VALUES (?, ?, 1) "
                "ON CONFLICT(periodo, nome) DO UPDATE SET n = n + 1", periodo, nome)

    def contadores(self, nome_like, desde):
        """[(periodo, nome, n)] com periodo >= desde."""
        with self.trava:
            return self.con.execute("SELECT periodo, nome, n FROM contadores WHERE nome LIKE ? AND periodo >= ? "
                                    "ORDER BY periodo", (nome_like, desde)).fetchall()

    def sats_por_dia(self, desde):
        with self.trava:
            return self.con.execute("SELECT dia, COALESCE(SUM(sats), 0), COUNT(*) FROM resgates "
                                    "WHERE status = 'pago' AND dia >= ? GROUP BY dia ORDER BY dia",
                                    (desde,)).fetchall()

    def guardar_relato(self, texto):
        self.ex("INSERT INTO relatos(criado, texto) VALUES (?, ?)", time.time(), texto[:2000])

    def ultimos_relatos(self, n=20):
        with self.trava:
            return self.con.execute("SELECT criado, texto FROM relatos ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    # ---------- limpeza ----------
    def limpar(self, token_ttl_s=1800, pow_ttl_s=600, retencao_dias=30):
        """Apaga o que não serve mais. Resgates concluídos (endereço + hash do IP) e doações
        pagas ficam `retencao_dias`; resgates em 'erro'/'pendente' ficam 6x mais, para conciliação."""
        agora = time.time()
        limite = agora - max(2, retencao_dias) * 86400
        n = {}
        n["tokens"] = self.ex("DELETE FROM tokens WHERE criado < ?", agora - token_ttl_s - 3600).rowcount
        n["provas"] = self.ex("DELETE FROM pow_usados WHERE criado < ?", agora - pow_ttl_s - 3600).rowcount
        n["doacoes"] = (self.ex("DELETE FROM doacoes WHERE pago = 0 AND criado < ?", agora - 2 * 86400).rowcount
                        + self.ex("DELETE FROM doacoes WHERE pago = 1 AND criado < ?", limite).rowcount)
        n["resgates"] = (self.ex("DELETE FROM resgates WHERE status IN ('pago', 'falhou') AND criado < ?",
                                 limite).rowcount
                         + self.ex("DELETE FROM resgates WHERE criado < ?",
                                   agora - max(2, retencao_dias) * 6 * 86400).rowcount)
        n["contadores"] = self.ex("DELETE FROM contadores WHERE periodo < ?",
                                  time.strftime("%Y-%m-%d", time.gmtime(agora - 40 * 86400))).rowcount
        n["relatos"] = self.ex("DELETE FROM relatos WHERE criado < ?", agora - 14 * 86400).rowcount
        semana = time.strftime("%Y-%m-%d", time.gmtime(agora - 7 * 86400))
        self.ex("DELETE FROM estado WHERE chave LIKE 'pausa_recompensas:%' AND chave < ?",
                "pausa_recompensas:" + semana)
        self.ex("DELETE FROM estado WHERE chave LIKE 'bloqueio:%' AND chave < ?", "bloqueio:" + semana)
        return n
