"""Biblioteca Pessoal - sistema local (Flask + SQLite).

Rodar:  python app.py   e abrir http://localhost:5000
"""
import os
import io
import re
import csv
import json
import sqlite3
import hashlib
import secrets
import contextlib
import tempfile
import shutil
import zipfile
import unicodedata
from pathlib import Path
from difflib import SequenceMatcher
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta
from functools import wraps
from html import escape as esc

from flask import (Flask, request, redirect, url_for, session, flash,
                   send_from_directory, Response, get_flashed_messages, abort, send_file)
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
_INSTANCE_ID = hashlib.sha256(os.path.normcase(os.path.abspath(BASE)).encode('utf-8')).hexdigest()[:16]
_PERFIL_LOCAL = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), '.local', 'share')
DATA_BASE = os.path.abspath(os.environ.get('BIBLIO_DATA_DIR') or os.path.join(_PERFIL_LOCAL, f'BibliotecaPessoal-{_INSTANCE_ID}'))
DB = os.path.join(DATA_BASE, 'biblioteca.db')
CAPAS = os.path.join(DATA_BASE, 'capas')
CDD_THUMBS = os.path.join(DATA_BASE, 'cdd_thumbs')
os.makedirs(DATA_BASE, exist_ok=True)
os.makedirs(CAPAS, exist_ok=True)
os.makedirs(CDD_THUMBS, exist_ok=True)


def preparar_dados_locais():
    """Migra os dados antigos e preserva-os fora da pasta substituída em atualizações."""
    db_antiga = os.path.join(BASE, 'biblioteca.db')
    if not os.path.exists(DB) and os.path.isfile(db_antiga):
        origem = sqlite3.connect(db_antiga)
        destino = sqlite3.connect(DB)
        try:
            origem.backup(destino)
        finally:
            destino.close(); origem.close()
    for nome in ('capas', 'cdd_thumbs'):
        origem = os.path.join(BASE, nome)
        destino = CAPAS if nome == 'capas' else CDD_THUMBS
        if os.path.isdir(origem):
            for pasta, _, ficheiros in os.walk(origem):
                relativa = os.path.relpath(pasta, origem)
                atual = destino if relativa == '.' else os.path.join(destino, relativa)
                os.makedirs(atual, exist_ok=True)
                for ficheiro in ficheiros:
                    src = os.path.join(pasta, ficheiro); dst = os.path.join(atual, ficheiro)
                    if not os.path.exists(dst): shutil.copy2(src, dst)
    pacote_thumbs = os.path.join(BASE, 'cdd_thumbs_assets.zip')
    if os.path.isfile(pacote_thumbs):
        try:
            with zipfile.ZipFile(pacote_thumbs) as pacote:
                for info in pacote.infolist():
                    nome_thumb = os.path.basename(info.filename)
                    if info.is_dir() or not re.fullmatch(r'cdd_\d{3}\.svg', nome_thumb) or info.file_size > 32 * 1024:
                        continue
                    destino_thumb = os.path.join(CDD_THUMBS, nome_thumb)
                    if not os.path.exists(destino_thumb):
                        with pacote.open(info) as origem, open(destino_thumb, 'wb') as destino:
                            shutil.copyfileobj(origem, destino)
        except zipfile.BadZipFile:
            pass
    for nome in ('cdd_base.txt', 'email.json'):
        origem = os.path.join(BASE, nome); destino = os.path.join(DATA_BASE, nome)
        if os.path.isfile(origem) and not os.path.exists(destino): shutil.copy2(origem, destino)


preparar_dados_locais()

PRAZO = 30            # dias de empréstimo
MAX_RENOV = 2         # renovações permitidas
LIMITE_DIAS = 31      # período máximo de relatórios e histórico de eventos
AREAS = {'acervo': 'Acervo', 'leitores': 'Leitores', 'emprestimos': 'Empréstimos',
         'relatorios': 'Relatórios', 'eventos': 'Eventos', 'usuarios': 'Equipa'}
MIDIAS = ['Livro', 'Audiobook', 'CD', 'DVD', 'E-book', 'Mapa', 'Outro']
EXT_CAPA = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ROTULO = {'emprestado': 'Emprestado', 'atrasado': 'Atrasado', 'devolvido': 'Devolvido'}

TABELAS = {
    'usuarios': ['id', 'nome', 'senha', 'admin', 'perms', 'senha_temporaria'],
    'livros': ['id', 'titulo', 'autor', 'coautor', 'editora', 'ano', 'edicao', 'volume',
               'cdd', 'chamada', 'midia', 'exemplares', 'capa', 'subtitulo', 'local_publicacao', 'isbn'],
    'leitores': ['id', 'nome', 'matricula', 'email', 'telefone', 'senha', 'senha_temporaria'],
    'emprestimos': ['id', 'livro_id', 'leitor_id', 'retirada', 'prevista', 'devolucao', 'renovacoes'],
    'eventos': ['id', 'data', 'usuario', 'acao'],
    'reservas': ['id', 'livro_id', 'leitor_id', 'criada', 'status'],
    'notificacoes': ['id', 'leitor_id', 'data', 'texto', 'lida'],
}
CAMPOS_PLANILHA = ['titulo', 'subtitulo', 'autor', 'coautor', 'editora', 'local_publicacao', 'isbn', 'ano',
                   'edicao', 'volume', 'cdd', 'midia', 'exemplares']

app = Flask(__name__)
# A chave muda a cada inicialização para invalidar cookies e exigir novo login.
# BIBLIO_KEY continua contribuindo com um segredo privado configurável.
app.secret_key = hashlib.sha256(
    os.environ.get('BIBLIO_KEY', 'biblioteca-local').encode('utf-8') + os.urandom(32)
).digest()
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024
MAX_CAMPO_PLANILHA = 100_000
csv.field_size_limit(MAX_CAMPO_PLANILHA)
app.config['MAX_FORM_MEMORY_SIZE'] = 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

@app.errorhandler(413)
def arquivo_excede_limite(_erro):
    return 'O ficheiro excede o limite máximo de 2 GB.', 413


@app.before_request
def proteger_post_csrf():
    if session.get('leitor_senha_temporaria') and request.endpoint not in ('minha_conta', 'senha_propria_leitor', 'sair'):
        return redirect(url_for('minha_conta'))
    if request.method == 'POST':
        esperado = session.get('_csrf_token', '')
        recebido = request.form.get('_csrf_token', '') or request.headers.get('X-CSRF-Token', '')
        if not esperado or not recebido or not secrets.compare_digest(esperado, recebido):
            abort(400, description='Token de segurança inválido. Atualize a página e tente novamente.')

# ---------------------------------------------------------------- banco de dados

SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY, nome TEXT UNIQUE NOT NULL,
    senha TEXT DEFAULT '', admin INTEGER DEFAULT 0, perms TEXT DEFAULT '', senha_temporaria INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS livros (id INTEGER PRIMARY KEY, titulo TEXT NOT NULL,
    autor TEXT DEFAULT '', coautor TEXT DEFAULT '', editora TEXT DEFAULT '', ano TEXT DEFAULT '',
    edicao TEXT DEFAULT '', volume TEXT DEFAULT '', cdd TEXT DEFAULT '', chamada TEXT DEFAULT '',
    midia TEXT DEFAULT 'Livro', exemplares INTEGER DEFAULT 1, capa TEXT DEFAULT '', isbn TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS leitores (id INTEGER PRIMARY KEY, nome TEXT NOT NULL,
    matricula TEXT DEFAULT '', email TEXT DEFAULT '', telefone TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS emprestimos (id INTEGER PRIMARY KEY, livro_id INTEGER, leitor_id INTEGER,
    retirada TEXT, prevista TEXT, devolucao TEXT, renovacoes INTEGER DEFAULT 0,
    FOREIGN KEY(livro_id) REFERENCES livros(id) ON DELETE RESTRICT,
    FOREIGN KEY(leitor_id) REFERENCES leitores(id) ON DELETE RESTRICT);
CREATE TABLE IF NOT EXISTS eventos (id INTEGER PRIMARY KEY, data TEXT, usuario TEXT, acao TEXT);
CREATE TABLE IF NOT EXISTS reservas (id INTEGER PRIMARY KEY, livro_id INTEGER, leitor_id INTEGER,
    criada TEXT, status TEXT DEFAULT 'ativa',
    FOREIGN KEY(livro_id) REFERENCES livros(id) ON DELETE RESTRICT,
    FOREIGN KEY(leitor_id) REFERENCES leitores(id) ON DELETE RESTRICT);
CREATE TABLE IF NOT EXISTS notificacoes (id INTEGER PRIMARY KEY, leitor_id INTEGER, data TEXT,
    texto TEXT, lida INTEGER DEFAULT 0,
    FOREIGN KEY(leitor_id) REFERENCES leitores(id) ON DELETE CASCADE);
"""

SQL_LIVROS = ("SELECT l.*, l.exemplares"
              " - (SELECT COUNT(*) FROM emprestimos e WHERE e.livro_id = l.id AND e.devolucao IS NULL)"
              " - (SELECT COUNT(*) FROM reservas r WHERE r.livro_id = l.id AND r.status = 'pronta')"
              " AS disp FROM livros l")
SQL_EMP = ("SELECT e.*, l.titulo AS livro, l.midia AS midia, r.nome AS leitor "
           "FROM emprestimos e LEFT JOIN livros l ON l.id = e.livro_id "
           "LEFT JOIN leitores r ON r.id = e.leitor_id")


def conectar():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    c.execute('PRAGMA busy_timeout = 10000')
    return c


@contextlib.contextmanager
def transacao():
    """Serializa operações concorrentes e confirma-as como uma unidade."""
    c = conectar()
    try:
        c.execute('BEGIN IMMEDIATE')
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def run(sql, args=()):
    c = conectar()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def rows(sql, args=()):
    c = conectar()
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def row(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def init_db():
    c = conectar()
    try:
        c.execute('PRAGMA journal_mode = WAL')
        c.executescript(SCHEMA)
        user_cols = [r[1] for r in c.execute('PRAGMA table_info(usuarios)').fetchall()]
        if 'senha_temporaria' not in user_cols:
            c.execute('ALTER TABLE usuarios ADD COLUMN senha_temporaria INTEGER DEFAULT 0')
        # Credenciais de teste conhecidas passam a exigir troca individual no próximo acesso.
        for u in c.execute('SELECT id,senha FROM usuarios WHERE senha_temporaria=0').fetchall():
            try:
                if u['senha'] and check_password_hash(u['senha'], '1234'):
                    c.execute('UPDATE usuarios SET senha_temporaria=1 WHERE id=?', (u['id'],))
            except (ValueError, TypeError):
                pass
        cols = [r[1] for r in c.execute("PRAGMA table_info(livros)").fetchall()]
        for col in ['subtitulo', 'local_publicacao', 'isbn']:
            if col not in cols:
                c.execute(f"ALTER TABLE livros ADD COLUMN {col} TEXT DEFAULT ''")
        leitor_cols = [r[1] for r in c.execute("PRAGMA table_info(leitores)").fetchall()]
        for col, ddl in [('senha', "TEXT DEFAULT ''"), ('senha_temporaria', 'INTEGER DEFAULT 0')]:
            if col not in leitor_cols:
                c.execute(f'ALTER TABLE leitores ADD COLUMN {col} {ddl}')
        for l in c.execute('SELECT id,senha FROM leitores WHERE senha_temporaria=0').fetchall():
            try:
                if l['senha'] and check_password_hash(l['senha'], '1234'):
                    c.execute('UPDATE leitores SET senha_temporaria=1 WHERE id=?', (l['id'],))
            except (ValueError, TypeError):
                pass
        # Migra tabelas antigas para impor as relações também em bases já existentes.
        if not c.execute('PRAGMA foreign_key_list(emprestimos)').fetchall():
            c.execute('PRAGMA foreign_keys = OFF')
            orfaos = (
                c.execute("SELECT COUNT(*) FROM emprestimos e LEFT JOIN livros l ON l.id=e.livro_id LEFT JOIN leitores r ON r.id=e.leitor_id WHERE l.id IS NULL OR r.id IS NULL").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM reservas x LEFT JOIN livros l ON l.id=x.livro_id LEFT JOIN leitores r ON r.id=x.leitor_id WHERE l.id IS NULL OR r.id IS NULL").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM notificacoes n LEFT JOIN leitores r ON r.id=n.leitor_id WHERE r.id IS NULL").fetchone()[0],
            )
            if any(orfaos):
                raise RuntimeError(f'A base contém referências órfãs (empréstimos, reservas, avisos): {orfaos}. Faça backup e corrija antes da migração.')
            c.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE emprestimos_novo (id INTEGER PRIMARY KEY, livro_id INTEGER, leitor_id INTEGER,
                    retirada TEXT, prevista TEXT, devolucao TEXT, renovacoes INTEGER DEFAULT 0,
                    FOREIGN KEY(livro_id) REFERENCES livros(id) ON DELETE RESTRICT,
                    FOREIGN KEY(leitor_id) REFERENCES leitores(id) ON DELETE RESTRICT);
                INSERT INTO emprestimos_novo SELECT id,livro_id,leitor_id,retirada,prevista,devolucao,renovacoes FROM emprestimos;
                DROP TABLE emprestimos;
                ALTER TABLE emprestimos_novo RENAME TO emprestimos;
                CREATE TABLE reservas_novo (id INTEGER PRIMARY KEY, livro_id INTEGER, leitor_id INTEGER,
                    criada TEXT, status TEXT DEFAULT 'ativa',
                    FOREIGN KEY(livro_id) REFERENCES livros(id) ON DELETE RESTRICT,
                    FOREIGN KEY(leitor_id) REFERENCES leitores(id) ON DELETE RESTRICT);
                INSERT INTO reservas_novo SELECT id,livro_id,leitor_id,criada,status FROM reservas;
                DROP TABLE reservas;
                ALTER TABLE reservas_novo RENAME TO reservas;
                CREATE TABLE notificacoes_novo (id INTEGER PRIMARY KEY, leitor_id INTEGER, data TEXT,
                    texto TEXT, lida INTEGER DEFAULT 0,
                    FOREIGN KEY(leitor_id) REFERENCES leitores(id) ON DELETE CASCADE);
                INSERT INTO notificacoes_novo SELECT id,leitor_id,data,texto,lida FROM notificacoes;
                DROP TABLE notificacoes;
                ALTER TABLE notificacoes_novo RENAME TO notificacoes;
                COMMIT;
            """)
            c.execute('PRAGMA foreign_keys = ON')
            violacoes = c.execute('PRAGMA foreign_key_check').fetchall()
            if violacoes:
                raise RuntimeError(f'A migração encontrou violações de integridade: {len(violacoes)}.')
        c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_emprestimos_livro_ativo ON emprestimos(livro_id, devolucao);
            CREATE INDEX IF NOT EXISTS idx_emprestimos_leitor_ativo ON emprestimos(leitor_id, devolucao);
            CREATE INDEX IF NOT EXISTS idx_reservas_livro_fila ON reservas(livro_id, status, criada, id);
            CREATE INDEX IF NOT EXISTS idx_reservas_leitor_status ON reservas(leitor_id, status);
            CREATE INDEX IF NOT EXISTS idx_notificacoes_leitor_id ON notificacoes(leitor_id, id DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_reserva_ativa_leitor_livro
                ON reservas(livro_id, leitor_id) WHERE status IN ('ativa','pronta');
        """)
        if not c.execute("SELECT 1 FROM usuarios").fetchone():
            senha_inicial = secrets.token_urlsafe(15)
            c.execute("INSERT INTO usuarios(nome, senha, admin, perms, senha_temporaria) VALUES ('Administrador', ?, 1, '', 1)",
                      (generate_password_hash(senha_inicial),))
            c.commit()
            print('Primeiro acesso da biblioteca: utilizador Administrador; senha temporária única: ' + senha_inicial)
    finally:
        c.close()


# ---------------------------------------------------------------- utilitários

def hoje():
    return date.today().isoformat()


def norm(s):
    return unicodedata.normalize('NFD', str(s or '')).encode('ascii', 'ignore').decode().lower().strip()


def toint(v, padrao=1):
    try:
        return max(1, int(float(str(v).replace(',', '.'))))
    except (TypeError, ValueError):
        return padrao


def gerar_senha_temporaria():
    return secrets.token_urlsafe(12)


def log(acao, c=None):
    agora = datetime.now()
    args = (agora.strftime('%Y-%m-%d %H:%M:%S'), session.get('nome', session.get('leitor_nome', '-')), acao)
    limite = ((agora - timedelta(days=LIMITE_DIAS)).strftime('%Y-%m-%d %H:%M:%S'),)
    if c is not None:
        c.execute("INSERT INTO eventos(data, usuario, acao) VALUES (?, ?, ?)", args)
        c.execute("DELETE FROM eventos WHERE data < ?", limite)
    else:
        run("INSERT INTO eventos(data, usuario, acao) VALUES (?, ?, ?)", args)
        run("DELETE FROM eventos WHERE data < ?", limite)


def can(p):
    return bool(session.get('admin')) or p in session.get('perms', [])


def disponiveis(livro_id):
    r = row(SQL_LIVROS + " WHERE l.id = ?", (livro_id,))
    return r['disp'] if r else 0


def situacao(e):
    if e['devolucao']:
        return 'devolvido'
    return 'atrasado' if e['prevista'] < hoje() else 'emprestado'


EMAIL_CFG = os.path.join(DATA_BASE, 'email.json')


def carregar_email_cfg():
    dados = {}
    if os.path.exists(EMAIL_CFG):
        try:
            with open(EMAIL_CFG, encoding='utf-8') as f:
                dados = json.load(f)
        except Exception:
            dados = {}
    return {
        'host': dados.get('host') or os.environ.get('BIBLIO_SMTP_HOST', ''),
        'porta': int(dados.get('porta') or os.environ.get('BIBLIO_SMTP_PORT', 587)),
        'usuario': dados.get('usuario') or os.environ.get('BIBLIO_SMTP_USER', ''),
        'senha': os.environ.get('BIBLIO_SMTP_PASS', ''),
        'remetente': dados.get('remetente') or dados.get('usuario') or os.environ.get('BIBLIO_SMTP_USER', ''),
    }


def _smtp_enviar(cfg, msg):
    import smtplib
    if cfg['porta'] == 465:
        s = smtplib.SMTP_SSL(cfg['host'], 465, timeout=20)
    else:
        s = smtplib.SMTP(cfg['host'], cfg['porta'], timeout=20)
        s.starttls()
    with s:
        if cfg['usuario']:
            s.login(cfg['usuario'], cfg['senha'])
        s.send_message(msg)


def enviar_direto(destino, assunto, texto):
    """Devolve None se enviou; ou uma mensagem de erro."""
    cfg = carregar_email_cfg()
    if not cfg['host']:
        return 'Servidor de e-mail não configurado (Equipa > E-mail).'
    if not destino:
        return 'Destinatário sem e-mail cadastrado.'
    from email.message import EmailMessage
    msg = EmailMessage()
    msg['Subject'] = assunto
    msg['From'] = cfg['remetente']
    msg['To'] = destino
    msg.set_content(texto)
    try:
        _smtp_enviar(cfg, msg)
        return None
    except Exception as ex:
        return f'Falha ao enviar: {ex}'


def enviar_email(leitor_id, texto):
    l = row("SELECT email FROM leitores WHERE id = ?", (leitor_id,))
    erro = enviar_direto(l['email'] if l else '', 'Biblioteca: aviso de reserva', texto)
    if erro:
        log(f'E-mail não enviado: {erro}')


def notificar(leitor_id, texto):
    run("INSERT INTO notificacoes(leitor_id, data, texto, lida) VALUES (?, ?, ?, 0)",
        (leitor_id, datetime.now().strftime('%Y-%m-%d %H:%M'), texto))
    enviar_email(leitor_id, texto)


def fila(livro_id):
    return rows("SELECT * FROM reservas WHERE livro_id = ? AND status = 'ativa' ORDER BY criada, id", (livro_id,))


def avisar_fila(livro_id):
    """Com mais de duas pessoas na fila, avisa cada uma da sua posição."""
    espera = fila(livro_id)
    if len(espera) > 2:
        titulo = row("SELECT titulo FROM livros WHERE id = ?", (livro_id,))['titulo']
        for i, r in enumerate(espera, 1):
            notificar(r['leitor_id'], f'Atualização: você está em {i}º lugar na fila de espera de "{titulo}" ({len(espera)} na fila).')


def disponibilidade_db(c, livro_id):
    return c.execute("SELECT exemplares - (SELECT COUNT(*) FROM emprestimos WHERE livro_id=? AND devolucao IS NULL) - (SELECT COUNT(*) FROM reservas WHERE livro_id=? AND status='pronta') FROM livros WHERE id=?",
                     (livro_id, livro_id, livro_id)).fetchone()


def inserir_notificacao_db(c, leitor_id, texto):
    c.execute("INSERT INTO notificacoes(leitor_id,data,texto,lida) VALUES (?,?,?,0)",
              (leitor_id, datetime.now().strftime('%Y-%m-%d %H:%M'), texto))


def promover_db(c, livro_id):
    """Promove a fila sob o mesmo bloqueio transacional da devolução/cancelamento."""
    item = c.execute("SELECT titulo FROM livros WHERE id=?", (livro_id,)).fetchone()
    if not item:
        return []
    avisos = []
    while (disponibilidade_db(c, livro_id) or [0])[0] > 0:
        espera = c.execute("SELECT id,leitor_id FROM reservas WHERE livro_id=? AND status='ativa' ORDER BY criada,id LIMIT 1",
                           (livro_id,)).fetchone()
        if not espera:
            break
        alterada = c.execute("UPDATE reservas SET status='pronta' WHERE id=? AND status='ativa'", (espera['id'],)).rowcount
        if not alterada:
            continue
        texto = f'"{item["titulo"]}" foi devolvido e está reservado para você. Procure a biblioteca para retirar.'
        inserir_notificacao_db(c, espera['leitor_id'], texto)
        avisos.append((espera['leitor_id'], texto))
    return avisos


def enviar_avisos(avisos):
    for leitor_id, texto in avisos:
        enviar_email(leitor_id, texto)


def promover(livro_id):
    with transacao() as c:
        avisos = promover_db(c, livro_id)
    enviar_avisos(avisos)
    avisar_fila(livro_id)


def post_btn(action, texto, cls='', confirma=''):
    on = (" onsubmit=\"return confirm('" + confirma + "')\"") if confirma else ''
    return (f'<form method="post" action="{action}" style="display:inline"{on}>'
            f'<button class="btn {cls}">{texto}</button></form>')


def area(p):
    """Exige login e permissão na área p."""
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if 'uid' not in session:
                return redirect(url_for('entrar'))
            if session.get('senha_temporaria') and request.endpoint != 'trocar_senha_usuario':
                return redirect(url_for('trocar_senha_usuario'))
            if not can(p):
                return page('Sem permissão', '<div class="card">Você não tem acesso a esta área.</div>')
            return f(*a, **k)
        return w
    return deco


# ---------------------------------------------------------------- CDD (sugestão por palavras-chave)

CDD_PALAVRAS = {
    # literatura e ficção (800)
    'romance': '869.3', 'ficcao': '869.3', 'conto': '869.3', 'contos': '869.3',
    'cronica': '869.4', 'poesia': '869.1', 'poema': '869.1', 'poemas': '869.1',
    'teatro': '869.2', 'peca': '869.2', 'literatura': '800', 'ficcao cientifica': '823.0872',
    'fantasia': '823.0872', 'terror': '823.0872', 'suspense': '823.0872',
    'policial': '823.0872', 'aventura': '823.0872', 'quadrinhos': '741.5', 'manga': '741.5',
    'hq': '741.5', 'mitologia': '398.2', 'lendas': '398.2', 'contos infantis': '398.2',
    'biografia': '920', 'memorias': '920', 'idioma': '400', 'gramatica': '415',
    'dicionario': '403', 'ingles': '420', 'frances': '440', 'espanhol': '460', 'latim': '470',
    # autoajuda e psicologia (100 a 160)
    'autoajuda': '158.1', 'motivacao': '158.1', 'felicidade': '158.1', 'sucesso': '158.1',
    'meditacao': '158.12', 'emocoes': '152.4', 'inteligencia emocional': '152.4',
    'psicologia': '150', 'mente': '150', 'comportamento': '155.2', 'infancia': '155.4',
    'filosofia': '100', 'etica': '170', 'logica': '160',
    'religiao': '200', 'biblia': '220', 'espiritismo': '133.9', 'mediunidade': '133.9',
    'espirito': '133.9', 'esoterismo': '133', 'astrologia': '133.5',
    # ciências sociais (300)
    'sociologia': '301', 'politica': '320', 'economia': '330', 'financas': '332',
    'investimento': '332.6', 'direito': '340', 'educacao': '370', 'pedagogia': '370',
    'escola': '370', 'administracao': '658', 'negocios': '650', 'marketing': '658.8',
    'contabilidade': '657',
    # ciências naturais (500)
    'matematica': '510', 'astronomia': '520', 'universo': '520', 'fisica': '530',
    'quimica': '540', 'biologia': '570', 'ecologia': '577', 'plantas': '580',
    'animais': '590', 'jardinagem': '635',
    # tecnologia (600)
    'medicina': '610', 'saude': '613', 'nutricao': '613', 'dieta': '613.2', 'doenca': '616',
    'yoga': '613.7', 'engenharia': '620', 'culinaria': '641.5', 'receitas': '641.5',
    'cozinha': '641.5', 'vinho': '641.2',
    # informática (000)
    'computacao': '004', 'informatica': '004', 'internet': '004.6', 'programacao': '005.1',
    'python': '005.133', 'javascript': '005.133', 'linux': '005.4', 'windows': '005.4',
    'banco de dados': '005.74',
    # artes, lazer e esporte (700)
    'arte': '700', 'arquitetura': '720', 'desenho': '741', 'pintura': '759',
    'fotografia': '770', 'musica': '780', 'instrumento': '780', 'cinema': '791.43',
    'filme': '791.43', 'jogos': '794', 'xadrez': '794.1', 'esporte': '796',
    'futebol': '796.334',
    # história e geografia (900)
    'historia': '900', 'geografia': '910', 'guia de viagem': '910.2', 'mapa': '912',
    'brasil': '981',
    'historia do cinema': '791.4309', 'historia do cinema brasileiro': '791.430981',
    'historia do brasil': '981', 'historia de portugal': '946.9', 'historia da europa': '940',
    'historia da africa': '960', 'historia da asia': '950', 'historia da america latina': '980',
    'historia da musica': '780.9', 'historia da literatura': '809', 'historia da filosofia': '109',
    'historia da ciencia': '509', 'historia da educacao': '370.9', 'historia do teatro': '792.09',
    'historia da fotografia': '770.9', 'historia da moda': '391.009', 'historia da medicina': '610.9',
    'historia da computacao': '004.09', 'historia da arte': '709',
    'percepcao social': '302.12', 'social': '302', 'atribuicao': '302.12', 'interacao social': '302',
    # psicologia: processos mentais (150-159)
    'percepcao': '152.1', 'percepcao sensorial': '152.1', 'sensacao': '152.1', 'sensorial': '152.1',
    'sentidos': '152.1', 'memoria': '153.1', 'aprendizagem': '153.15', 'atencao': '153.7',
    'inteligencia': '153.9', 'emocao': '152.4', 'sonhos': '154.6', 'hipnose': '154.7',
    'estresse': '155.9', 'stress': '155.9', 'luto': '155.937', 'personalidade': '155.2',
    'desenvolvimento humano': '155', 'adolescencia': '155.5', 'velhice': '155.67', 'envelhecimento': '155.67',
    'psicanalise': '150.195', 'freud': '150.195', 'jung': '150.195', 'psicoterapia': '616.8914',
    'ansiedade': '616.85', 'depressao': '616.8527', 'tdah': '616.8589', 'autismo': '616.85882',
    # religião e espiritualidade (200-299)
    'cristianismo': '230', 'catolicismo': '282', 'evangelico': '280', 'islamismo': '297',
    'budismo': '294.3', 'hinduismo': '294.5', 'judaismo': '296', 'tarot': '133.3',
    'numerologia': '133.335', 'umbanda': '299.67', 'candomble': '299.6',
    # ciências sociais (300-399)
    'antropologia': '306', 'familia': '306.8', 'sexualidade': '306.7', 'relacoes': '302',
    'comunicacao': '302.2', 'midia': '302.23', 'publicidade': '659.1', 'jornalismo': '070.4',
    'ciencia politica': '320', 'eleicoes': '324.6', 'trabalho': '331', 'carreira': '650.14',
    'empreendedorismo': '658.11', 'contabilidade': '657', 'impostos': '336.2', 'tributario': '343',
    'constituicao': '342', 'penal': '345', 'civil': '346', 'trabalhista': '344.01', 'filosofia do direito': '340.1',
    'folclore': '398', 'contos populares': '398.2', 'lendas urbanas': '398.2',
    # ciências puras e aplicadas (500-600)
    'geologia': '551', 'meteorologia': '551.5', 'mar': '551.46', 'oceanos': '551.46',
    'zoologia': '590', 'botanica': '580', 'insetos': '595.7', 'aves': '598', 'peixes': '597',
    'dinossauros': '567.9', 'genetica': '576.5', 'evolucao': '576.8', 'anatomia': '611',
    'fisiologia': '612', 'farmacia': '615', 'primeiros socorros': '616.025', 'nutricao esportiva': '613.2',
    'eletronica': '621.381', 'eletricidade': '621.3', 'mecanica': '621', 'automoveis': '629.2',
    'construcao': '624', 'arquitetura': '720', 'estatistica': '519.5', 'probabilidade': '519.2',
    'algebra': '512', 'geometria': '516', 'calculo': '515', 'logica matematica': '511.3',
    'agricultura': '630', 'pecuaria': '636', 'veterinaria': '636.089', 'gatos': '636.8', 'caes': '636.7',
    # línguas e literatura (400-899)
    'portugues': '469.5', 'redacao': '808.042', 'escrita criativa': '808.3', 'teoria literaria': '801',
    'literatura brasileira': '869', 'literatura portuguesa': '869.1', 'literatura infantil': '028.5',
    'infantojuvenil': '028.5', 'infanto-juvenil': '028.5', 'juvenil': '028.5', 'classicos': '800',
    'drama': '869.2', 'novela': '869.3', 'ensaio': '814', 'sociedade': '301', 'cultura': '306',
    'historia da arte': '709', 'historia antiga': '930', 'historia medieval': '940.1',
    'historia do mundo': '909', 'segunda guerra': '940.53', 'guerra': '355', 'revolucao': '303.6',
    'mitologia grega': '292', 'mitologia nordica': '293', 'egito': '932', 'roma antiga': '937',
    'africa': '960', 'europa': '940', 'asia': '950', 'america latina': '980', 'portugal': '946.9',
    'viagens': '910.4', 'turismo': '338.4791', 'gastronomia': '641.013', 'bebidas': '641.2',
    'decoracao': '747', 'artesanato': '745.5', 'moda': '391', 'teatro infantil': '792.0',
    'danca': '793.3', 'ópera': '782.1', 'opera': '782.1', 'cinema brasileiro': '791.430981',
    'fotografia digital': '770.285', 'design': '745.4', 'escultura': '730', 'grafite': '751.73',
}


CDD_BASE_ARQUIVO = os.path.join(DATA_BASE, 'cdd_base.txt')
_LINHA_BASE_CDD = re.compile(r'^\s*([^|]+?)\s*\|\s*([^=]+?)\s*=\s*(.*?)\s*$')
_CDD_BASE_CACHE = {'mtime': None, 'dados': []}
_CODIGO_CDD = re.compile(r'^(?:B)?\d{1,3}(?:\.\d+)?$', re.IGNORECASE)


def _ler_base_cdd():
    """Lê linhas no formato código | assunto = termo, sinônimo, expressão."""
    dados = []
    if not os.path.isfile(CDD_BASE_ARQUIVO):
        return dados
    with open(CDD_BASE_ARQUIVO, encoding='utf-8-sig', errors='replace') as arquivo:
        for numero, linha in enumerate(arquivo, 1):
            if not linha.strip() or linha.lstrip().startswith('#'):
                continue
            m = _LINHA_BASE_CDD.match(linha)
            if not m:
                continue
            codigos_txt, assunto, expressoes_txt = m.groups()
            codigos = [c.strip() for c in codigos_txt.split(',') if _CODIGO_CDD.fullmatch(c.strip())]
            if not codigos:
                continue
            termos = [norm(assunto)]
            termos.extend(norm(t.strip()) for t in expressoes_txt.split(','))
            termos = list(dict.fromkeys(t for t in termos if len(t.rstrip('~').strip()) >= 2))
            dados.append({'codigos': codigos, 'assunto': assunto.strip(),
                          'termos': termos, 'linha': numero})
    return dados


def base_cdd():
    """Mantém a base em cache e a recarrega quando o arquivo for atualizado."""
    if not os.path.isfile(CDD_BASE_ARQUIVO):
        return []
    mtime = os.path.getmtime(CDD_BASE_ARQUIVO)
    if _CDD_BASE_CACHE['mtime'] != mtime:
        _CDD_BASE_CACHE['dados'] = _ler_base_cdd()
        _CDD_BASE_CACHE['mtime'] = mtime
    return _CDD_BASE_CACHE['dados']


def _termo_cdd_encontrado(termo, texto):
    termo = norm(termo)
    curinga = termo.endswith('~')
    termo = termo[:-1].rstrip() if curinga else termo
    if not termo:
        return False
    padrao = r'\b' + re.escape(termo)
    padrao += r'\w*\b' if curinga else r'\b'
    return bool(re.search(padrao, texto))


def chave_titulo(texto):
    return re.sub(r'[^a-z0-9]+', ' ', norm(texto)).strip()


def sugestoes_do_catalogo(titulo, limite=6):
    alvo = chave_titulo(titulo)
    tokens_alvo = {t for t in alvo.split() if len(t) > 2}
    if len(alvo) < 4:
        return []
    candidatos = []
    registros = rows("SELECT titulo, autor, cdd FROM livros WHERE TRIM(COALESCE(cdd, '')) <> ''")
    for registro in registros:
        nome = str(registro['titulo'] or '')
        chave = chave_titulo(nome)
        if not chave:
            continue
        exata = chave == alvo
        if exata:
            similaridade = 1.0
        else:
            tokens = {t for t in chave.split() if len(t) > 2}
            jaccard = len(tokens_alvo & tokens) / max(1, len(tokens_alvo | tokens))
            similaridade = max(SequenceMatcher(None, alvo, chave).ratio(), jaccard)
            if len(tokens_alvo) < 3 or similaridade < 0.72:
                continue
        candidatos.append({
            'cdd': str(registro['cdd'] or '').strip(), 'palavra': nome,
            'assunto': 'Mesmo título no seu acervo' if exata else 'Título parecido no seu acervo',
            'origem': 'catalogo_exato' if exata else 'catalogo_parecido',
            'referencia': f"{nome} — {registro['autor'] or 'autor não informado'}",
            'similaridade': round(similaridade, 2),
            'score': 1000 if exata else 20 + similaridade * 10,
            'ordem': len(candidatos),
        })
    candidatos.sort(key=lambda x: (-x['score'], x['ordem']))
    return candidatos[:limite]


def sugerir_cdd_lista(texto, limite=6, titulo=''):
    """Combina a base curada com exemplos do catálogo salvo pelo usuário."""
    t = ' ' + norm(texto) + ' '
    encontrados = []
    ordem = 0
    def adicionar(codigos, assunto, termos, origem):
        nonlocal ordem
        termo_melhor = ''
        for termo in termos:
            if _termo_cdd_encontrado(termo, t):
                limpo = norm(termo).rstrip('~').strip()
                if len(limpo) > len(termo_melhor): termo_melhor = limpo
        if not termo_melhor: return
        for codigo in codigos:
            encontrados.append({'cdd': codigo, 'palavra': termo_melhor, 'assunto': assunto,
                                'origem': origem, 'score': len(termo_melhor), 'ordem': ordem})
            ordem += 1
    for registro in base_cdd():
        adicionar(registro['codigos'], registro['assunto'], registro['termos'], 'base')
    for termo, codigo in CDD_PALAVRAS.items():
        adicionar([codigo], termo, [termo], 'legado')
    encontrados.extend(sugestoes_do_catalogo(titulo, limite))
    encontrados.sort(key=lambda x: (-x['score'], 0 if x['origem'] == 'base' else 1, x['ordem']))
    unicos, vistos = [], set()
    for item in encontrados:
        codigo = item['cdd']
        if codigo in vistos:
            existente = next(x for x in unicos if x['cdd'] == codigo)
            if item.get('referencia') and not existente.get('referencia'):
                existente['referencia'] = item['referencia']
                existente['origem_catalogo'] = item['origem']
            continue
        vistos.add(codigo)
        candidato = {k: item[k] for k in ('cdd', 'palavra', 'assunto', 'origem') if k in item}
        if item.get('referencia'): candidato['referencia'] = item['referencia']
        candidato['preencher'] = item['origem'] == 'base'
        unicos.append(candidato)
        if len(unicos) >= limite: break
    return unicos

def sugerir_cdd(texto):
    """Mantém a interface antiga para os chamadores que esperam (CDD, termo)."""
    sugestoes = sugerir_cdd_lista(texto, limite=1)
    return (sugestoes[0]['cdd'], sugestoes[0]['palavra']) if sugestoes else ('', '')


def capa_padrao_cdd(cdd):
    m = re.search(r'(?:^|[^0-9])(?:B)?(\d{3})', str(cdd or ''), re.IGNORECASE)
    if not m:
        return None
    nome = f'cdd_{m.group(1)}.svg'
    return nome if os.path.isfile(os.path.join(CDD_THUMBS, nome)) else None


def gerar_chamada(cdd, autor):
    cdd = (cdd or '').strip()
    if not cdd:
        return ''
    partes = (autor or '').strip().split()
    letra = norm(partes[-1])[:1].upper() if partes else ''
    return f'{cdd} {letra}'.strip()


# ---------------------------------------------------------------- layout

CSS = """
body{font-family:system-ui,"Segoe UI",sans-serif;margin:0;background:#f7e18025;color:#263238;font-size:16px;line-height:1.5}
header{background:#1f3a5f;color:#fff;padding:10px 16px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header a{color:#fff}
nav{background:#17304f;padding:0 12px;display:flex;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:thin}
nav a{color:#cfe0f5;padding:10px 12px;text-decoration:none;font-size:16px;white-space:nowrap;flex:0 0 auto}
nav a.ativo{color:#fff;border-bottom:2px solid #fff}
main{padding:16px;max-width:1150px;margin:auto}
.card{background:#42c8b175;border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px #0001}
.aba-leitores .card{background:#42c8b146}
table{width:100%;border-collapse:collapse;font-size:16px;line-height:1.4}
th,td{text-align:left;padding:6px;border-bottom:1px solid #eee;vertical-align:middle}
tr.atrasado{background:#fde9e7} tr.devolvido{color:#888}
input,select{padding:8px 9px;margin:3px 0;border:1px solid #ccc;border-radius:4px;font-size:16px;color:#263238}
.btn{padding:8px 13px;border:none;border-radius:4px;background:#1f3a5f;color:#fff;cursor:pointer;text-decoration:none;display:inline-block;margin:2px 0;font-size:16px}
.perigo{background:#b3261e}
.aviso{background:#fff3cd;padding:8px;border-radius:4px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;margin-bottom:8px}
.grid label{font-size:16px;display:flex;flex-direction:column}
.capa{height:84px;border-radius:2px}
.capa-3d{transform:perspective(600px) rotateY(-16deg) rotateX(2deg);transform-origin:left center;box-shadow:8px 6px 14px #0004;position:relative;z-index:1;transition:transform .18s ease,box-shadow .18s ease;cursor:zoom-in}
.capa-3d:hover{transform:perspective(600px) rotateY(-16deg) rotateX(2deg) scale(2);z-index:10;box-shadow:12px 10px 22px #0006}
.capa-vazia{height:84px;width:60px;background:#8c6d46;color:#fff;display:flex;align-items:center;justify-content:center;text-align:center;font-size:10px;font-family:Georgia,serif;border-radius:2px;box-shadow:6px 4px 10px #0003;transform:perspective(600px) rotateY(-16deg);padding:4px}
.folha{background:#fdfaf3;font-family:Georgia,'Times New Roman',serif;border:1px solid #d8cdb4;padding:24px;border-radius:4px}
.folha-topo{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start;border-bottom:1px double #8c6d46;padding-bottom:16px;margin-bottom:12px}
.folha-capa{min-width:120px;padding:10px 0 0 10px}
.folha-texto{flex:1;min-width:260px}
.fr-linha{margin:6px 0}
.fr-campo{display:flex;flex-direction:column;font-size:14px;color:#5a4a2f}
.fr-campo input{font-family:Georgia,serif;font-size:17px;background:transparent;border:none;border-bottom:1px solid #b9a77f;border-radius:0;color:#263238}
.fr-nota{font-style:normal;color:#3a464b;font-size:14px;line-height:1.5}
input::placeholder{color:#58666b;opacity:1}\ninput[list]::-webkit-calendar-picker-indicator{display:none !important}
.capa-opcao{height:70px;margin:4px;cursor:zoom-in;border:2px solid transparent;border-radius:2px;position:relative;transition:transform .18s ease}
.capa-opcao:hover{transform:scale(2);z-index:10}
.capa-opcao.escolhida{border-color:#1f3a5f}
#capa-preview,#capa-padrao-preview{height:336px !important;max-width:min(100%,280px);width:auto;object-fit:contain;margin:10px 8px;vertical-align:top}
.reader-hero{background:linear-gradient(135deg,#edf4f1,#f8f5ec);border:1px solid #d7e4dc;border-radius:10px;padding:20px;margin-bottom:16px}
.reader-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.reader-tile{background:#f8faf9;border:1px solid #e1e9e4;border-radius:8px;padding:14px}
.reader-status{display:inline-block;padding:4px 9px;border-radius:20px;background:#e8f3ed;color:#24543a;font-size:14px}
@media(max-width:600px){#capa-preview,#capa-padrao-preview{height:300px !important;max-width:100%}}
@media print{header,nav,.naoimprime,.btn,input,select{display:none !important}main{padding:0}}
"""


def page(titulo, corpo):
    session.setdefault('_csrf_token', secrets.token_urlsafe(32))
    links, info = '', ''
    if 'leitor_uid' in session:
        links = (f'<a href="{url_for("minha_conta")}">Minha biblioteca</a>'
                 f'<a href="{url_for("catalogo_leitor")}">Catálogo</a>')
        info = f'{esc(session.get("leitor_nome", "Leitor"))} · <form method="post" action="{url_for("sair")}" style="display:inline"><button class="btn">Sair</button></form>'
    if 'uid' in session:
        for k, v in AREAS.items():
            if can(k):
                cls = 'ativo' if request.endpoint == k else ''
                links += f'<a href="{url_for(k)}" class="{cls}">{v}</a>'
        links += f'<a href="{url_for("minha_conta")}">Área do leitor</a>'
        info = f'{esc(session["nome"])} · <form method="post" action="{url_for("sair")}" style="display:inline"><button class="btn">Sair</button></form>'
    avisos = ''.join(f'<div class="aviso">{esc(m)}</div>' for m in get_flashed_messages())
    bg = request.args.get('bg', 'f7e18025')
    if not re.fullmatch(r'(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})', bg):
        bg = 'f7e18025'
    css_bg = 'body{background:#' + bg + '}'
    pagina_classe = 'aba-leitores' if request.endpoint == 'leitores' else ''
    return ('<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(titulo)} · Biblioteca</title><style>{CSS}{css_bg}</style></head><body class="{pagina_classe}">'
            f'<header><strong>Biblioteca Pessoal</strong><span>{info}</span></header>'
            f'<nav>{links}</nav><main>{avisos}{corpo}</main>'
            f'{SCRIPT_BASE.replace("__CSRF_TOKEN__", esc(session["_csrf_token"]))}</body></html>')


SCRIPT_BASE = """
<script>
const csrfToken = "__CSRF_TOKEN__";
document.querySelectorAll('form[method=post i]').forEach(form => {
  if (!form.querySelector('input[name="_csrf_token"]')) {
    const token=document.createElement('input'); token.type='hidden'; token.name='_csrf_token'; token.value=csrfToken; form.appendChild(token);
  }
});
function semAcento(s){ return s.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase(); }
// Aviso antes de sair de um cadastro sem salvar (formulários com class form-cadastro)
let formSujo = false;
document.addEventListener('input', e => { if (e.target.closest('form.form-cadastro')) formSujo = true; });
document.addEventListener('change', e => { if (e.target.closest('form.form-cadastro')) formSujo = true; });
document.addEventListener('submit', () => { formSujo = false; });
document.addEventListener('click', e => {
  const a = e.target.closest('a[href]');
  if (formSujo && a && !a.getAttribute('href').startsWith('#')) {
    if (!confirm('Você tem dados não salvos. Sair mesmo assim? O que foi digitado será perdido.')) e.preventDefault();
    else formSujo = false;
  }
});
window.addEventListener('beforeunload', e => { if (formSujo) { e.preventDefault(); e.returnValue = ''; } });
// Busca instantânea: cada campo com class "filtro" esconde as linhas da tabela class "lista" que não batem.
document.addEventListener('input', function(e){
  if (!e.target.classList.contains('filtro')) return;
  const tabela = document.querySelector('table.lista');
  if (!tabela) return;
  const termo = semAcento(e.target.value);
  for (const tr of tabela.querySelectorAll('tr')) {
    if (tr.querySelector('th')) continue;
    tr.style.display = semAcento(tr.textContent).includes(termo) ? '' : 'none';
  }
});
</script>
"""


def capa_img(l):
    if not l['capa']:
        return f'<div class="capa-vazia">{esc(l["titulo"])[:40]}</div>'
    return f'<img class="capa capa-3d" src="{url_for("capa", nome=l["capa"])}">'


@app.route('/capas/<nome>')
def capa(nome):
    if re.fullmatch(r'cdd_\d{3}\.svg', nome):
        return send_from_directory(CDD_THUMBS, nome)
    return send_from_directory(CAPAS, nome)


@app.route('/cdd-thumbs/<codigo>')
def cdd_thumb(codigo):
    if not re.fullmatch(r'\d{3}', codigo):
        return '', 404
    return send_from_directory(CDD_THUMBS, f'cdd_{codigo}.svg')


# ---------------------------------------------------------------- login

@app.route('/')
def inicio():
    return redirect(url_for('minha_conta') if 'leitor_uid' in session else
                        url_for('acervo') if 'uid' in session else url_for('entrar'))


@app.route('/entrar', methods=['GET', 'POST'])
def entrar():
    if request.method == 'POST':
        u = row("SELECT * FROM usuarios WHERE lower(nome) = lower(?)", ((request.form.get('nome') or '').strip(),))
        pw = request.form.get('senha', '')
        ok = u is not None and bool(u['senha']) and check_password_hash(u['senha'], pw)
        if ok:
            session.clear()
            session.update(uid=u['id'], nome=u['nome'], admin=bool(u['admin']),
                           perms=[p for p in (u['perms'] or '').split(',') if p],
                           senha_temporaria=bool(u['senha_temporaria']))
            log('Entrou no sistema')
            return redirect(url_for('trocar_senha_usuario') if u['senha_temporaria'] else url_for('acervo'))
        flash('Senha incorreta.')
    corpo = (f'<div class="card" style="max-width:360px;margin:40px auto"><h3>Acesso da equipa</h3>'
             f'<form method="post"><label>Utilizador<br><input name="nome" required autocomplete="username"></label><br>'
             f'<input name="senha" type="password" placeholder="Senha" autocomplete="current-password" required><br>'
             f'<button class="btn">Entrar</button></form>'
             f"<p><a href=\"{url_for('minha_conta')}\">Acesso à área do leitor</a></p></div>")
    return page('Entrar', corpo)


@app.route('/equipa/minha-senha', methods=['GET','POST'])
def trocar_senha_usuario():
    if 'uid' not in session:
        return redirect(url_for('entrar'))
    if request.method == 'POST':
        nova=request.form.get('nova') or ''
        confirmacao=request.form.get('confirmacao') or ''
        if len(nova)<8:
            flash('A senha deve ter pelo menos 8 caracteres.')
        elif nova!=confirmacao:
            flash('A confirmação da senha não coincide.')
        else:
            run('UPDATE usuarios SET senha=?,senha_temporaria=0 WHERE id=?',(generate_password_hash(nova),session['uid']))
            session['senha_temporaria']=False
            log('Definiu uma senha pessoal')
            flash('Senha definida. Já pode continuar.')
            return redirect(url_for('acervo'))
    corpo=f'''<div class="card" style="max-width:480px;margin:32px auto"><h2>Defina a sua senha</h2>
      <p>A senha temporária precisa ser substituída antes de continuar.</p>
      <form method="post"><label>Nova senha<br><input name="nova" type="password" minlength="8" required autocomplete="new-password"></label><br>
      <label>Confirme a senha<br><input name="confirmacao" type="password" minlength="8" required autocomplete="new-password"></label><br>
      <button class="btn">Guardar senha</button></form></div>'''
    return page('Definir senha',corpo)


@app.route('/sair', methods=['POST'])
def sair():
    era_leitor = 'leitor_uid' in session
    if 'uid' in session:
        log('Saiu do sistema')
    elif era_leitor:
        log('Saiu da área do leitor')
    session.clear()
    return redirect(url_for('minha_conta') if era_leitor else url_for('entrar'))


def leitor_logado():
    if 'leitor_uid' not in session:
        return None
    return row("SELECT * FROM leitores WHERE id = ?", (session['leitor_uid'],))


def exigir_leitor():
    if not leitor_logado():
        return redirect(url_for('minha_conta'))
    return None


@app.route('/minha-conta', methods=['GET', 'POST'])
def minha_conta():
    leitor = leitor_logado()
    if request.method == 'POST' and not leitor:
        identificador = (request.form.get('identificador') or '').strip()
        senha = request.form.get('senha') or ''
        encontrados = rows(
            "SELECT * FROM leitores WHERE matricula = ? OR "
            "(email <> '' AND lower(email) = lower(?))",
            (identificador, identificador))
        if len(encontrados) == 1 and encontrados[0]['senha'] and check_password_hash(encontrados[0]['senha'], senha):
            l = encontrados[0]
            session.clear()
            session.update(leitor_uid=l['id'], leitor_nome=l['nome'],
                           leitor_senha_temporaria=bool(l['senha_temporaria']))
            log(f'Entrou na área do leitor: {l["nome"]}')
            return redirect(url_for('minha_conta'))
        flash('Matrícula/e-mail ou senha incorretos. Se precisar, peça à biblioteca para redefinir a senha.')
    if not leitor:
        aviso_equipa = ('<div class="aviso">Está com a sessão da equipa aberta. Entre com a matrícula/e-mail e senha de um leitor para mudar para a área privada.</div>'
                        if 'uid' in session else '')
        corpo = f"""
        {aviso_equipa}
        <div class="card" style="max-width:440px;margin:40px auto">
          <h2>Minha biblioteca</h2>
          <p>Consulte os seus empréstimos, prazos, reservas e avisos.</p>
          <form method="post">
            <label>Matrícula ou e-mail<br><input name="identificador" autocomplete="username" required style="width:100%"></label><br>
            <label>Senha<br><input name="senha" type="password" autocomplete="current-password" required style="width:100%"></label><br>
            <button class="btn">Entrar</button>
          </form>
          <p class="fr-nota">A sessão termina quando o sistema é reiniciado. A senha temporária deve ser substituída antes de entrar na área privada.</p>
          <a href="{url_for('entrar')}">Acesso da equipa</a>
        </div>"""
        return page('Área do leitor', corpo)

    if leitor['senha_temporaria']:
        corpo = f'''<div class="card" style="max-width:480px;margin:32px auto"><h2>Defina a sua senha</h2>
          <p>Por segurança, a senha inicial deve ser substituída antes de usar a área privada.</p>
          <form method="post" action="{url_for('senha_propria_leitor')}">
            <input type="hidden" name="troca_inicial" value="1">
            <label>Nova senha<br><input name="nova" type="password" minlength="8" required autocomplete="new-password"></label><br>
            <label>Confirme a senha<br><input name="confirmacao" type="password" minlength="8" required autocomplete="new-password"></label><br>
            <button class="btn">Guardar senha</button>
          </form></div>'''
        return page('Definir senha', corpo)
    uid = leitor['id']
    emprestimos_atuais = rows(SQL_EMP + " WHERE e.leitor_id = ? AND e.devolucao IS NULL ORDER BY e.prevista", (uid,))
    emprestimos_antigos = rows(SQL_EMP + " WHERE e.leitor_id = ? AND e.devolucao IS NOT NULL ORDER BY e.devolucao DESC LIMIT 20", (uid,))
    reservas = rows("SELECT r.*, b.titulo, b.midia FROM reservas r LEFT JOIN livros b ON b.id = r.livro_id "
                    "WHERE r.leitor_id = ? AND r.status IN ('ativa','pronta') ORDER BY r.criada", (uid,))
    notif = rows("SELECT * FROM notificacoes WHERE leitor_id = ? ORDER BY id DESC LIMIT 30", (uid,))
    notif_novas = sum(1 for n in notif if not n['lida'])
    emprestimo_rows = ''.join(
        f"<tr class=\"{'atrasado' if situacao(e) == 'atrasado' else ''}\"><td>{esc(e['livro'])}</td>"
        f"<td>{fmt(e['retirada'])}</td><td>{fmt(e['prevista'])}</td>"
        f"<td><span class=\"reader-status\">{esc(ROTULO[situacao(e)])}</span></td><td>"
        + (post_btn(url_for('renovar_leitor', id=e['id']), 'Renovar')
           if e['renovacoes'] < MAX_RENOV and not tem_reserva(e['livro_id']) else '—')
        + f"</td></tr>" for e in emprestimos_atuais)
    if not emprestimo_rows:
        emprestimo_rows = '<tr><td colspan="5">Não tem empréstimos ativos.</td></tr>'
    reservas_rows = ''
    for r in reservas:
        pos = 'Pronta para retirar' if r['status'] == 'pronta' else str(
            [x['leitor_id'] for x in fila(r['livro_id'])].index(uid) + 1) + 'º na fila'
        reservas_rows += (f"<tr><td>{esc(r['titulo'] or 'Item removido')}</td><td>{esc(pos)}</td><td>"
                          f"{post_btn(url_for('cancelar_reserva_leitor', id=r['id']), 'Cancelar reserva', 'perigo', 'Cancelar esta reserva?')}</td></tr>")
    if not reservas_rows:
        reservas_rows = '<tr><td colspan="3">Não tem reservas ativas.</td></tr>'
    notificacoes_rows = ''.join(
        f"<tr><td>{esc(n['data'])}</td><td>{esc(n['texto'])}</td><td>{'Lida' if n['lida'] else 'Nova'}</td></tr>"
        for n in notif)
    if not notificacoes_rows:
        notificacoes_rows = '<tr><td colspan="3">Ainda não há notificações.</td></tr>'
    historico_rows = ''.join(
        f"<tr><td>{esc(e['livro'])}</td><td>{fmt(e['retirada'])}</td><td>{fmt(e['devolucao'])}</td></tr>"
        for e in emprestimos_antigos) or '<tr><td colspan="3">O histórico aparecerá aqui.</td></tr>'
    alerta = ('<div class="aviso">Está a usar a senha inicial de teste. Altere-a abaixo.</div>'
              if leitor['senha_temporaria'] else '')
    corpo = f"""
    <section class="reader-hero">
      <h1>Olá, {esc(leitor['nome'])}</h1>
      <p>Veja os seus prazos e reservas num só lugar.</p>
      <div class="reader-grid">
        <div class="reader-tile"><strong>{len(emprestimos_atuais)}</strong><br>empréstimo(s) ativo(s)</div>
        <div class="reader-tile"><strong>{len(reservas)}</strong><br>reserva(s) ativa(s)</div>
        <div class="reader-tile"><strong>{notif_novas}</strong><br>aviso(s) por ler</div>
      </div>
      <p><a class="btn" href="{url_for('catalogo_leitor')}">Pesquisar no catálogo</a></p>
    </section>
    {alerta}
    <div class="card"><h2>Meus empréstimos</h2>
      <table><tr><th>Item</th><th>Retirado</th><th>Devolução prevista</th><th>Situação</th><th></th></tr>{emprestimo_rows}</table>
      <p class="fr-nota">Renovação conforme as regras da biblioteca; não é permitida se outra pessoa reservou o item.</p>
    </div>
    <div class="card"><h2>Minhas reservas</h2>
      <table><tr><th>Item</th><th>Situação</th><th></th></tr>{reservas_rows}</table>
    </div>
    <div class="card"><h2>Avisos</h2>
      {f'<form method="post" action="{url_for("notificacoes_lidas")}"><button class="btn">Marcar avisos como lidos</button></form>' if notif_novas else ''}
      <table><tr><th>Data</th><th>Mensagem</th><th>Estado</th></tr>{notificacoes_rows}</table>
    </div>
    <div class="card"><h2>Empréstimos anteriores</h2>
      <details><summary>Mostrar histórico</summary>
      <table><tr><th>Item</th><th>Retirada</th><th>Devolução</th></tr>{historico_rows}</table></details>
    </div>
    <div class="card"><h2>Alterar senha</h2>
      <form method="post" action="{url_for('senha_propria_leitor')}">
        <label>Senha atual <input type="password" name="atual" required autocomplete="current-password"></label>
        <label>Nova senha <input type="password" name="nova" minlength="8" required autocomplete="new-password"></label>
        <label>Confirmar senha <input type="password" name="confirmacao" minlength="8" required autocomplete="new-password"></label>
        <button class="btn">Guardar nova senha</button>
      </form>
    </div>"""
    return page('Minha biblioteca', corpo)


@app.route('/catalogo')
def catalogo_leitor():
    if not leitor_logado():
        return redirect(url_for('minha_conta'))
    q = norm(request.args.get('q', ''))
    livros = rows(SQL_LIVROS + " ORDER BY l.titulo COLLATE NOCASE")
    if q:
        livros = [b for b in livros if q in norm(' '.join(str(b[c] or '') for c in
                  ['titulo', 'subtitulo', 'autor', 'coautor', 'editora', 'cdd', 'midia']))]
    trs = ''
    for b in livros:
        acao = ('<span class="reader-status">Disponível</span>' if b['disp'] > 0 else
                post_btn(url_for('reservar_leitor', id=b['id']), 'Reservar fila de espera'))
        trs += (f"<tr><td>{capa_img(b)}</td><td><strong>{esc(b['titulo'])}</strong>"
                f"<br>{esc(b['autor'])}</td><td>{esc(b['midia'])}</td><td>{esc(b['cdd'])}</td>"
                f"<td>{acao}</td></tr>")
    if not trs:
        trs = '<tr><td colspan="5">Nenhum item corresponde à pesquisa.</td></tr>'
    corpo = f"""
    <div class="card"><h1>Catálogo</h1>
      <form method="get"><input name="q" value="{esc(request.args.get('q',''))}" placeholder="Título, autor, assunto ou CDD" style="width:min(100%,480px)">
      <button class="btn">Pesquisar</button></form>
      <p class="fr-nota">{len(livros)} resultado(s). A reserva fica disponível quando todos os exemplares estiverem emprestados.</p>
      <table><tr><th></th><th>Obra / autor</th><th>Mídia</th><th>CDD</th><th>Disponibilidade / ação</th></tr>{trs}</table>
    </div>"""
    return page('Catálogo', corpo)


@app.route('/minha-conta/senha', methods=['POST'])
def senha_propria_leitor():
    leitor = leitor_logado()
    if not leitor:
        return redirect(url_for('minha_conta'))
    atual = request.form.get('atual') or ''
    nova = request.form.get('nova') or ''
    confirmacao = request.form.get('confirmacao') or ''
    troca_inicial = bool(leitor['senha_temporaria']) and request.form.get('troca_inicial') == '1'
    if not troca_inicial and not check_password_hash(leitor['senha'], atual):
        flash('A senha atual não confere.')
    elif len(nova) < 8:
        flash('A nova senha precisa de pelo menos 8 caracteres.')
    elif nova != confirmacao:
        flash('A confirmação da senha não coincide.')
    else:
        run("UPDATE leitores SET senha = ?, senha_temporaria = 0 WHERE id = ?",
            (generate_password_hash(nova), leitor['id']))
        log(f"Alterou senha na área do leitor: {leitor['nome']}")
        session['leitor_senha_temporaria'] = False
        flash('Senha alterada.')
    return redirect(url_for('minha_conta'))


@app.route('/minha-conta/renovar/<int:id>', methods=['POST'])
def renovar_leitor(id):
    leitor=leitor_logado()
    if not leitor:
        return redirect(url_for('minha_conta'))
    with transacao() as c:
        e=c.execute(SQL_EMP+' WHERE e.id=? AND e.leitor_id=? AND e.devolucao IS NULL',(id,leitor['id'])).fetchone()
        if not e:
            flash('Este empréstimo não está ativo na sua conta.')
        elif c.execute("SELECT 1 FROM reservas WHERE livro_id=? AND status IN ('ativa','pronta')",(e['livro_id'],)).fetchone():
            flash('Não é possível renovar: outra pessoa reservou este item.')
        elif e['renovacoes']>=MAX_RENOV:
            flash('Limite de renovações atingido.')
        else:
            base=max(date.fromisoformat(e['prevista']),date.today())
            nova=(base+timedelta(days=PRAZO)).isoformat()
            c.execute('UPDATE emprestimos SET prevista=?,renovacoes=renovacoes+1 WHERE id=? AND leitor_id=? AND devolucao IS NULL',(nova,id,leitor['id']))
            log(f"Renovou '{e['livro']}' pelo portal do leitor",c)
            flash('Empréstimo renovado.')
    return redirect(url_for('minha_conta'))

@app.route('/minha-conta/reservar/<int:id>', methods=['POST'])
def reservar_leitor(id):
    leitor=leitor_logado()
    if not leitor:
        return redirect(url_for('minha_conta'))
    try:
        with transacao() as c:
            livro=c.execute('SELECT titulo FROM livros WHERE id=?',(id,)).fetchone()
            if not livro:
                flash('Item não encontrado.')
                return redirect(url_for('catalogo_leitor'))
            if disponibilidade_db(c,id)[0]>0:
                flash('O item ainda está disponível; a reserva abre quando todos os exemplares forem emprestados.')
                return redirect(url_for('catalogo_leitor'))
            if c.execute('SELECT 1 FROM emprestimos WHERE livro_id=? AND leitor_id=? AND devolucao IS NULL',(id,leitor['id'])).fetchone():
                flash('Já tem este item emprestado.')
                return redirect(url_for('catalogo_leitor'))
            if c.execute("SELECT 1 FROM reservas WHERE livro_id=? AND leitor_id=? AND status IN ('ativa','pronta')",(id,leitor['id'])).fetchone():
                flash('Já tem uma reserva ativa deste item.')
                return redirect(url_for('catalogo_leitor'))
            c.execute("INSERT INTO reservas(livro_id,leitor_id,criada,status) VALUES (?,?,?,'ativa')",(id,leitor['id'],datetime.now().isoformat()))
            pos=c.execute("SELECT COUNT(*) FROM reservas WHERE livro_id=? AND status='ativa'",(id,)).fetchone()[0]
            texto=f"Reserva registada para '{livro['titulo']}'. Está em {pos}º lugar na fila."
            inserir_notificacao_db(c,leitor['id'],texto)
            log(f"Reservou '{livro['titulo']}' pelo portal do leitor",c)
    except sqlite3.IntegrityError:
        flash('A reserva não pôde ser criada. Atualize a página e tente novamente.')
        return redirect(url_for('catalogo_leitor'))
    enviar_email(leitor['id'],texto)
    avisar_fila(id)
    flash(f'Reserva registada: {pos}º na fila.')
    return redirect(url_for('catalogo_leitor'))

@app.route('/minha-conta/reservas/<int:id>/cancelar', methods=['POST'])
def cancelar_reserva_leitor(id):
    leitor=leitor_logado()
    if not leitor:
        return redirect(url_for('minha_conta'))
    avisos=[]; livro_id=None
    with transacao() as c:
        r=c.execute("SELECT * FROM reservas WHERE id=? AND leitor_id=? AND status IN ('ativa','pronta')",(id,leitor['id'])).fetchone()
        if r:
            livro_id=r['livro_id']
            c.execute("UPDATE reservas SET status='cancelada' WHERE id=? AND leitor_id=?",(id,leitor['id']))
            log('Cancelou reserva na área do leitor',c)
            avisos=promover_db(c,livro_id)
    enviar_avisos(avisos)
    if livro_id:
        avisar_fila(livro_id)
        flash('Reserva cancelada.')
    return redirect(url_for('minha_conta'))

@app.route('/minha-conta/avisos/lidos', methods=['POST'])
def notificacoes_lidas():
    leitor = leitor_logado()
    if not leitor:
        return redirect(url_for('minha_conta'))
    run("UPDATE notificacoes SET lida = 1 WHERE leitor_id = ?", (leitor['id'],))
    return redirect(url_for('minha_conta'))


# ---------------------------------------------------------------- acervo

SCRIPT_CDD = """
<script>
async function sugerir(){
  const titulo = document.getElementById('titulo').value;
  const autor = document.getElementById('autor').value;
  const box = document.getElementById('sugestao');
  if ((titulo + autor).trim().length < 3) return;
  box.textContent = 'Consultando sinônimos e títulos do seu acervo...';
  const params = new URLSearchParams({titulo, autor});
  const r = await fetch('/api/cdd?' + params.toString());
  const j = await r.json();
  box.textContent = '';
  if (j.sugestoes && j.sugestoes.length) {
    box.append('Sugestões CDD (base e acervo): ');
    j.sugestoes.forEach((item, i) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'btn';
      b.textContent = item.referencia
        ? item.assunto + ': ' + item.referencia + ' — CDD ' + item.cdd
        : item.assunto + ' — ' + item.cdd;
      b.title = item.referencia || ('Correspondência: ' + item.palavra);
      b.onclick = () => { document.getElementById('cdd').value = item.cdd; atualizarPreviewCapaPadrao(); };
      box.append(b);
      if (i === 0 && item.preencher && !cddEditadoPeloUsuario) {
        document.getElementById('cdd').value = item.cdd;
        atualizarPreviewCapaPadrao();
      }
    });
  } else {
    box.textContent = 'Sem correspondência na base ou no acervo. Preencha a CDD manualmente.';
    if (!cddEditadoPeloUsuario) document.getElementById('cdd').value = '';
  }
}
function mostrarOpcaoCapaPadrao(){
  const label = document.getElementById('capa-padrao-label');
  if (label) label.style.display = 'block';
}
function atualizarPreviewCapaPadrao(){
  const check = document.getElementById('usar_capa_cdd');
  const img = document.getElementById('capa-padrao-preview');
  const info = document.getElementById('capa-padrao-info');
  if (!check || !img) return;
  if (!check.checked) { img.style.display = 'none'; if (info) info.textContent = ''; return; }
  const valor = document.getElementById('cdd').value;
  const m = valor.match(/(?:^|[^0-9])(?:B)?(\\d{3})/i);
  if (!m) { img.style.display = 'none'; if (info) info.textContent = 'Preencha a CDD para ver a imagem temática.'; return; }
  const codigo = m[1];
  img.src = '/cdd-thumbs/' + codigo;
  img.alt = 'Imagem temática da classe CDD ' + codigo;
  img.style.display = 'block';
  if (info) info.textContent = 'Imagem ilustrativa da classe CDD ' + codigo + ' — não é a capa real.';
  img.onerror = () => { img.style.display = 'none'; if (info) info.textContent = 'Não há imagem temática para esta classe CDD.'; };
}
async function buscarCapas(){
  const t = document.getElementById('titulo').value, a = document.getElementById('autor').value;
  const isbn = document.getElementById('isbn').value;
  const box = document.getElementById('capas-sugeridas');
  if (!t.trim() && !a.trim() && !isbn.trim()) { box.textContent = 'Digite o título, autor ou ISBN antes de buscar.'; return; }
  box.textContent = 'Buscando capas...';
  const r = await fetch('/api/capas?' + new URLSearchParams({titulo:t, autor:a, isbn}));
  const j = await r.json();
  box.textContent = '';
  if (!j.capas.length) {
    box.textContent = j.erro || 'Nenhuma capa encontrada.';
    mostrarOpcaoCapaPadrao();
    return;
  }
  j.capas.forEach(c => {
    const img = document.createElement('img');
    img.src = c.thumb; img.className = 'capa-opcao'; img.alt = c.titulo + ' — ' + c.autor;
    img.title = (c.origem || 'Fonte') + ': ' + c.titulo + ' — ' + c.autor;
    img.onclick = () => {
      document.getElementById('capa_url').value = c.grande || c.thumb;
      const p = document.getElementById('capa-preview');
      p.src = c.grande || c.thumb; p.style.display = '';
      const check = document.getElementById('usar_capa_cdd');
      if (check) check.checked = false;
      atualizarPreviewCapaPadrao();
      box.querySelectorAll('img').forEach(i => i.classList.remove('escolhida'));
      img.classList.add('escolhida');
    };
    box.append(img);
  });
  box.append(document.createTextNode(' Clique numa capa para pré-visualizar. Se nenhuma for a correta, escolha a imagem temática da CDD.'));
  mostrarOpcaoCapaPadrao();
}
let cddEditadoPeloUsuario = false;
let timerSugestao = null;
document.addEventListener('DOMContentLoaded', () => {
  const c = document.getElementById('cdd');
  if (c && c.value) cddEditadoPeloUsuario = true;   // registro já salvo: não trocar sozinho
  if (c) c.addEventListener('input', () => { cddEditadoPeloUsuario = true; atualizarPreviewCapaPadrao(); });
  const usarPadrao = document.getElementById('usar_capa_cdd');
  if (usarPadrao) usarPadrao.addEventListener('change', atualizarPreviewCapaPadrao);
  const arquivoCapa = document.querySelector('input[name="capa"]');
  const converterCapa = document.getElementById('converter-capa-webp');
  const estadoConversao = document.getElementById('estado-conversao-capa');
  let arquivoOriginalCapa = null;
  let tentativaConversao = 0;
  const tamanhoLegivel = bytes => bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  async function criarWebp(file) {
    const url = URL.createObjectURL(file);
    try {
      const imagem = new Image(); imagem.src = url; await imagem.decode();
      const canvas = document.createElement('canvas');
      canvas.width = imagem.naturalWidth; canvas.height = imagem.naturalHeight;
      const contexto = canvas.getContext('2d');
      if (!contexto) throw new Error('Não foi possível preparar a imagem para conversão.');
      contexto.drawImage(imagem, 0, 0);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/webp', 0.86));
      if (!blob || blob.type !== 'image/webp') throw new Error('Este navegador não conseguiu criar WebP.');
      const baseNome = file.name.replace(/\\.[^.]+$/, '') || 'capa';
      return new File([blob], `${baseNome}.webp`, {type: 'image/webp', lastModified: Date.now()});
    } finally { URL.revokeObjectURL(url); }
  }
  async function atualizarArquivoCapa() {
    if (!arquivoCapa) return;
    const tentativa = ++tentativaConversao, original = arquivoOriginalCapa;
    if (!original) { if (estadoConversao) estadoConversao.textContent = ''; return; }
    if (usarPadrao) usarPadrao.checked = false;
    if (!converterCapa || !converterCapa.checked || original.type === 'image/webp') {
      const lista = new DataTransfer(); lista.items.add(original); arquivoCapa.files = lista.files;
      if (estadoConversao) estadoConversao.textContent = original.type === 'image/webp'
        ? `O ficheiro já é WebP (${tamanhoLegivel(original.size)}).`
        : `Formato original mantido (${tamanhoLegivel(original.size)}).`;
      atualizarPreviewCapaPadrao(); return;
    }
    if (estadoConversao) estadoConversao.textContent = 'A converter nesta máquina…';
    try {
      const convertido = await criarWebp(original);
      if (tentativa !== tentativaConversao) return;
      const lista = new DataTransfer(); lista.items.add(convertido); arquivoCapa.files = lista.files;
      const reducao = original.size ? Math.round((1 - convertido.size / original.size) * 100) : 0;
      if (estadoConversao) estadoConversao.textContent = `WebP criado: ${tamanhoLegivel(convertido.size)} (${reducao > 0 ? `${reducao}% menor` : 'tamanho semelhante'}).`;
    } catch (erro) {
      if (tentativa !== tentativaConversao) return;
      const lista = new DataTransfer(); lista.items.add(original); arquivoCapa.files = lista.files;
      if (estadoConversao) estadoConversao.textContent = `${erro.message} O ficheiro original foi mantido.`;
    }
    atualizarPreviewCapaPadrao();
  }
  if (arquivoCapa) arquivoCapa.addEventListener('change', () => {
    arquivoOriginalCapa = arquivoCapa.files[0] || null; atualizarArquivoCapa();
  });
  if (converterCapa) converterCapa.addEventListener('change', atualizarArquivoCapa);
  if (usarPadrao && usarPadrao.checked) atualizarPreviewCapaPadrao();
  ['titulo', 'autor'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', () => {
      clearTimeout(timerSugestao);
      timerSugestao = setTimeout(sugerir, 400);
    });
  });
});
</script>
"""


CAPA_HOSTS = {'books.google.com', 'books.googleusercontent.com', 'covers.openlibrary.org'}
MAX_CAPA_BYTES = 5 * 1024 * 1024


def _url_capa_permitida(url):
    p = urllib.parse.urlparse(url)
    try: porta_ok = p.port in (None, 443)
    except ValueError: return False
    return p.scheme == 'https' and not p.username and not p.password and porta_ok and (p.hostname or '').lower().rstrip('.') in CAPA_HOSTS


class RedirecionamentoCapas(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_capa_permitida(newurl):
            raise urllib.error.HTTPError(newurl, code, 'Redirecionamento para domínio não autorizado', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validar_imagem(dados, content_type=''):
    tipo = (content_type or '').split(';', 1)[0].strip().lower()
    if dados.startswith(b'\x89PNG\r\n\x1a\n') and dados[12:16] == b'IHDR': ext, mime = 'png', 'image/png'
    elif dados.startswith(b'\xff\xd8\xff') and dados.endswith(b'\xff\xd9'): ext, mime = 'jpg', 'image/jpeg'
    elif dados[:6] in (b'GIF87a', b'GIF89a'): ext, mime = 'gif', 'image/gif'
    elif dados[:4] == b'RIFF' and dados[8:12] == b'WEBP': ext, mime = 'webp', 'image/webp'
    else: return None
    if tipo and tipo not in (mime, 'image/jpg'):
        return None
    return ext


def _http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'BibliotecaPessoal/1.0'})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode('utf-8'))


def _https(u):
    return 'https://' + u[len('http://'):] if u.startswith('http://') else u


def buscar_capas(titulo, autor, isbn=''):
    """Busca em Google Books e Open Library, priorizando ISBN e reportando falhas."""
    titulo, autor, isbn = (titulo or '').strip(), (autor or '').strip(), (isbn or '').strip()
    isbn_limpo = re.sub(r'[^0-9Xx]', '', isbn)
    if not titulo and not autor and not isbn_limpo:
        return [], 'Digite o título, o autor ou o ISBN.'
    resultados, erros, vistos = [], [], set()
    consultas_google = []
    if isbn_limpo: consultas_google.append({'q': f'isbn:{isbn_limpo}', 'maxResults': 6})
    if titulo or autor:
        q = ((f'intitle:{titulo} ' if titulo else '') + (f'inauthor:{autor}' if autor else '')).strip()
        consultas_google.append({'q': q, 'maxResults': 6})
        if titulo: consultas_google.append({'q': f'intitle:{titulo}', 'maxResults': 6})
    for consulta in consultas_google:
        try:
            dados = _http_json('https://www.googleapis.com/books/v1/volumes?' + urllib.parse.urlencode(consulta))
            for it in dados.get('items', []):
                v = it.get('volumeInfo', {})
                links = v.get('imageLinks', {})
                thumb = links.get('thumbnail') or links.get('smallThumbnail')
                if not thumb: continue
                chave = it.get('id') or thumb
                if chave in vistos: continue
                vistos.add(chave)
                grande = next((links[k] for k in ('extraLarge', 'large', 'medium') if links.get(k)), thumb)
                resultados.append({'titulo': v.get('title', ''), 'autor': ', '.join(v.get('authors', [])),
                                   'thumb': _https(thumb), 'grande': _https(grande), 'origem': 'Google Books'})
                if len(resultados) >= 6: break
        except Exception as ex:
            erros.append('Google Books: ' + str(ex))
        if len(resultados) >= 6: break
    consultas_ol = []
    if isbn_limpo: consultas_ol.append({'isbn': isbn_limpo, 'limit': 6})
    if titulo or autor:
        consulta = {'limit': 6}
        if titulo: consulta['title'] = titulo
        if autor: consulta['author'] = autor
        consultas_ol.append(consulta)
        if titulo: consultas_ol.append({'title': titulo, 'limit': 6})
    for consulta in consultas_ol:
        try:
            dados = _http_json('https://openlibrary.org/search.json?' + urllib.parse.urlencode(consulta))
            for d in dados.get('docs', []):
                cid = d.get('cover_i')
                if not cid: continue
                chave = f'openlibrary:{cid}'
                if chave in vistos: continue
                vistos.add(chave)
                resultados.append({'titulo': d.get('title', ''),
                                   'autor': ', '.join(d.get('author_name', [])[:2]),
                                   'thumb': f'https://covers.openlibrary.org/b/id/{cid}-M.jpg',
                                   'grande': f'https://covers.openlibrary.org/b/id/{cid}-L.jpg',
                                   'origem': 'Open Library'})
                if len(resultados) >= 6: break
        except Exception as ex:
            erros.append('Open Library: ' + str(ex))
        if len(resultados) >= 6: break
    if not resultados:
        detalhe = '; '.join(erros) if erros else 'As fontes responderam, mas não encontraram capas para este livro.'
        return [], detalhe + ' Você também pode enviar uma imagem.'
    return resultados, ''

def baixar_capa(url, livro_id):
    """Baixa apenas HTTPS de hosts exatos e valida tamanho, MIME e assinatura real."""
    if not url:
        return None
    if not _url_capa_permitida(url):
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'BibliotecaPessoal/1.0'})
        opener = urllib.request.build_opener(RedirecionamentoCapas())
        with opener.open(req, timeout=15) as r:
            if not _url_capa_permitida(r.geturl()):
                return None
            tipo = r.headers.get('Content-Type', '')
            tamanho = r.headers.get('Content-Length')
            if tamanho and int(tamanho) > MAX_CAPA_BYTES:
                return None
            dados = r.read(MAX_CAPA_BYTES + 1)
        if len(dados) > MAX_CAPA_BYTES:
            return None
        ext = validar_imagem(dados, tipo)
        if not ext:
            return None
        nome = f'{livro_id}.{ext}'
        destino = os.path.join(CAPAS, nome)
        with open(destino, 'wb') as f:
            f.write(dados)
        return nome
    except Exception:
        return None


@app.route('/api/capas')
def api_capas():
    if 'uid' not in session:
        return {'capas': [], 'erro': 'Entre no sistema.'}
    capas, erro = buscar_capas(request.args.get('titulo', ''), request.args.get('autor', ''), request.args.get('isbn', ''))
    return {'capas': capas, 'erro': erro}


@app.route('/api/cdd')
def api_cdd():
    if 'uid' not in session:
        return {'cdd': '', 'palavra': '', 'sugestoes': []}
    titulo = request.args.get('titulo', '').strip()
    autor = request.args.get('autor', '').strip()
    q = request.args.get('q', '').strip() or f'{titulo} {autor}'
    sugestoes = sugerir_cdd_lista(q, titulo=titulo)
    primeira = sugestoes[0] if sugestoes else {}
    return {'cdd': primeira.get('cdd', ''), 'palavra': primeira.get('palavra', ''),
            'sugestoes': sugestoes}


def form_livro(v, action):
    v = dict(v) if v else {}
    def campo(nome, rotulo, extra=''):
        return (f'<label class="fr-campo">{rotulo}<input name="{nome}" id="{nome}" '
                f'value="{esc(str(v.get(nome) or ""))}" {extra}></label>')
    midias = ''.join(f"<option value='{m}' {'selected' if (v.get('midia') or 'Livro') == m else ''}>{m}</option>"
                     for m in MIDIAS)
    padrao_ativo = bool(re.fullmatch(r'cdd_\d{3}\.svg', str(v.get('capa') or '')))
    padrao_checked = 'checked' if padrao_ativo else ''
    padrao_visivel = 'block' if padrao_ativo else 'none'
    padrao_src = url_for('capa', nome=v['capa']) if padrao_ativo else ''
    if v.get('capa'):
        capa = f'<img class="capa capa-3d" src="{url_for("capa", nome=v["capa"])}">'
    else:
        capa = '<div class="capa-vazia">sem capa</div>'
    return f"""
    <form method="post" action="{action}" enctype="multipart/form-data" class="folha form-cadastro">
      <div class="folha-topo">
        <div class="folha-capa">{capa}<br><input type="file" name="capa" accept="image/*">
          <label class="fr-nota"><input type="checkbox" id="converter-capa-webp"> Converter a imagem para WebP neste computador</label>
          <small class="fr-nota">Opcional: o navegador converte a imagem antes de a guardar, sem instalar outro programa. WebP costuma ocupar menos espaço; SVG não é adequado para fotografias de capas.</small>
          <small id="estado-conversao-capa" class="fr-nota" aria-live="polite"></small>
          <br><button type="button" class="btn" onclick="buscarCapas()">Buscar capa na internet</button>
          <input type="hidden" name="capa_url" id="capa_url">
          <img id="capa-preview" class="capa capa-3d" style="display:none">
          <label id="capa-padrao-label" class="fr-nota" style="display:{padrao_visivel}"><input type="checkbox" name="usar_capa_cdd" id="usar_capa_cdd" value="1" {padrao_checked}> Usar imagem temática da CDD se nenhuma capa estiver correta.</label>
          <img id="capa-padrao-preview" class="capa capa-3d" src="{padrao_src}" alt="Imagem temática da CDD" style="display:{'block' if padrao_ativo else 'none'};height:150px">
          <small id="capa-padrao-info"></small>
          <div id="capas-sugeridas"></div>
        </div>
        <div class="folha-texto">
          <div class="fr-linha">{campo('autor', 'Autor(a)', 'onchange="sugerir()"')}</div>
          <div class="fr-linha">{campo('titulo', 'Título *', 'required onchange="sugerir()"')}</div>
          <div class="fr-linha">{campo('subtitulo', 'Subtítulo (se houver)')}</div>
          <div class="fr-linha">{campo('edicao', 'Edição (ex.: 2ª)')}</div>
          <div class="fr-linha">{campo('local_publicacao', 'Local de publicação')}</div>
          <div class="fr-linha">{campo('editora', 'Editor')}</div>
          <div class="fr-linha">{campo('ano', 'Ano de publicação')}</div>
          <div class="fr-linha">{campo('isbn', 'ISBN (opcional)')}</div>
        </div>
      </div>
      <p class="fr-nota">Dados de catalogação e exemplar:</p>
      <div class="grid">
        {campo('coautor', 'Coautor(es)')}
        {campo('volume', 'Volume / versão')}
        <label>Mídia<select name="midia">{midias}</select></label>
        {campo('cdd', 'CDD (editável)')}
        {campo('chamada', 'Nº chamada (vazio = automático)')}
        {campo('exemplares', 'Exemplares', 'type="number" min="1"')}
      </div>
      <div id="sugestao" class="aviso">Digite o título e o autor para ver a sugestão de CDD.</div>
      <button type="button" class="btn" onclick="sugerir()">Sugerir CDD</button>
      <button class="btn">Salvar</button>
      <a class="btn" href="{url_for('acervo')}">Cancelar</a>
    </form>
    {SCRIPT_CDD}
    """


def dados_livro(f):
    d = {c: (f.get(c) or '').strip() for c in CAMPOS_PLANILHA}
    d['midia'] = d['midia'] or 'Livro'
    d['ano'] = re.sub(r'\.0$', '', d['ano'])
    d['exemplares'] = toint(f.get('exemplares'))
    d['chamada'] = (f.get('chamada') or '').strip() or gerar_chamada(d['cdd'], d['autor'])
    return d


def duplicado(d, ignorar_id=None, c=None):
    sql = "SELECT id FROM livros WHERE lower(titulo)=lower(?) AND lower(autor)=lower(?) AND midia=? AND id IS NOT ?"
    args = (d['titulo'], d['autor'], d['midia'], ignorar_id)
    return c.execute(sql, args).fetchone() if c is not None else row(sql, args)


def inserir_livro(d, c=None):
    cols = list(d.keys())
    sql = f"INSERT INTO livros({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    valores = [d[campo] for campo in cols]
    if c is not None:
        cur = c.execute(sql, valores)
        return cur.lastrowid
    return run(sql, valores)


def salvar_capa(arquivo, livro_id):
    """Valida o conteúdo da imagem e grava-a localmente com a extensão detetada."""
    if not arquivo or not arquivo.filename:
        return None
    dados = arquivo.stream.read(MAX_CAPA_BYTES + 1)
    if len(dados) > MAX_CAPA_BYTES:
        flash('A capa excede o limite de 5 MB; o registo será guardado sem essa imagem.')
        return None
    # A assinatura do ficheiro é a fonte de verdade; o nome pode ter extensão errada ou nenhuma.
    ext = validar_imagem(dados)
    if not ext:
        flash('Formato de capa não suportado. Use PNG, JPG, GIF ou WebP.')
        return None
    nome = f'{livro_id}.{ext}'
    with open(os.path.join(CAPAS, nome), 'wb') as f:
        f.write(dados)
    return nome

@app.route('/acervo')
@area('acervo')
def acervo():
    q_raw = request.args.get('q', '')
    q = norm(q_raw)
    lista = [l for l in rows(SQL_LIVROS + " ORDER BY l.titulo COLLATE NOCASE")
             if q in norm(' '.join(str(l[c] or '') for c in ['titulo', 'autor', 'coautor', 'editora', 'chamada', 'midia']))]
    trs = ''.join(f"""<tr>
        <td>{capa_img(l)}</td><td>{esc(l['titulo'])}</td><td>{esc(l['autor'])}</td>
        <td>{esc(l['midia'])}</td><td>{esc(l['cdd'])}</td><td>{esc(l['chamada'])}</td>
        <td>{l['disp']}/{l['exemplares']}</td>
        <td><a class="btn" href="{url_for('editar_livro', id=l['id'])}">Editar</a>
        {post_btn(url_for('excluir_livro', id=l['id']), 'Excluir', 'perigo', 'Excluir este registro?')}</td>
        </tr>""" for l in lista)
    corpo = f"""
    <div class="card">
      <a class="btn" href="{url_for('novo_livro')}">+ Novo livro / item</a>
      <input class="filtro" placeholder="Buscar enquanto digita..." style="float:right;width:300px">
    </div>
    <div class="card">
      <table class="lista">
        <tr><th></th><th>Título</th><th>Autor</th><th>Mídia</th><th>CDD</th><th>Chamada</th><th>Disp./Total</th><th></th></tr>
        {trs}
      </table>
    </div>
    <div class="card">
      <h3>Importar planilha (CSV ou Excel)</h3>
      <p>Colunas aceitas: titulo, autor, coautor, editora, ano, edicao, volume, isbn, cdd, midia, exemplares.
         Títulos já existentes (mesmo título, autor e mídia) são ignorados.</p>
      <form method="post" action="{url_for('importar_planilha')}" enctype="multipart/form-data">
        <input type="file" name="arquivo" accept=".csv,.xlsx"> <button class="btn">Importar</button>
      </form>
    </div>"""
    return page('Acervo', corpo)


@app.route('/acervo/novo', methods=['GET', 'POST'])
@area('acervo')
def novo_livro():
    if request.method == 'POST':
        d = dados_livro(request.form)
        if not d['titulo']:
            flash('Informe o título.')
            return redirect(url_for('novo_livro'))
        if duplicado(d):
            flash('Já existe este título, autor e mídia. Use Editar para ajustar os exemplares.')
            return redirect(url_for('acervo'))
        novo_id = inserir_livro(d)
        capa_nome = salvar_capa(request.files.get('capa'), novo_id) or baixar_capa(request.form.get('capa_url', ''), novo_id)
        if not capa_nome and request.form.get('usar_capa_cdd'):
            capa_nome = capa_padrao_cdd(d['cdd'])
            if not capa_nome:
                flash('Não há imagem temática cadastrada para esta CDD; o livro foi salvo sem capa.')
        if capa_nome:
            run("UPDATE livros SET capa = ? WHERE id = ?", (capa_nome, novo_id))
        log(f'Cadastrou item ({d["midia"]}): {d["titulo"]}')
        flash('Cadastrado.')
        return redirect(url_for('acervo'))
    return page('Novo livro', form_livro({}, url_for('novo_livro')))


@app.route('/acervo/<int:id>/editar', methods=['GET', 'POST'])
@area('acervo')
def editar_livro(id):
    l = row("SELECT * FROM livros WHERE id = ?", (id,))
    if not l:
        flash('Item não encontrado.')
        return redirect(url_for('acervo'))
    if request.method == 'POST':
        d = dados_livro(request.form)
        if not d['titulo']:
            flash('Informe o título.')
            return redirect(url_for('editar_livro', id=id))
        if duplicado(d, id):
            flash('Já existe outro registro com este título, autor e mídia.')
            return redirect(url_for('editar_livro', id=id))
        ocupados = row("SELECT (SELECT COUNT(*) FROM emprestimos WHERE livro_id=? AND devolucao IS NULL) + (SELECT COUNT(*) FROM reservas WHERE livro_id=? AND status='pronta') AS n", (id,id))['n']
        try:
            bruto = int(float(request.form.get('exemplares') or 0))
        except ValueError:
            bruto = 0
        if bruto < 1:
            flash('O número de exemplares deve ser pelo menos 1.')
            return redirect(url_for('editar_livro', id=id))
        if bruto < ocupados:
            flash(f'Não pode ser menor que os {ocupados} exemplar(es) emprestados ou reservados para retirada.')
            return redirect(url_for('editar_livro', id=id))
        sets = ', '.join(f'{k} = ?' for k in d)
        run(f"UPDATE livros SET {sets} WHERE id = ?", list(d.values()) + [id])
        capa_nome = salvar_capa(request.files.get('capa'), id) or baixar_capa(request.form.get('capa_url', ''), id)
        if not capa_nome and request.form.get('usar_capa_cdd'):
            capa_nome = capa_padrao_cdd(d['cdd'])
            if not capa_nome:
                flash('Não há imagem temática cadastrada para esta CDD; o item foi salvo sem capa.')
        if capa_nome:
            run("UPDATE livros SET capa = ? WHERE id = ?", (capa_nome, id))
        elif re.fullmatch(r'cdd_\d{3}\.svg', str(l['capa'] or '')) and not request.form.get('usar_capa_cdd'):
            run("UPDATE livros SET capa = '' WHERE id = ?", (id,))
        log(f'Alterou item: {d["titulo"]}')
        flash('Atualizado.')
        return redirect(url_for('acervo'))
    return page('Editar livro', form_livro(dict(l), url_for('editar_livro', id=id)))


@app.route('/acervo/<int:id>/excluir', methods=['POST'])
@area('acervo')
def excluir_livro(id):
    if row("SELECT 1 FROM emprestimos WHERE livro_id = ? UNION ALL SELECT 1 FROM reservas WHERE livro_id = ? LIMIT 1", (id,id)):
        flash('Não é possível excluir: o item tem histórico de empréstimos ou reservas.')
    else:
        l = row("SELECT titulo FROM livros WHERE id = ?", (id,))
        if l:
            run("DELETE FROM livros WHERE id = ?", (id,))
            log(f'Excluiu item: {l["titulo"]}')
    return redirect(url_for('acervo'))


def ler_planilha(arquivo):
    """Itera CSV/XLSX sem copiar o ficheiro inteiro para a memória."""
    nome = (arquivo.filename or '').lower()
    if nome.endswith('.xlsx'):
        try:
            import openpyxl
        except ImportError:
            raise ValueError('para Excel, instale o openpyxl: pip install openpyxl')
        arquivo.stream.seek(0)
        with zipfile.ZipFile(arquivo.stream) as pacote:
            descompactado = sum(info.file_size for info in pacote.infolist())
            if descompactado > 2 * 1024 * 1024 * 1024:
                raise ValueError('o conteúdo descompactado do Excel excede o limite de 2 GB')
        arquivo.stream.seek(0)
        wb = openpyxl.load_workbook(arquivo.stream, read_only=True, data_only=True)
        linhas = wb.active.iter_rows(values_only=True)
    elif nome.endswith('.csv'):
        arquivo.stream.seek(0)
        amostra = arquivo.stream.read(65536).decode('utf-8-sig', errors='replace')
        arquivo.stream.seek(0)
        try:
            dialeto = csv.Sniffer().sniff(amostra[:8192], delimiters=';,\t')
        except csv.Error:
            dialeto = csv.excel
        texto = io.TextIOWrapper(arquivo.stream, encoding='utf-8-sig', errors='replace', newline='')
        linhas = csv.reader(texto, dialeto)
        wb = None
    else:
        raise ValueError('formato não suportado; escolha CSV ou XLSX')
    try:
        cabecalho = next(linhas, None)
        if not cabecalho:
            return
        cab = [norm(c).replace(' ', '') for c in cabecalho]
        for r in linhas:
            item = {}
            for campo in CAMPOS_PLANILHA:
                if campo in cab:
                    i = cab.index(campo)
                    valor = r[i] if i < len(r) else ''
                    valor = '' if valor is None else str(valor).strip()
                    if len(valor) > MAX_CAMPO_PLANILHA:
                        raise ValueError(f'o campo {campo} excede 100 mil caracteres')
                    item[campo] = valor
            if item.get('titulo'):
                yield item
    finally:
        if wb is not None:
            wb.close()


@app.route('/importar-planilha', methods=['POST'])
@area('acervo')
def importar_planilha():
    arq = request.files.get('arquivo')
    if not arq or not arq.filename:
        flash('Escolha um arquivo .csv ou .xlsx.')
        return redirect(url_for('acervo'))
    novos = repetidos = thumbs = 0
    try:
        with transacao() as c:
            for it in ler_planilha(arq):
                d = dados_livro(it)
                if duplicado(d, c=c):
                    repetidos += 1
                    continue
                d['capa'] = capa_padrao_cdd(d.get('cdd')) or ''
                if d['capa']:
                    thumbs += 1
                inserir_livro(d, c=c)
                novos += 1
            log(f'Importou planilha: {novos} novos, {repetidos} repetidos', c)
    except Exception as ex:
        flash(f'Importação cancelada sem gravar alterações: {ex}')
        return redirect(url_for('acervo'))
    flash(f'Importação concluída: {novos} novos, {repetidos} já existiam, {thumbs} receberam thumb temática CDD.')
    return redirect(url_for('acervo'))


# ---------------------------------------------------------------- leitores

@app.route('/leitores')
@area('leitores')
def leitores():
    q_raw = request.args.get('q', '')
    q = norm(q_raw)
    lista = [l for l in rows("SELECT * FROM leitores ORDER BY nome COLLATE NOCASE")
             if q in norm(' '.join(str(l[c] or '') for c in ['nome', 'matricula', 'email', 'telefone']))]
    trs = ''.join(f"""<tr><td>{esc(l['nome'])}</td><td>{esc(l['matricula'])}</td>
        <td>{esc(l['email'])}</td><td>{esc(l['telefone'])}</td>
        <td>{post_btn(url_for('senha_leitor', id=l['id']), 'Gerar senha temporária', '', 'Gerar uma senha temporária única para este leitor?')}
        {post_btn(url_for('excluir_leitor', id=l['id']), 'Excluir', 'perigo', 'Excluir este leitor?')}</td></tr>"""
                  for l in lista)
    corpo = f"""
    <div class="card">
      <h3>Cadastrar leitor</h3>
      <form method="post" class="form-cadastro" action="{url_for('novo_leitor')}">
        <div class="grid">
          <input name="nome" placeholder="Nome *" required>
          <input name="matricula" placeholder="Matrícula / ID *" required>
          <input name="email" placeholder="E-mail">
          <input name="telefone" placeholder="Telefone">
        </div>
        <button class="btn">Gravar</button>
      </form>
    </div>
    <div class="card">
      <input class="filtro naoimprime" placeholder="Buscar enquanto digita...">
      <p class="fr-nota">Área privada: o leitor entra com matrícula (ou e-mail) e altera a senha temporária no primeiro acesso.</p>
      <table class="lista"><tr><th>Nome</th><th>Matrícula / ID</th><th>E-mail</th><th>Telefone</th><th></th></tr>{trs}</table>
    </div>"""
    return page('Leitores', corpo)


@app.route('/leitores/novo', methods=['POST'])
@area('leitores')
def novo_leitor():
    nome = (request.form.get('nome') or '').strip()
    matricula = (request.form.get('matricula') or '').strip()
    if not nome or not matricula:
        flash('Informe o nome e a matrícula/ID para ativar o acesso privado.')
        return redirect(url_for('leitores'))
    if row("SELECT 1 FROM leitores WHERE matricula = ?", (matricula,)):
        flash('Já existe um leitor com esta matrícula/ID.')
        return redirect(url_for('leitores'))
    senha_tmp = gerar_senha_temporaria()
    run("INSERT INTO leitores(nome, matricula, email, telefone, senha, senha_temporaria) VALUES (?, ?, ?, ?, ?, 1)",
        (nome, matricula, request.form.get('email', '').strip(),
         request.form.get('telefone', '').strip(), generate_password_hash(senha_tmp)))
    log(f'Cadastrou leitor: {nome}')
    flash(f'Leitor cadastrado. Senha temporária única de {nome}: {senha_tmp}. Entregue-a ao leitor; não será mostrada novamente.')
    return redirect(url_for('leitores'))


@app.route('/leitores/<int:id>/senha', methods=['POST'])
@area('leitores')
def senha_leitor(id):
    l = row("SELECT nome FROM leitores WHERE id = ?", (id,))
    if l:
        senha_tmp = gerar_senha_temporaria()
        run("UPDATE leitores SET senha = ?, senha_temporaria = 1 WHERE id = ?",
            (generate_password_hash(senha_tmp), id))
        log(f'Redefiniu senha do leitor: {l["nome"]}')
        flash(f'Senha temporária única de {l["nome"]}: {senha_tmp}. Entregue-a ao leitor; não será mostrada novamente.')
    return redirect(url_for('leitores'))
@app.route('/leitores/<int:id>/excluir', methods=['POST'])
@area('leitores')
def excluir_leitor(id):
    if row("SELECT 1 FROM emprestimos WHERE leitor_id = ? UNION ALL SELECT 1 FROM reservas WHERE leitor_id = ? LIMIT 1", (id,id)):
        flash('Não é possível excluir: o leitor tem histórico de empréstimos ou reservas.')
    else:
        l = row("SELECT nome FROM leitores WHERE id = ?", (id,))
        if l:
            run("DELETE FROM leitores WHERE id = ?", (id,))
            log(f'Excluiu leitor: {l["nome"]}')
    return redirect(url_for('leitores'))


# ---------------------------------------------------------------- empréstimos

def get_emp(id):
    return row(SQL_EMP + " WHERE e.id = ?", (id,))


def tem_reserva(livro_id):
    return bool(row("SELECT 1 FROM reservas WHERE livro_id = ? AND status IN ('ativa', 'pronta')", (livro_id,)))


@app.route('/emprestimos')
@area('emprestimos')
def emprestimos():
    op_l = '<option value="">Escolha o leitor</option>' + ''.join(
        f'<option value="{x["id"]}">{esc(x["nome"])}</option>'
        for x in rows("SELECT id, nome FROM leitores ORDER BY nome COLLATE NOCASE"))
    op_b = '<option value="">Escolha o item</option>' + ''.join(
        f'<option value="{b["id"]}">{esc(b["titulo"])} — {esc(b["midia"])} ({b["disp"]} disp.)</option>'
        for b in rows(SQL_LIVROS + " WHERE disp > 0 ORDER BY l.titulo COLLATE NOCASE"))
    op_r = '<option value="">Escolha o item</option>' + ''.join(
        f'<option value="{b["id"]}">{esc(b["titulo"])} — {esc(b["midia"])}</option>'
        for b in rows(SQL_LIVROS + " WHERE disp <= 0 ORDER BY l.titulo COLLATE NOCASE"))

    trs = ''
    for e in rows(SQL_EMP + " ORDER BY e.retirada DESC"):
        s = situacao(e)
        acoes = ''
        if not e['devolucao']:
            if e['renovacoes'] < MAX_RENOV and not tem_reserva(e['livro_id']):
                acoes += post_btn(url_for('renovar', id=e['id']), 'Renovar')
            acoes += post_btn(url_for('devolver', id=e['id']), 'Devolver')
        cls = 'devolvido' if s == 'devolvido' else ('atrasado' if s == 'atrasado' else '')
        extra = f" (renovado {e['renovacoes']}x)" if e['renovacoes'] else ''
        trs += (f'<tr class="{cls}"><td>{esc(e["leitor"])}</td><td>{esc(e["livro"])} ({esc(e["midia"])})</td>'
                f'<td>{fmt(e["retirada"])}</td><td>{fmt(e["prevista"])}</td><td>{fmt(e["devolucao"])}</td>'
                f'<td>{ROTULO[s]}{extra}</td><td>{acoes}</td></tr>')

    res_trs = ''
    for r in rows("SELECT r.*, l.titulo AS livro, l.midia AS midia, p.nome AS leitor FROM reservas r "
                  "LEFT JOIN livros l ON l.id = r.livro_id LEFT JOIN leitores p ON p.id = r.leitor_id "
                  "WHERE r.status IN ('ativa', 'pronta') ORDER BY r.livro_id, r.criada, r.id"):
        if r['status'] == 'pronta':
            pos, sit = '—', 'Pronta para retirada'
            acao = post_btn(url_for('retirar_reserva', id=r['id']), 'Retirar (fazer empréstimo)')
        else:
            ids = [x['id'] for x in fila(r['livro_id'])]
            pos, sit = f'{ids.index(r["id"]) + 1}º', 'Na fila'
            acao = ''
        acao += post_btn(url_for('cancelar_reserva', id=r['id']), 'Cancelar', 'perigo', 'Cancelar esta reserva?')
        res_trs += (f'<tr><td>{esc(r["livro"])} ({esc(r["midia"])})</td><td>{esc(r["leitor"])}</td>'
                    f'<td>{pos}</td><td>{sit}</td><td>{acao}</td></tr>')

    notif_trs = ''.join(
        f'<tr><td>{esc(n["data"])}</td><td>{esc(n["leitor"])}</td><td>{esc(n["texto"])}</td></tr>'
        for n in rows("SELECT n.*, l.nome AS leitor FROM notificacoes n "
                      "LEFT JOIN leitores l ON l.id = n.leitor_id ORDER BY n.id DESC LIMIT 100"))

    dl_disp = ''.join(f'<option value="{esc(b["titulo"])} — {esc(b["midia"])}"></option>'
                      for b in rows(SQL_LIVROS + " WHERE disp > 0 ORDER BY l.titulo COLLATE NOCASE"))
    indis = rows(SQL_LIVROS + " WHERE disp <= 0 ORDER BY l.titulo COLLATE NOCASE")
    dl_r = ''.join(f'<option value="{esc(b["titulo"])} — {esc(b["midia"])}"></option>' for b in indis)
    dl_l = ''.join(f'<option value="{esc(x["nome"])}"></option>'
                   for x in rows("SELECT nome FROM leitores ORDER BY nome COLLATE NOCASE"))
    corpo = f"""
    <div class="card">
      <h3>Novo empréstimo</h3>
      <form method="post" class="form-cadastro" action="{url_for('novo_emprestimo')}">
        <div class="grid">
          <select name="leitor_id">{op_l}</select>
          <select name="livro_id">{op_b}</select>
          <input name="livro_txt" list="dl-disp" placeholder="ou digite o título ou autor" autocomplete="off">
        </div>
        <datalist id="dl-disp">{dl_disp}</datalist>
        <button class="btn">Registrar empréstimo ({PRAZO} dias)</button>
      </form>
    </div>
    <div class="card">
      <h3>Reservar item indisponível</h3>
      <form method="post" class="form-cadastro" action="{url_for('reservar')}">
        <div class="grid">
          <input name="leitor_txt" list="dl-leitores" placeholder="Digite o nome do leitor" autocomplete="off" required>
          <select name="livro_id">{op_r}</select>
          <input name="livro_txt" list="dl-itens" placeholder="ou digite o título ou autor do item" autocomplete="off">
        </div>
        <datalist id="dl-leitores">{dl_l}</datalist>
        <datalist id="dl-itens">{dl_r}</datalist>
        <button class="btn">Reservar</button>
      </form>
    </div>
    <div class="card">
      <h3>Empréstimos</h3>
      <input class="filtro" placeholder="Buscar por leitor ou livro enquanto digita..." style="width:100%">
      <table class="lista">
        <tr><th>Leitor</th><th>Item</th><th>Retirada</th><th>Prevista</th><th>Devolução</th><th>Situação</th><th></th></tr>
        {trs}
      </table>
    </div>
    <div class="card">
      <h3>Reservas</h3>
      <table>
        <tr><th>Item</th><th>Leitor</th><th>Posição</th><th>Situação</th><th></th></tr>
        {res_trs}
      </table>
    </div>
    <div class="card">
      <h3>Notificações enviadas</h3>
      <p>Mensagens geradas pelo sistema (reservas, devoluções e posição na fila). Envio por e-mail depende de configuração; SMS exige um serviço contratado.</p>
      <table>
        <tr><th>Data</th><th>Leitor</th><th>Mensagem</th></tr>
        {notif_trs}
      </table>
    </div>"""
    return page('Empréstimos', corpo)


def resolver_leitor(form):
    if form.get('leitor_id'):
        return form['leitor_id']
    n = norm(form.get('leitor_txt', ''))
    l = next((x for x in rows("SELECT id, nome FROM leitores") if norm(x['nome']) == n), None)
    return l['id'] if l else None


def resolver_livro(form, disponivel):
    if form.get('livro_id'):
        return form['livro_id']
    t = norm(form.get('livro_txt', ''))
    filtro = " WHERE disp > 0" if disponivel else " WHERE disp <= 0"
    b = next((x for x in rows(SQL_LIVROS + filtro) if norm(f"{x['titulo']} — {x['midia']}") == t), None)
    return b['id'] if b else None


@app.route('/emprestimos/novo', methods=['POST'])
@area('emprestimos')
def novo_emprestimo():
    leitor_id = resolver_leitor(request.form)
    livro_id = resolver_livro(request.form, disponivel=True)
    if not leitor_id or not livro_id:
        flash('Escolha o leitor e o item.')
        return redirect(url_for('emprestimos'))
    try:
        with transacao() as c:
            disponiveis_agora = disponibilidade_db(c, int(livro_id))
            if not disponiveis_agora or disponiveis_agora[0] < 1:
                flash('Não há exemplares disponíveis deste item. Use a opção Reservar.')
                return redirect(url_for('emprestimos'))
            hj = date.today()
            c.execute("INSERT INTO emprestimos(livro_id,leitor_id,retirada,prevista,devolucao,renovacoes) VALUES (?,?,?,?,NULL,0)",
                      (int(livro_id), int(leitor_id), hj.isoformat(), (hj+timedelta(days=PRAZO)).isoformat()))
            item = c.execute('SELECT titulo FROM livros WHERE id=?', (livro_id,)).fetchone()
            pessoa = c.execute('SELECT nome FROM leitores WHERE id=?', (leitor_id,)).fetchone()
            log(f'Emprestou "{item["titulo"]}" para {pessoa["nome"]}', c)
    except sqlite3.IntegrityError:
        flash('O item ou leitor já não está disponível. Atualize a página e tente novamente.')
        return redirect(url_for('emprestimos'))
    flash('Empréstimo registrado.')
    return redirect(url_for('emprestimos'))

@app.route('/reservas/novo', methods=['POST'])
@area('emprestimos')
def reservar():
    leitor_id = resolver_leitor(request.form)
    livro_id = resolver_livro(request.form, disponivel=False)
    if not leitor_id or not livro_id:
        flash('Escolha o leitor e o item na lista de sugestões.')
        return redirect(url_for('emprestimos'))
    try:
        with transacao() as c:
            livro = c.execute('SELECT titulo FROM livros WHERE id=?', (livro_id,)).fetchone()
            if not livro:
                flash('Item não encontrado.')
                return redirect(url_for('emprestimos'))
            disp = disponibilidade_db(c, int(livro_id))[0]
            if disp > 0:
                flash('O item está disponível. Registre o empréstimo em vez de reservar.')
                return redirect(url_for('emprestimos'))
            if c.execute('SELECT 1 FROM emprestimos WHERE livro_id=? AND leitor_id=? AND devolucao IS NULL', (livro_id,leitor_id)).fetchone():
                flash('Este leitor já está com este item emprestado.')
                return redirect(url_for('emprestimos'))
            if c.execute("SELECT 1 FROM reservas WHERE livro_id=? AND leitor_id=? AND status IN ('ativa','pronta')", (livro_id,leitor_id)).fetchone():
                flash('Este leitor já tem uma reserva deste item.')
                return redirect(url_for('emprestimos'))
            c.execute("INSERT INTO reservas(livro_id,leitor_id,criada,status) VALUES (?,?,?,'ativa')",
                      (int(livro_id),int(leitor_id),datetime.now().isoformat()))
            fila_ids = [r[0] for r in c.execute("SELECT leitor_id FROM reservas WHERE livro_id=? AND status='ativa' ORDER BY criada,id", (livro_id,))]
            pos = fila_ids.index(int(leitor_id))+1
            texto = f'Reserva registrada para "{livro["titulo"]}". Você está em {pos}º lugar na fila.'
            inserir_notificacao_db(c, int(leitor_id), texto)
            pessoa = c.execute('SELECT nome FROM leitores WHERE id=?', (leitor_id,)).fetchone()
            log(f'Reservou "{livro["titulo"]}" para {pessoa["nome"]} ({pos}º na fila)', c)
    except sqlite3.IntegrityError:
        flash('A reserva não pôde ser criada. Atualize a página e tente novamente.')
        return redirect(url_for('emprestimos'))
    enviar_email(int(leitor_id), texto)
    avisar_fila(int(livro_id))
    flash(f'Reserva registrada: {pos}º na fila.')
    return redirect(url_for('emprestimos'))

@app.route('/reservas/<int:id>/retirar', methods=['POST'])
@area('emprestimos')
def retirar_reserva(id):
    try:
        with transacao() as c:
            r = c.execute("SELECT * FROM reservas WHERE id=? AND status='pronta'", (id,)).fetchone()
            if not r:
                flash('Esta reserva já foi atendida ou deixou de estar pronta.')
                return redirect(url_for('emprestimos'))
            if disponibilidade_db(c,r['livro_id'])[0] < 0:
                flash('A reserva excede os exemplares cadastrados; ajuste o estoque antes da retirada.')
                return redirect(url_for('emprestimos'))
            c.execute("UPDATE reservas SET status='atendida' WHERE id=? AND status='pronta'", (id,))
            hj = date.today()
            c.execute("INSERT INTO emprestimos(livro_id,leitor_id,retirada,prevista,devolucao,renovacoes) VALUES (?,?,?,?,NULL,0)",
                      (r['livro_id'],r['leitor_id'],hj.isoformat(),(hj+timedelta(days=PRAZO)).isoformat()))
            log('Empréstimo registrado a partir de reserva', c)
    except sqlite3.IntegrityError:
        flash('Não foi possível retirar a reserva; confirme a disponibilidade do exemplar.')
        return redirect(url_for('emprestimos'))
    flash('Empréstimo registrado a partir da reserva.')
    return redirect(url_for('emprestimos'))

@app.route('/reservas/<int:id>/cancelar', methods=['POST'])
@area('emprestimos')
def cancelar_reserva(id):
    avisos=[]
    with transacao() as c:
        r = c.execute("SELECT * FROM reservas WHERE id=? AND status IN ('ativa','pronta')", (id,)).fetchone()
        if r:
            c.execute("UPDATE reservas SET status='cancelada' WHERE id=?", (id,))
            log('Cancelou uma reserva', c)
            avisos = promover_db(c, r['livro_id'])
    enviar_avisos(avisos)
    if r:
        avisar_fila(r['livro_id'])
        flash('Reserva cancelada.')
    return redirect(url_for('emprestimos'))

@app.route('/emprestimos/<int:id>/devolver', methods=['POST'])
@area('emprestimos')
def devolver(id):
    avisos=[]
    with transacao() as c:
        e = c.execute(SQL_EMP + ' WHERE e.id=? AND e.devolucao IS NULL', (id,)).fetchone()
        if e:
            atualizada = c.execute('UPDATE emprestimos SET devolucao=? WHERE id=? AND devolucao IS NULL', (hoje(),id)).rowcount
            if atualizada:
                log(f'Devolveu "{e["livro"]}" de {e["leitor"]}', c)
                avisos = promover_db(c, e['livro_id'])
    enviar_avisos(avisos)
    if e:
        avisar_fila(e['livro_id'])
        flash('Devolução registrada.')
    return redirect(url_for('emprestimos'))

@app.route('/emprestimos/<int:id>/renovar', methods=['POST'])
@area('emprestimos')
def renovar(id):
    with transacao() as c:
        e = c.execute(SQL_EMP + ' WHERE e.id=? AND e.devolucao IS NULL', (id,)).fetchone()
        if not e:
            flash('Empréstimo ativo não encontrado.')
        elif c.execute("SELECT 1 FROM reservas WHERE livro_id=? AND status IN ('ativa','pronta')", (e['livro_id'],)).fetchone():
            flash('Não é possível renovar: este item tem reserva.')
        elif e['renovacoes'] >= MAX_RENOV:
            flash('Limite de renovações atingido.')
        else:
            base=max(date.fromisoformat(e['prevista']),date.today())
            nova=(base+timedelta(days=PRAZO)).isoformat()
            c.execute('UPDATE emprestimos SET prevista=?,renovacoes=renovacoes+1 WHERE id=? AND devolucao IS NULL', (nova,id))
            log(f'Renovou "{e["livro"]}" de {e["leitor"]}', c)
            flash('Empréstimo renovado.')
    return redirect(url_for('emprestimos'))

@app.route('/emprestimos/<int:id>/excluir', methods=['POST'])
@area('emprestimos')
def excluir_emprestimo(id):
    e = get_emp(id)
    if e:
        run("DELETE FROM emprestimos WHERE id = ?", (id,))
        log(f'Excluiu empréstimo de "{e["livro"]}" para {e["leitor"]}')
    return redirect(url_for('emprestimos'))



def fmt(iso):
    return iso.split('-')[2] + '/' + iso.split('-')[1] + '/' + iso.split('-')[0] if iso else '—'


# ---------------------------------------------------------------- relatórios

TIPOS_RELATORIO = {
    'emprestimos': 'Empréstimos feitos',
    'devolucoes': 'Devoluções',
    'reservas': 'Reservas',
    'atrasados': 'Em atraso (agora)',
    'ativos': 'Emprestados agora',
}
SITUACAO_RESERVA = {'ativa': 'Na fila', 'pronta': 'Pronta para retirada',
                    'atendida': 'Atendida', 'cancelada': 'Cancelada'}


def dados_relatorio(tipo, de, ate, lei, tit):
    """Devolve (cabeçalho, linhas). Períodos valem para empréstimos, devoluções e reservas."""
    hj = hoje()
    def do_leitor(leitor_id):
        return not lei or str(leitor_id) != lei
    def do_titulo(t):
        return not tit or tit in norm(t)
    out = []
    if tipo == 'reservas':
        cab = ['Leitor', 'Item', 'Data do pedido', 'Situação']
        for r in rows("SELECT r.*, l.titulo AS livro, l.midia AS midia, p.nome AS leitor FROM reservas r "
                      "LEFT JOIN livros l ON l.id = r.livro_id LEFT JOIN leitores p ON p.id = r.leitor_id "
                      "ORDER BY r.criada DESC"):
            d = (r['criada'] or '')[:10]
            if not (de <= d <= ate) or do_leitor(r['leitor_id']) is False or not do_titulo(r['livro'] or ''):
                continue
            out.append([r['leitor'], f"{r['livro']} ({r['midia']})", fmt(d),
                        SITUACAO_RESERVA.get(r['status'], r['status'])])
        return cab, out
    if tipo == 'devolucoes':
        cab = ['Leitor', 'Item', 'Retirada', 'Devolução', 'Dias de atraso']
        for e in rows(SQL_EMP + " ORDER BY e.devolucao DESC"):
            if not e['devolucao'] or not (de <= e['devolucao'] <= ate):
                continue
            if not do_leitor(e['leitor_id']) or not do_titulo(e['livro'] or ''):
                continue
            atraso = max(0, (date.fromisoformat(e['devolucao']) - date.fromisoformat(e['prevista'])).days)
            out.append([e['leitor'], f"{e['livro']} ({e['midia']})", fmt(e['retirada']),
                        fmt(e['devolucao']), atraso])
        return cab, out
    if tipo == 'atrasados':
        cab = ['Leitor', 'Item', 'Prevista', 'Dias de atraso']
        for e in rows(SQL_EMP + " ORDER BY e.prevista"):
            if e['devolucao'] or e['prevista'] >= hj:
                continue
            if not do_leitor(e['leitor_id']) or not do_titulo(e['livro'] or ''):
                continue
            dias = (date.today() - date.fromisoformat(e['prevista'])).days
            out.append([e['leitor'], f"{e['livro']} ({e['midia']})", fmt(e['prevista']), dias])
        return cab, out
    if tipo == 'ativos':
        cab = ['Leitor', 'Item', 'Retirada', 'Prevista']
        for e in rows(SQL_EMP + " ORDER BY e.prevista"):
            if e['devolucao']:
                continue
            if not do_leitor(e['leitor_id']) or not do_titulo(e['livro'] or ''):
                continue
            out.append([e['leitor'], f"{e['livro']} ({e['midia']})", fmt(e['retirada']), fmt(e['prevista'])])
        return cab, out
    # empréstimos feitos no período
    cab = ['Leitor', 'Item', 'Retirada', 'Prevista', 'Devolução', 'Situação']
    for e in rows(SQL_EMP + " ORDER BY e.retirada DESC"):
        if not (de <= e['retirada'] <= ate):
            continue
        if not do_leitor(e['leitor_id']) or not do_titulo(e['livro'] or ''):
            continue
        out.append([e['leitor'], f"{e['livro']} ({e['midia']})", fmt(e['retirada']), fmt(e['prevista']),
                    fmt(e['devolucao']), ROTULO[situacao(e)]])
    for r in rows("SELECT r.*, l.titulo AS livro, l.midia AS midia, p.nome AS leitor FROM reservas r "
                  "LEFT JOIN livros l ON l.id = r.livro_id LEFT JOIN leitores p ON p.id = r.leitor_id "
                  "WHERE r.status IN ('ativa', 'pronta') ORDER BY r.criada DESC"):
        d = (r['criada'] or '')[:10]
        if not (de <= d <= ate) or not do_leitor(r['leitor_id']) or not do_titulo(r['livro'] or ''):
            continue
        out.append([r['leitor'], f"{r['livro']} ({r['midia']})", fmt(d), '—', '—',
                    'Reservado: ' + SITUACAO_RESERVA.get(r['status'], r['status'])])
    return cab, out


@app.route('/relatorios')
@area('relatorios')
def relatorios():
    a = request.args
    tipo = a.get('tipo', 'emprestimos')
    if tipo not in TIPOS_RELATORIO:
        tipo = 'emprestimos'
    try:
        ate_d = date.fromisoformat(a.get('ate') or hoje())
    except ValueError:
        ate_d = date.today()
    try:
        de_d = date.fromisoformat(a.get('de') or '')
    except ValueError:
        de_d = ate_d - timedelta(days=LIMITE_DIAS - 1)
    aviso = ''
    if de_d > ate_d:
        de_d = ate_d
    if (ate_d - de_d).days > LIMITE_DIAS - 1:
        de_d = ate_d - timedelta(days=LIMITE_DIAS - 1)
        aviso = f' O período foi limitado a {LIMITE_DIAS} dias.'
    de, ate = de_d.isoformat(), ate_d.isoformat()
    lei, tit = a.get('leitor', ''), norm(a.get('titulo', ''))
    cab, linhas = dados_relatorio(tipo, de, ate, lei, tit)
    nota = ' O período não se aplica a este relatório (mostra a situação atual).' if tipo in ('atrasados', 'ativos') else ''
    opt_tipo = ''.join(f'<option value="{k}" {"selected" if k == tipo else ""}>{v}</option>'
                       for k, v in TIPOS_RELATORIO.items())
    opt_l = '<option value="">Todos os leitores</option>' + ''.join(
        f'<option value="{x["id"]}" {"selected" if str(x["id"]) == lei else ""}>{esc(x["nome"])}</option>'
        for x in rows("SELECT id, nome FROM leitores ORDER BY nome COLLATE NOCASE"))
    ths = ''.join(f'<th>{esc(str(c))}</th>' for c in cab)
    trs = ''.join('<tr>' + ''.join(f'<td>{esc(str(x))}</td>' for x in l) + '</tr>' for l in linhas)
    corpo = f"""
    <div class="card naoimprime">
      <h3>Relatórios</h3>
      <form method="get">
        <div class="grid">
          <select name="tipo" onchange="this.form.submit()">{opt_tipo}</select>
          <label>De <input type="date" name="de" value="{de}"></label>
          <label>Até <input type="date" name="ate" value="{ate}"></label>
          <select name="leitor">{opt_l}</select>
          <input name="titulo" value="{esc(a.get('titulo', ''))}" placeholder="Título contém...">
        </div>
        <button class="btn">Filtrar</button>
        <button type="button" class="btn" onclick="window.print()">Imprimir</button>
      </form>
      <p class="fr-nota">Período máximo de {LIMITE_DIAS} dias.{aviso}{nota}</p>
    </div>
    <div class="card">
      <h3>{TIPOS_RELATORIO[tipo]}: {len(linhas)} registro(s)</h3>
      <table class="lista"><tr>{ths}</tr>{trs}</table>
    </div>"""
    return page('Relatórios', corpo)


# ---------------------------------------------------------------- eventos

@app.route('/eventos')
@area('eventos')
def eventos():
    q_raw = request.args.get('q', '')
    q = norm(q_raw)
    corte = (datetime.now() - timedelta(days=LIMITE_DIAS)).strftime('%Y-%m-%d %H:%M:%S')
    lista = [e for e in rows("SELECT * FROM eventos WHERE data >= ? ORDER BY id DESC LIMIT 1000", (corte,))
             if q in norm(f"{e['usuario']} {e['acao']}")][:500]
    trs = ''.join(f'<tr><td>{esc(e["data"])}</td><td>{esc(e["usuario"])}</td><td>{esc(e["acao"])}</td></tr>'
                  for e in lista)
    corpo = f"""
    <div class="card">
      <h3>Eventos</h3>
      <p class="fr-nota">Mostrando os últimos {LIMITE_DIAS} dias.</p>
      <input class="filtro" placeholder="Buscar por usuário ou ação enquanto digita..." style="width:100%">
      <table class="lista"><tr><th>Data</th><th>Usuário</th><th>Ação</th></tr>{trs}</table>
    </div>"""
    return page('Eventos', corpo)


# ---------------------------------------------------------------- usuários, permissões e backup

@app.route('/usuarios')
@area('usuarios')
def usuarios():
    cfg_email = carregar_email_cfg()
    checks = ''.join(f"<label><input type='checkbox' name='perms' value='{k}'> {v}</label>"
                     for k, v in AREAS.items())
    trs = ''
    for u in rows("SELECT * FROM usuarios ORDER BY nome COLLATE NOCASE"):
        perms = [p for p in (u['perms'] or '').split(',') if p]
        resumo = 'Administrador' if u['admin'] else (', '.join(AREAS[p] for p in perms if p in AREAS) or 'Nenhuma')
        excluir = ''
        if u['id'] != 1 and u['id'] != session.get('uid'):
            excluir = post_btn(url_for('excluir_usuario', id=u['id']), 'Excluir', 'perigo', 'Excluir este usuário?')
        trs += (f'<tr><td>{esc(u["nome"])}</td><td>{resumo}</td><td>'
                f'<form method="post" action="{url_for("senha_usuario", id=u["id"])}" style="display:inline">'
                f'<input name="senha" type="password" placeholder="Nova senha"> <button class="btn">Trocar</button></form> '
                f'{excluir}</td></tr>')
    corpo = f"""
    <div class="aviso">Senhas temporárias são substituídas no primeiro acesso. Use pelo menos 8 caracteres.</div>
    <div class="card">
      <h3>Novo colaborador</h3>
      <form method="post" class="form-cadastro" action="{url_for('novo_usuario')}">
        <div class="grid">
          <input name="nome" placeholder="Nome *" required>
          <input name="senha" type="password" placeholder="Senha (mínimo 8 caracteres)">
        </div>
        <p>Permissões:</p>
        <div class="grid">{checks}<label><input type="checkbox" name="admin" value="1"> <strong>Administrador (tudo)</strong></label></div>
        <button class="btn">Criar colaborador</button>
      </form>
    </div>
    <div class="card">
      <h3>Colaboradores</h3>
      <input class="filtro" placeholder="Buscar usuário enquanto digita..." style="width:100%">
      <table class="lista"><tr><th>Colaborador</th><th>Permissões</th><th></th></tr>{trs}</table>
    </div>
    <div class="card">
      <h3>E-mail das notificações</h3>
      <p>Use uma conta de teste dedicada. Servidor Gmail: smtp.gmail.com, porta 587.
         A senha SMTP vem da variável <code>BIBLIO_SMTP_PASS</code> do Windows e não é salva neste arquivo.</p>
      <form method="post" action="{url_for('email_config')}">
        <div class="grid">
          <input name="host" placeholder="Servidor SMTP" value="{esc(cfg_email['host'])}">
          <input name="porta" placeholder="Porta" value="{esc(str(cfg_email['porta']))}">
          <input name="usuario" placeholder="Seu e-mail (login)" value="{esc(cfg_email['usuario'])}">
          <input name="remetente" placeholder="Remetente (opcional)" value="{esc(cfg_email['remetente'])}">
        </div>
        <button class="btn">Salvar configuração</button>
      </form>
      <form method="post" action="{url_for('email_teste')}" style="margin-top:10px">
        <input name="destino" placeholder="E-mail para teste">
        <button class="btn">Enviar e-mail de teste</button>
      </form>
    </div>
    <div class="card">
      <h3>Dados de exemplo</h3>
      <p>Cria livros e leitores fictícios para testar o sistema; mantém apenas Administrador e Ana Souza na equipa. Não apaga o acervo.</p>
      <form method="post" action="{url_for('carregar_exemplos')}">
        <button class="btn">Carregar dados de exemplo</button>
      </form>
    </div>
    <div class="card">
      <h3>Backup e restauração</h3>
      <p>Os dados ficam no arquivo <code>biblioteca.db</code> ao lado deste programa. O backup em JSON é uma cópia extra.</p>
      <a class="btn" href="{url_for('exportar_completo')}">Descarregar backup completo (dados + capas)</a>
      <a class="btn" href="{url_for('exportar')}">Exportar dados (JSON)</a>
      <form method="post" action="{url_for('importar_json')}" enctype="multipart/form-data" style="display:inline"
            onsubmit="return confirm('Isto substitui TODOS os dados e capas atuais. Continuar?')">
        <input type="file" name="arquivo" accept=".zip,.json"> <button class="btn perigo">Restaurar backup</button>
      </form>
    </div>"""
    return page('Colaboradores', corpo)


@app.route('/usuarios/novo', methods=['POST'])
@area('usuarios')
def novo_usuario():
    nome = (request.form.get('nome') or '').strip()
    senha = request.form.get('senha', '')
    if not nome:
        flash('Informe o nome.')
        return redirect(url_for('usuarios'))
    if len(senha) < 8:
        flash('A senha do colaborador deve ter pelo menos 8 caracteres.')
        return redirect(url_for('usuarios'))
    perms = [p for p in request.form.getlist('perms') if p in AREAS]
    admin = 1 if request.form.get('admin') else 0
    try:
        run("INSERT INTO usuarios(nome, senha, admin, perms, senha_temporaria) VALUES (?, ?, ?, ?, 0)",
            (nome, generate_password_hash(senha), admin, ','.join(perms)))
    except sqlite3.IntegrityError:
        flash('Já existe um usuário com este nome.')
        return redirect(url_for('usuarios'))
    log(f'Criou usuário: {nome}')
    flash('Usuário criado.')
    return redirect(url_for('usuarios'))


@app.route('/usuarios/<int:id>/senha', methods=['POST'])
@area('usuarios')
def senha_usuario(id):
    nova = request.form.get('senha', '')
    u = row("SELECT nome FROM usuarios WHERE id = ?", (id,))
    if u and len(nova) < 8:
        flash('A senha deve ter pelo menos 8 caracteres.')
        return redirect(url_for('usuarios'))
    if u:
        run("UPDATE usuarios SET senha = ?, senha_temporaria=0 WHERE id = ?", (generate_password_hash(nova), id))
        log(f'Alterou senha de: {u["nome"]}')
        flash('Senha alterada.')
    return redirect(url_for('usuarios'))


@app.route('/usuarios/<int:id>/excluir', methods=['POST'])
@area('usuarios')
def excluir_usuario(id):
    if id != 1 and id != session.get('uid'):
        u = row("SELECT nome FROM usuarios WHERE id = ?", (id,))
        if u:
            run("DELETE FROM usuarios WHERE id = ?", (id,))
            log(f'Excluiu usuário: {u["nome"]}')
    return redirect(url_for('usuarios'))


@app.route('/exportar')
@area('usuarios')
def exportar():
    dados = {t: [dict(r) for r in rows(f"SELECT * FROM {t}")] for t in TABELAS}
    return Response(json.dumps(dados, ensure_ascii=False, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename=biblioteca-{hoje()}.json'})


@app.route('/exportar-completo')
@area('usuarios')
def exportar_completo():
    pasta = os.path.join(DATA_BASE, '_backups')
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, f'Biblioteca-completa-{datetime.now():%Y%m%d-%H%M%S}.zip')
    with tempfile.TemporaryDirectory() as tmp:
        db_copia = os.path.join(tmp, 'biblioteca.db')
        origem = conectar()
        destino = sqlite3.connect(db_copia)
        try:
            origem.backup(destino)
        finally:
            destino.close(); origem.close()
        cfg_email = carregar_email_cfg()
        cfg_segura = {k: cfg_email.get(k, '') for k in ('host','porta','usuario','remetente')}
        cfg_tmp = os.path.join(tmp, 'email.json')
        with open(cfg_tmp,'w',encoding='utf-8') as f:
            json.dump(cfg_segura,f,ensure_ascii=False,indent=2)
        with zipfile.ZipFile(caminho, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(db_copia, 'biblioteca.db')
            z.write(CDD_BASE_ARQUIVO, 'cdd_base.txt')
            z.write(cfg_tmp, 'email.json')
            for pasta_imagens in (CAPAS, CDD_THUMBS):
                if os.path.isdir(pasta_imagens):
                    for base, _, nomes in os.walk(pasta_imagens):
                        for nome in nomes:
                            ficheiro = os.path.join(base, nome)
                            raiz_dados = DATA_BASE
                            z.write(ficheiro, os.path.relpath(ficheiro, raiz_dados))
    return send_file(caminho, as_attachment=True, download_name=os.path.basename(caminho),
                     mimetype='application/zip', max_age=0)


def validar_backup_json(dados):
    if not isinstance(dados, dict) or set(dados) != set(TABELAS):
        raise ValueError('o JSON deve conter todas as tabelas do backup completo')
    obrigatorios = {
        'usuarios': {'id','nome','senha','admin','perms'},
        'livros': {'id','titulo','autor','midia','exemplares'},
        'leitores': {'id','nome'},
        'emprestimos': {'id','livro_id','leitor_id'},
        'eventos': {'id'}, 'reservas': {'id','livro_id','leitor_id','status'},
        'notificacoes': {'id','leitor_id','texto'},
    }
    if not any(isinstance(u,dict) and int(u.get('admin') or 0)==1 for u in dados['usuarios']):
        raise ValueError('o backup não contém uma conta de Administrador')
    for tabela, registros in dados.items():
        if not isinstance(registros, list):
            raise ValueError(f'a tabela {tabela} não é uma lista')
        if any(not isinstance(reg, dict) or not obrigatorios[tabela].issubset(reg) for reg in registros):
            raise ValueError(f'a tabela {tabela} tem registos incompletos')


def restaurar_db_de_ficheiro(c, caminho_db):
    fonte = sqlite3.connect(caminho_db)
    try:
        if fonte.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('o banco dentro do ZIP está danificado')
        tabelas = {r[0] for r in fonte.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABELAS).issubset(tabelas):
            raise ValueError('o banco dentro do ZIP não contém todas as tabelas')
        if not fonte.execute('SELECT 1 FROM usuarios WHERE admin=1 LIMIT 1').fetchone():
            raise ValueError('o backup não contém uma conta de Administrador')
        for tabela in TABELAS:
            campos = {r[1] for r in fonte.execute(f'PRAGMA table_info({tabela})')}
            essenciais = {'usuarios':{'id','nome','senha','admin','perms'},'livros':{'id','titulo'},
                'leitores':{'id','nome'},'emprestimos':{'id','livro_id','leitor_id'},'eventos':{'id'},
                'reservas':{'id','livro_id','leitor_id'},'notificacoes':{'id','leitor_id'}}[tabela]
            if not essenciais.issubset(campos):
                raise ValueError(f'a tabela {tabela} do backup está incompleta')
        c.execute('ATTACH DATABASE ? AS backup_origem', (caminho_db,))
        ordem_apagar = ('notificacoes','reservas','emprestimos','eventos','livros','leitores','usuarios')
        ordem_inserir = ('usuarios','livros','leitores','emprestimos','eventos','reservas','notificacoes')
        c.execute('BEGIN IMMEDIATE')
        for tabela in ordem_apagar:
            c.execute(f'DELETE FROM {tabela}')
        for tabela in ordem_inserir:
            destino = {r[1] for r in c.execute(f'PRAGMA table_info({tabela})')}
            origem = {r[1] for r in c.execute(f'PRAGMA backup_origem.table_info({tabela})')}
            comuns = [nome for nome in TABELAS[tabela] if nome in destino and nome in origem]
            lista = ','.join(comuns)
            c.execute(f'INSERT INTO {tabela}({lista}) SELECT {lista} FROM backup_origem.{tabela}')
        violacoes = c.execute('PRAGMA foreign_key_check').fetchall()
        if violacoes:
            raise ValueError('o backup contém referências inválidas entre tabelas')
        for tabela in ('usuarios','leitores'):
            for reg in c.execute(f'SELECT id,senha FROM {tabela} WHERE senha_temporaria=0').fetchall():
                try:
                    if reg['senha'] and check_password_hash(reg['senha'],'1234'):
                        c.execute(f'UPDATE {tabela} SET senha_temporaria=1 WHERE id=?',(reg['id'],))
                except (ValueError,TypeError):
                    pass
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        try: c.execute('DETACH DATABASE backup_origem')
        except sqlite3.Error: pass
        fonte.close()


@app.route('/importar-json', methods=['POST'])
@area('usuarios')
def importar_json():
    arq = request.files.get('arquivo')
    if not arq or not arq.filename:
        flash('Escolha um backup .zip completo ou um JSON de dados.')
        return redirect(url_for('usuarios'))
    nome = arq.filename.lower()
    try:
        if nome.endswith('.zip'):
            with tempfile.TemporaryDirectory(dir=DATA_BASE) as tmp:
                with zipfile.ZipFile(arq.stream) as z:
                    infos = z.infolist()
                    if sum(i.file_size for i in infos) > 2 * 1024 * 1024 * 1024:
                        raise ValueError('o conteúdo do backup excede 2 GB')
                    permitidos = {'biblioteca.db'}
                    for info in infos:
                        alvo = os.path.normpath(info.filename).replace('\\','/')
                        if alvo.startswith('../') or alvo.startswith('/') or (alvo not in ('biblioteca.db','cdd_base.txt','email.json') and not alvo.startswith(('capas/','cdd_thumbs/'))):
                            raise ValueError('o ZIP contém caminhos não permitidos')
                    dbinfo = next((i for i in infos if i.filename.replace('\\','/') == 'biblioteca.db'), None)
                    if not dbinfo:
                        raise ValueError('não encontrei biblioteca.db dentro do ZIP')
                    db_tmp = os.path.join(tmp, 'biblioteca.db')
                    with z.open(dbinfo) as src, open(db_tmp,'wb') as dst:
                        shutil.copyfileobj(src,dst,1024*1024)
                    ficheiros_novos = {}
                    for nome_local in ('cdd_base.txt','email.json'):
                        info_local = next((i for i in infos if i.filename.replace('\\','/') == nome_local),None)
                        if info_local:
                            destino_local=os.path.join(tmp,nome_local)
                            with z.open(info_local) as src, open(destino_local,'wb') as dst: shutil.copyfileobj(src,dst,1024*1024)
                            if nome_local=='email.json':
                                cfg=json.loads(Path(destino_local).read_text(encoding='utf-8'))
                                cfg={k:cfg.get(k,'') for k in ('host','porta','usuario','remetente')}
                                Path(destino_local).write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
                            ficheiros_novos[os.path.join(DATA_BASE,nome_local)]=destino_local
                    dirs_novos = {CAPAS:os.path.join(tmp,'capas'), CDD_THUMBS:os.path.join(tmp,'cdd_thumbs')}
                    for pasta in dirs_novos.values(): os.makedirs(pasta,exist_ok=True)
                    for info in infos:
                        rel = info.filename.replace('\\','/')
                        if rel.startswith(('capas/','cdd_thumbs/')) and not info.is_dir():
                            destino=os.path.join(tmp,*rel.split('/'))
                            os.makedirs(os.path.dirname(destino),exist_ok=True)
                            with z.open(info) as src, open(destino,'wb') as dst: shutil.copyfileobj(src,dst,1024*1024)
                # Mantém cópia recuperável das pastas atuais até o banco ser validado.
                pastas_antigas = {}; ficheiros_antigos = {}
                try:
                    for atual, nova in dirs_novos.items():
                        antiga = os.path.join(tmp, 'old_' + os.path.basename(atual))
                        if os.path.isdir(atual): os.replace(atual,antiga); pastas_antigas[atual]=antiga
                        os.replace(nova,atual)
                    for atual,nova in ficheiros_novos.items():
                        antiga=os.path.join(tmp,'old_'+os.path.basename(atual))
                        if os.path.isfile(atual): os.replace(atual,antiga); ficheiros_antigos[atual]=antiga
                        os.replace(nova,atual)
                    c = conectar()
                    try:
                        restaurar_db_de_ficheiro(c,db_tmp)
                    finally:
                        c.close()
                except Exception:
                    for atual, antiga in ficheiros_antigos.items():
                        if os.path.isfile(atual): os.remove(atual)
                        if os.path.isfile(antiga): os.replace(antiga,atual)
                    for atual, antiga in pastas_antigas.items():
                        if os.path.isdir(atual): shutil.rmtree(atual)
                        if os.path.isdir(antiga): os.replace(antiga,atual)
                    raise
        elif nome.endswith('.json'):
            max_json = 256 * 1024 * 1024
            bruto = arq.stream.read(max_json + 1)
            if len(bruto) > max_json: raise ValueError('JSON acima de 256 MiB; use o ZIP completo')
            dados = json.loads(bruto.decode('utf-8-sig'))
            validar_backup_json(dados)
            with transacao() as c:
                apagar=('notificacoes','reservas','emprestimos','eventos','livros','leitores','usuarios')
                for tabela in apagar: c.execute(f'DELETE FROM {tabela}')
                inserir=('usuarios','livros','leitores','emprestimos','eventos','reservas','notificacoes')
                for tabela in inserir:
                    for reg in dados[tabela]:
                        usados=[k for k in TABELAS[tabela] if k in reg]
                        c.execute(f"INSERT INTO {tabela}({','.join(usados)}) VALUES ({','.join('?'*len(usados))})",[reg[k] for k in usados])
                for tabela in ('usuarios','leitores'):
                    for reg in c.execute(f'SELECT id,senha FROM {tabela} WHERE senha_temporaria=0').fetchall():
                        try:
                            if reg['senha'] and check_password_hash(reg['senha'],'1234'):
                                c.execute(f'UPDATE {tabela} SET senha_temporaria=1 WHERE id=?',(reg['id'],))
                        except (ValueError,TypeError):
                            pass
        else:
            raise ValueError('formato não suportado; escolha um ZIP completo ou JSON')
    except Exception as ex:
        flash(f'Backup não restaurado; os dados atuais foram preservados: {ex}')
        return redirect(url_for('usuarios'))
    _CDD_BASE_CACHE.update(mtime=None, dados=[])
    session.clear()
    flash('Backup restaurado. Entre novamente.')
    return redirect(url_for('entrar'))


@app.route('/email/config', methods=['POST'])
@area('usuarios')
def email_config():
    dados = {}
    if os.path.exists(EMAIL_CFG):
        with open(EMAIL_CFG, encoding='utf-8') as f:
            dados = json.load(f)
    for k in ['host', 'porta', 'usuario', 'remetente']:
        dados[k] = (request.form.get(k) or '').strip()
    dados.pop('senha', None)
    with open(EMAIL_CFG, 'w', encoding='utf-8') as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    flash('Configuração de e-mail salva.')
    return redirect(url_for('usuarios'))


@app.route('/email/teste', methods=['POST'])
@area('usuarios')
def email_teste():
    destino = request.form.get('destino', '').strip() or carregar_email_cfg()['usuario']
    erro = enviar_direto(destino, 'Teste - Biblioteca Pessoal',
                         'Se você recebeu esta mensagem, o envio de e-mail está funcionando.')
    flash(erro or 'E-mail de teste enviado. Confira a caixa de entrada e o spam.')
    return redirect(url_for('usuarios'))


EXEMPLO_LIVROS = [
    ('Dom Casmurro', 'Machado de Assis', '869.3', 'Livro', 2),
    ('Memórias Póstumas de Brás Cubas', 'Machado de Assis', '869.3', 'Livro', 1),
    ('Metamorfose', 'Franz Kafka', '833', 'Livro', 1),
    ('Viagem ao Centro da Terra', 'Júlio Verne', '843', 'Livro', 1),
    ('Viagem ao Centro da Terra', 'Júlio Verne', '843', 'Audiobook', 1),
    ('O Pequeno Príncipe', 'Antoine de Saint-Exupéry', '843', 'Livro', 3),
    ('Sapiens', 'Yuval Noah Harari', '909', 'Livro', 1),
    ('Cem Anos de Solidão', 'Gabriel García Márquez', '863', 'Livro', 1),
    ('Inteligência Emocional', 'Daniel Goleman', '152.4', 'Livro', 1),
    ('Código Limpo', 'Robert C. Martin', '005.1', 'Livro', 1),
]
EXEMPLO_USUARIOS = [
    ('Ana Souza', ['acervo', 'leitores', 'emprestimos', 'relatorios', 'eventos']),
]
EXEMPLO_LEITORES = ['Marcos Alves', 'Patrícia Lopes', 'Rafael Gomes', 'Sofia Ribeiro', 'Lucas Teixeira',
                    'Helena Barros', 'André Moura', 'Beatriz Cardoso', 'Thiago Nogueira', 'Camila Freitas']


@app.route('/exemplos', methods=['POST'])
@area('usuarios')
def carregar_exemplos():
    n_l = n_u = n_r = 0
    for titulo, autor, cdd, midia, ex in EXEMPLO_LIVROS:
        if not row("SELECT 1 FROM livros WHERE titulo = ? AND midia = ?", (titulo, midia)):
            inserir_livro({'titulo': titulo, 'subtitulo': '', 'autor': autor, 'coautor': '', 'editora': '',
                           'local_publicacao': '', 'ano': '', 'edicao': '', 'volume': '', 'cdd': cdd,
                           'midia': midia, 'exemplares': ex, 'chamada': gerar_chamada(cdd, autor)})
            n_l += 1
    credenciais_tmp = []
    for nome, perms in EXEMPLO_USUARIOS:
        if not row("SELECT 1 FROM usuarios WHERE nome = ?", (nome,)):
            senha_tmp = gerar_senha_temporaria()
            run("INSERT INTO usuarios(nome, senha, admin, perms, senha_temporaria) VALUES (?, ?, 0, ?, 1)",
                (nome, generate_password_hash(senha_tmp), ','.join(perms)))
            credenciais_tmp.append(f'{nome}: {senha_tmp}')
            n_u += 1
    for i, nome in enumerate(EXEMPLO_LEITORES, 1):
        if not row("SELECT 1 FROM leitores WHERE nome = ?", (nome,)):
            senha_tmp = gerar_senha_temporaria()
            matricula = f'EX{i:03d}'
            run("INSERT INTO leitores(nome, matricula, email, telefone, senha, senha_temporaria) VALUES (?, ?, ?, '', ?, 1)",
                (nome, matricula, f'exemplo{i}@exemplo.com', generate_password_hash(senha_tmp)))
            credenciais_tmp.append(f'{matricula} ({nome}): {senha_tmp}')
            n_r += 1
    log(f'Carregou dados de exemplo: {n_l} itens, {n_u} usuários, {n_r} leitores')
    mensagem = f'Exemplos carregados: {n_l} itens, {n_u} colaboradores e {n_r} leitores novos.'
    if credenciais_tmp:
        mensagem += ' Senhas temporárias, mostradas uma vez: ' + ' | '.join(credenciais_tmp)
    flash(mensagem)
    return redirect(url_for('usuarios'))


# ---------------------------------------------------------------- inicialização

init_db()

if __name__ == '__main__':
    rede = os.environ.get('BIBLIO_REDE', '').strip().lower() in ('1','true','sim','s')
    try:
        from waitress import serve
    except ImportError:
        if rede:
            raise SystemExit('Waitress não está instalado. Execute novamente o iniciador para instalar as dependências.')
        print('Aviso: Waitress ausente; usando servidor local de desenvolvimento.')
        serve = None
    if rede:
        pendentes = rows("SELECT 'Equipa' AS tipo,nome FROM usuarios WHERE senha_temporaria=1 UNION ALL SELECT 'Leitor',nome FROM leitores WHERE senha_temporaria=1")
        if pendentes:
            nomes = ', '.join(f"{r['tipo']}: {r['nome']}" for r in pendentes)
            raise SystemExit('Acesso LAN bloqueado até todas as senhas temporárias serem substituídas. Inicie em modo local e resolva: ' + nomes)
        import socket
        candidatos = socket.gethostbyname_ex(socket.gethostname())[2]
        ip = next((x for x in candidatos if not x.startswith('127.')), '127.0.0.1')
        print(f'Acesso local: http://localhost:5000 | Rede local: http://{ip}:5000')
        print('Modo de rede local ativo. Não exponha esta aplicação diretamente à Internet.')
        serve(app, host='0.0.0.0', port=5000, threads=8)
    else:
        print('Biblioteca rodando apenas neste computador: http://localhost:5000')
        if serve:
            serve(app, host='127.0.0.1', port=5000, threads=4)
        else:
            app.run(host='127.0.0.1', port=5000, debug=False)




