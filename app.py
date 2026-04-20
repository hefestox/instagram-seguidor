from flask import Flask, jsonify, request, make_response, send_from_directory, session, redirect, url_for, send_file
from functools import wraps
import sqlite3
import os
import uuid
import re
import secrets
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nichopost_secret_key')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

CARTEIRA_ADMIN  = '0xBa4D5e87e8bcaA85bF29105AB3171b9fDb2eF9dd'
PLANO_PRECO     = 9.99
PLANO_NOME      = 'NichoPost Pro'
TAXA_PLATAFORMA = 0.10
TAXA_SAQUE      = 0.02
COMISSAO_INDICACAO_ASSINATURA = 0.20
COMISSAO_INDICACAO_TAXA_SAQUE = 0.01

PRECOS = {
    'curtida':    0.001,
    'comentario': 0.002,
    'seguir':     0.003,
    'stories':    0.0005,
    'story':      0.0005,
}

nichos = ["biblico", "futebol", "politica", "entretenimento", "moda",
          "gastronomia", "fitness", "financas", "games", "viagem"]

conteudos = {
    "biblico":        "Versículo do dia: 'O Senhor é meu pastor, nada me faltará.' #fé #bíblia #cristão",
    "futebol":        "Análise: O time venceu com gol no último minuto! #futebol #esporte #vitória",
    "politica":       "Candidato X propõe mudanças importantes. #política #eleições #brasil",
    "entretenimento": "Novo filme blockbuster chega aos cinemas! #cinema #entretenimento #filme",
    "moda":           "Tendências primavera: Cores vibrantes e tecidos leves. #moda #beleza",
    "gastronomia":    "Receita fácil: Brigadeiro de chocolate caseiro. #culinária #receita",
    "fitness":        "Treino do dia: 30 min de corrida + alongamento. #fitness #saúde",
    "financas":       "Dica: Invista em cripto com cautela. #finanças #cripto #investimento",
    "games":          "Novo update do jogo traz missões épicas! #games #gaming #aventura",
    "viagem":         "Destino incrível: Praias paradisíacas em Bali. #viagem #lifestyle",
}


# ---- DB ----

def get_db():
    conn = sqlite3.connect('nichopost.db')
    conn.row_factory = sqlite3.Row
    return conn



def table_columns(conn, table_name):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]


def ensure_column(conn, table_name, column_name, column_sql):
    cols = table_columns(conn, table_name)
    if column_name not in cols:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def make_ref_code(username):
    base = re.sub(r'[^a-zA-Z0-9]', '', username).lower()[:8] or 'user'
    return f"{base}{secrets.token_hex(3)}"


def ensure_unique_ref_code(conn, username):
    while True:
        code = make_ref_code(username)
        exists = conn.execute('SELECT 1 FROM users WHERE ref_code=?', (code,)).fetchone()
        if not exists:
            return code


def registrar_comissao(conn, referrer, referred, origem, base_valor, percentual, valor, ref_tabela, ref_id):
    if not referrer or valor <= 0:
        return
    ja = conn.execute(
        'SELECT id FROM comissoes_indicacao WHERE origem=? AND ref_tabela=? AND ref_id=? AND referrer=?',
        (origem, ref_tabela, str(ref_id), referrer)
    ).fetchone()
    if ja:
        return
    conn.execute(
        '''INSERT INTO comissoes_indicacao
           (referrer,referred,origem,base_valor,percentual,valor,ref_tabela,ref_id)
           VALUES(?,?,?,?,?,?,?,?)''',
        (referrer, referred, origem, base_valor, percentual, valor, ref_tabela, str(ref_id))
    )
    conn.execute('UPDATE users SET saldo=saldo+?, ganhos_indicacao=COALESCE(ganhos_indicacao,0)+? WHERE username=?',
                 (valor, valor, referrer))

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            saldo REAL DEFAULT 0,
            is_worker INTEGER DEFAULT 0,
            subscribed INTEGER DEFAULT 0,
            ref_code TEXT UNIQUE,
            indicado_por TEXT,
            ganhos_indicacao REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS contas (
            id INTEGER PRIMARY KEY,
            nome TEXT UNIQUE NOT NULL,
            seguidores INTEGER DEFAULT 0,
            nicho TEXT,
            usuario TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agendamentos (
            id INTEGER PRIMARY KEY,
            conta TEXT,
            conteudo TEXT,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS campanhas (
            id INTEGER PRIMARY KEY,
            nome TEXT,
            alcance INTEGER DEFAULT 0,
            cliques INTEGER DEFAULT 0,
            custo REAL,
            nicho TEXT
        );
        CREATE TABLE IF NOT EXISTS tarefas (
            id INTEGER PRIMARY KEY,
            tipo TEXT NOT NULL,
            quantidade INTEGER NOT NULL,
            nicho TEXT,
            status TEXT DEFAULT 'pendente',
            trabalhador TEXT,
            proof TEXT,
            contratante TEXT NOT NULL,
            valor_total REAL NOT NULL,
            recompensa REAL NOT NULL,
            conta TEXT NOT NULL,
            data_criacao DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS saques (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            valor REAL NOT NULL,
            carteira TEXT NOT NULL,
            taxa REAL NOT NULL,
            valor_liquido REAL NOT NULL,
            status TEXT DEFAULT 'pendente',
            data_solicitacao DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS depositos (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            valor REAL NOT NULL,
            txid TEXT NOT NULL UNIQUE,
            proof TEXT NOT NULL,
            status TEXT DEFAULT 'pendente',
            observacao TEXT,
            aprovado_por TEXT,
            data_solicitacao DATETIME DEFAULT CURRENT_TIMESTAMP,
            data_processamento DATETIME
        );
        CREATE TABLE IF NOT EXISTS comissoes_indicacao (
            id INTEGER PRIMARY KEY,
            referrer TEXT NOT NULL,
            referred TEXT NOT NULL,
            origem TEXT NOT NULL,
            base_valor REAL NOT NULL,
            percentual REAL NOT NULL,
            valor REAL NOT NULL,
            ref_tabela TEXT,
            ref_id TEXT,
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    ensure_column(conn, 'users', 'ref_code', 'TEXT')
    ensure_column(conn, 'users', 'indicado_por', 'TEXT')
    ensure_column(conn, 'users', 'ganhos_indicacao', 'REAL DEFAULT 0')

    rows_users = conn.execute('SELECT id, username, ref_code FROM users').fetchall()
    for row in rows_users:
        if not row['ref_code']:
            conn.execute('UPDATE users SET ref_code=? WHERE id=?', (ensure_unique_ref_code(conn, row['username']), row['id']))


    if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        for u in [
            ('admin',   generate_password_hash('87347748'), 1000, 1, 1),
            ('worker1', generate_password_hash('pass'),       10, 1, 0),
            ('user1',   generate_password_hash('pass'),       50, 0, 1),
        ]:
            username, password_hash, saldo, is_worker, subscribed = u
            conn.execute('INSERT INTO users (username,password,saldo,is_worker,subscribed,ref_code,ganhos_indicacao) VALUES(?,?,?,?,?,?,?)',
                         (username, password_hash, saldo, is_worker, subscribed, ensure_unique_ref_code(conn, username), 0))

    if conn.execute('SELECT COUNT(*) FROM contas').fetchone()[0] == 0:
        for c in [
            ('@conta_biblica',     1250, 'biblico', 'admin'),
            ('@futebol_news',      3400, 'futebol', 'admin'),
            ('@politica_atual',     890, 'politica', 'admin'),
            ('@entretenimento_fun',2100, 'entretenimento', 'admin'),
            ('@pump_sniper',        500, 'financas', 'admin'),
        ]:
            conn.execute('INSERT INTO contas (nome,seguidores,nicho,usuario) VALUES(?,?,?,?)', c)

    if conn.execute('SELECT COUNT(*) FROM agendamentos').fetchone()[0] == 0:
        for a in [
            ('@conta_biblica',  'Post bíblico',     '2026-04-20T10:00'),
            ('@futebol_news',   'Análise de jogo',  '2026-04-21T14:00'),
            ('@politica_atual', 'Notícia política', '2026-04-22T18:00'),
        ]:
            conn.execute('INSERT INTO agendamentos (conta,conteudo,data) VALUES(?,?,?)', a)

    if conn.execute('SELECT COUNT(*) FROM campanhas').fetchone()[0] == 0:
        for c in [
            ('Campanha Bíblica',  5000, 150, 50,  'biblico'),
            ('Campanha Futebol', 12000, 400, 120, 'futebol'),
            ('Campanha Política', 3200,  80,  30, 'politica'),
        ]:
            conn.execute('INSERT INTO campanhas (nome,alcance,cliques,custo,nicho) VALUES(?,?,?,?,?)', c)

    conn.commit()
    conn.close()


init_db()


# ---- DECORATORS ----

def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            if request.path.startswith('/api/'):
                return jsonify({"error": "Nao autenticado"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ---- AUTH PAGES (HTML puro, sem Jinja2) ----

LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>NichoPost Login</title>
<style>
body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;background:#f0f2f5}
.box{background:#fff;padding:2rem;border-radius:12px;width:300px;
box-shadow:0 2px 12px rgba(0,0,0,.1)}
h2{margin:0 0 1.5rem;font-size:1.2rem;color:#111}
input{width:100%;padding:9px;margin:3px 0 12px;border:1px solid #ddd;
border-radius:7px;box-sizing:border-box;font-size:14px}
button{width:100%;padding:10px;background:#378ADD;color:#fff;border:none;
border-radius:7px;cursor:pointer;font-size:14px;font-weight:500}
button:hover{background:#185FA5}
.err{color:red;font-size:13px;margin-bottom:10px}
a{font-size:13px;color:#378ADD;text-decoration:none}
p{margin:12px 0 0}
</style></head>
<body><div class="box">
<h2>NichoPost</h2>
__ERROR__
<form method="post">
<input name="username" placeholder="Usuário" required>
<input name="password" type="password" placeholder="Senha" required>
<button type="submit">Entrar</button>
</form>
<p><a href="/register">Criar conta grátis</a></p>
</div></body></html>"""

REGISTER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>NichoPost Registro</title>
<style>
body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;background:#f0f2f5}
.box{background:#fff;padding:2rem;border-radius:12px;width:320px;
box-shadow:0 2px 12px rgba(0,0,0,.1)}
h2{margin:0 0 1.5rem;font-size:1.2rem;color:#111}
input[type=text],input[type=password]{width:100%;padding:9px;margin:3px 0 12px;
border:1px solid #ddd;border-radius:7px;box-sizing:border-box;font-size:14px}
.role-options{margin:15px 0}
.role-option{display:flex;align-items:center;gap:7px;margin-bottom:10px}
.role-option input[type=radio]{margin:0}
.role-option label{font-size:13px;cursor:pointer;margin:0}
button{width:100%;padding:10px;background:#378ADD;color:#fff;border:none;
border-radius:7px;cursor:pointer;font-size:14px;font-weight:500}
button:hover{background:#185FA5}
.err{color:red;font-size:13px;margin-bottom:10px}
a{font-size:13px;color:#378ADD;text-decoration:none}
p{margin:12px 0 0}
</style></head>
<body><div class="box">
<h2>Criar conta</h2>
__ERROR__
<form method="post">
<input type="text" name="username" placeholder="Usuário" required>
<input type="password" name="password" placeholder="Senha" required>
<input type="text" name="ref_code" placeholder="Código de indicação (opcional)" value="__REF__">
<div class="role-options">
    <p style="font-size:13px;margin:0 0 10px;color:#555">Escolha seu perfil:</p>
    <div class="role-option">
        <input type="radio" id="worker" name="role" value="worker" required>
        <label for="worker">👷 Quero ser trabalhador e ganhar USDT executando tarefas</label>
    </div>
    <div class="role-option">
        <input type="radio" id="contractor" name="role" value="contractor" required>
        <label for="contractor">📋 Quero ser contratante e criar tarefas para terceiros</label>
    </div>
</div>
<button type="submit">Registrar</button>
</form>
<p><a href="/login">Já tenho conta</a></p>
</div></body></html>"""


def html_resp(html, code=200):
    r = make_response(html, code)
    r.headers['Content-Type'] = 'text/html; charset=utf-8'
    return r


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user'] = user['username']
            return redirect(url_for('index'))
        error = '<p class="err">Usuário ou senha inválidos.</p>'
    return html_resp(LOGIN_HTML.replace('__ERROR__', error))


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', '')
        ref_code = request.form.get('ref_code', '').strip().lower()
        is_worker = 1 if role == 'worker' else 0
        if not username or not password or not role:
            error = '<p class="err">Preencha todos os campos.</p>'
        else:
            conn = get_db()
            try:
                indicado_por = None
                if ref_code:
                    dono_ref = conn.execute('SELECT username FROM users WHERE lower(ref_code)=lower(?)', (ref_code,)).fetchone()
                    if not dono_ref:
                        error = '<p class="err">Código de indicação inválido.</p>'
                    elif dono_ref['username'].lower() == username.lower():
                        error = '<p class="err">Você não pode usar seu próprio código.</p>'
                    else:
                        indicado_por = dono_ref['username']
                if not error:
                    conn.execute(
                        'INSERT INTO users (username,password,is_worker,ref_code,indicado_por,ganhos_indicacao) VALUES(?,?,?,?,?,?)',
                        (username, generate_password_hash(password), is_worker, ensure_unique_ref_code(conn, username), indicado_por, 0)
                    )
                    conn.commit()
                    session['user'] = username
                    return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                error = '<p class="err">Usuário já existe.</p>'
            finally:
                conn.close()
    ref_prefill = request.args.get('ref', '') if request.method == 'GET' else request.form.get('ref_code', '')
    html = REGISTER_HTML.replace('__ERROR__', error).replace('__REF__', ref_prefill)
    return html_resp(html)


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))


@app.route('/r/<ref_code>')
def referral_redirect(ref_code):
    return redirect(url_for('register', ref=ref_code))


# ---- FRONTEND ----

@app.route('/')
@require_login
def index():
    return send_file('nichopost_usdt_platform.html')


# ---- API: ME ----

@app.route('/api/me')
@require_login
def api_me():
    conn = get_db()
    row = conn.execute('SELECT username,saldo,is_worker,subscribed,ref_code,indicado_por,ganhos_indicacao FROM users WHERE username=?', (session['user'],)).fetchone()
    conn.close()
    return jsonify({
        "user":           row['username'],
        "saldo":          round(row['saldo'], 4),
        "is_worker":      bool(row['is_worker']),
        "subscribed":     bool(row['subscribed']),
        "nichos":         nichos,
        "precos":         PRECOS,
        "carteira_admin": CARTEIRA_ADMIN,
        "plano_preco":    PLANO_PRECO,
        "ref_code":        row['ref_code'],
        "indicado_por":    row['indicado_por'],
        "ganhos_indicacao": round(row['ganhos_indicacao'] or 0, 4),
        "link_indicacao":  request.host_url.rstrip('/') + url_for('referral_redirect', ref_code=row['ref_code'])
    })


# ---- API: CONFIG ----

@app.route('/api/config')
def api_config():
    return jsonify({
        "carteira_admin":      CARTEIRA_ADMIN,
        "rede":                "Ethereum ERC-20",
        "plano":               PLANO_NOME,
        "plano_preco":         PLANO_PRECO,
        "precos":              PRECOS,
        "taxa_plataforma_pct": TAXA_PLATAFORMA * 100,
        "taxa_saque_pct":      TAXA_SAQUE * 100,
    })

@app.route('/api/indicacao')
@require_login
def api_indicacao():
    conn = get_db()
    user = conn.execute('SELECT username,ref_code,ganhos_indicacao FROM users WHERE username=?', (session['user'],)).fetchone()
    indicados = conn.execute(
        'SELECT username,saldo,subscribed,is_worker FROM users WHERE indicado_por=? ORDER BY id DESC',
        (session['user'],)
    ).fetchall()
    comissoes = conn.execute(
        'SELECT referred,origem,base_valor,percentual,valor,criado_em FROM comissoes_indicacao WHERE referrer=? ORDER BY criado_em DESC LIMIT 50',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify({
        'ref_code': user['ref_code'],
        'link': request.host_url.rstrip('/') + url_for('referral_redirect', ref_code=user['ref_code']),
        'ganhos_totais': round(user['ganhos_indicacao'] or 0, 4),
        'percentual_assinatura': COMISSAO_INDICACAO_ASSINATURA * 100,
        'percentual_taxa_saque': COMISSAO_INDICACAO_TAXA_SAQUE * 100,
        'indicados': [dict(r) for r in indicados],
        'comissoes': [dict(r) for r in comissoes]
    })


# ---- API: DASHBOARD ----

@app.route('/api/dashboard')
@require_login
def api_dashboard():
    conn = get_db()
    num_contas  = conn.execute('SELECT COUNT(*) FROM contas WHERE usuario=?', (session['user'],)).fetchone()[0]
    num_posts   = conn.execute('SELECT COUNT(*) FROM agendamentos WHERE conta IN (SELECT nome FROM contas WHERE usuario=?)', (session['user'],)).fetchone()[0]
    orcamento   = conn.execute('SELECT COALESCE(SUM(custo),0) FROM campanhas').fetchone()[0]
    num_tarefas = conn.execute("SELECT COUNT(*) FROM tarefas WHERE status='pendente' AND conta IN (SELECT nome FROM contas WHERE usuario=?)", (session['user'],)).fetchone()[0]
    conn.close()
    return jsonify({
        "contas": num_contas,
        "posts_agendados": num_posts,
        "orcamento_anuncios": round(orcamento, 2),
        "tarefas_marketplace": num_tarefas,
    })


# ---- API: CONTAS ----

@app.route('/api/contas', methods=['GET', 'POST'])
@require_login
def api_contas():
    conn = get_db()
    if request.method == 'POST':
        data  = request.json or {}
        nome  = data.get('nome', '').strip()
        nicho = data.get('nicho', '').strip()
        if not nome or not nicho:
            conn.close()
            return jsonify({"error": "nome e nicho sao obrigatorios"}), 400
        try:
            conn.execute('INSERT INTO contas (nome,nicho,usuario) VALUES(?,?,?)', (nome, nicho, session['user']))
            conn.commit()
            conn.close()
            return jsonify({"message": "Conta adicionada"}), 201
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({"error": "Conta ja existe"}), 409
    rows = conn.execute('SELECT nome,seguidores,nicho FROM contas WHERE usuario=?', (session['user'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---- API: AGENDAMENTOS ----

@app.route('/api/agendamentos', methods=['GET', 'POST'])
@require_login
def api_agendamentos():
    conn = get_db()
    if request.method == 'POST':
        data     = request.json or {}
        conta    = data.get('conta', '').strip()
        conteudo = data.get('conteudo', '').strip()
        data_pub = data.get('data', '').strip()
        if not conta or not conteudo or not data_pub:
            conn.close()
            return jsonify({"error": "conta, conteudo e data sao obrigatorios"}), 400
        
        # Verificar se a conta pertence ao usuário
        conta_existe = conn.execute('SELECT COUNT(*) FROM contas WHERE nome=? AND usuario=?', (conta, session['user'])).fetchone()[0]
        if conta_existe == 0:
            conn.close()
            return jsonify({"error": "Conta nao encontrada ou nao pertence ao usuario"}), 403
        
        conn.execute('INSERT INTO agendamentos (conta,conteudo,data) VALUES(?,?,?)', (conta, conteudo, data_pub))
        conn.commit()
        conn.close()
        return jsonify({"message": "Post agendado"}), 201
    rows = conn.execute('SELECT conta,conteudo,data FROM agendamentos WHERE conta IN (SELECT nome FROM contas WHERE usuario=?)', (session['user'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---- API: CAMPANHAS ----

@app.route('/api/campanhas', methods=['GET', 'POST'])
@require_login
def api_campanhas():
    conn = get_db()
    if request.method == 'POST':
        data      = request.json or {}
        nome      = data.get('nome', '').strip()
        orcamento = float(data.get('orcamento', 0))
        nicho     = data.get('nicho', '').strip()
        if not nome or not nicho or orcamento <= 0:
            conn.close()
            return jsonify({"error": "nome, nicho e orcamento > 0 sao obrigatorios"}), 400
        conn.execute('INSERT INTO campanhas (nome,custo,nicho) VALUES(?,?,?)', (nome, orcamento, nicho))
        conn.commit()
        conn.close()
        return jsonify({"message": "Campanha criada"}), 201
    rows = conn.execute('SELECT nome,alcance,cliques,custo,nicho FROM campanhas').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---- API: CONTEUDO ----

@app.route('/api/conteudo/<nicho>')
@require_login
def api_conteudo(nicho):
    texto = conteudos.get(nicho)
    if not texto:
        return jsonify({"error": "Nicho nao encontrado", "disponiveis": nichos}), 404
    return jsonify({"nicho": nicho, "conteudo": texto})


# ---- API: CALCULADORA ----

@app.route('/api/marketplace/calcular', methods=['POST'])
@require_login
def api_calcular():
    data        = request.json or {}
    curtidas    = max(0, int(data.get('curtidas',    0)))
    comentarios = max(0, int(data.get('comentarios', 0)))
    seguidores  = max(0, int(data.get('seguidores',  0)))
    stories     = max(0, int(data.get('stories',     0)))
    c_c  = curtidas    * PRECOS['curtida']
    c_co = comentarios * PRECOS['comentario']
    c_s  = seguidores  * PRECOS['seguir']
    c_st = stories     * PRECOS['stories']
    sub  = c_c + c_co + c_s + c_st
    taxa = sub * TAXA_PLATAFORMA
    return jsonify({
        "curtidas":    round(c_c,  4),
        "comentarios": round(c_co, 4),
        "seguidores":  round(c_s,  4),
        "stories":     round(c_st, 4),
        "subtotal":    round(sub,  4),
        "taxa_10pct":  round(taxa, 4),
        "total":       round(sub + taxa, 4),
    })


# ---- API: ASSINATURA ----

@app.route('/api/assinar', methods=['POST'])
@require_login
def assinar():
    conn = get_db()
    row  = conn.execute('SELECT id,saldo,subscribed,indicado_por FROM users WHERE username=?', (session['user'],)).fetchone()
    if row['subscribed']:
        conn.close()
        return jsonify({"message": "Voce ja possui o NichoPost Pro"}), 200
    if row['saldo'] < PLANO_PRECO:
        conn.close()
        return jsonify({"error": "Saldo insuficiente. Deposite USDT e tente novamente."}), 403
    conn.execute('UPDATE users SET saldo=saldo-?, subscribed=1 WHERE username=?', (PLANO_PRECO, session['user']))
    bonus = 0
    if row['indicado_por']:
        bonus = round(PLANO_PRECO * COMISSAO_INDICACAO_ASSINATURA, 6)
        registrar_comissao(conn, row['indicado_por'], session['user'], 'assinatura', PLANO_PRECO,
                           COMISSAO_INDICACAO_ASSINATURA, bonus, 'users', row['id'])
    conn.commit()
    conn.close()
    msg = 'Assinatura NichoPost Pro ativada com sucesso!'
    if bonus > 0:
        msg += f' Seu indicador recebeu {bonus:.4f} USDT.'
    return jsonify({"message": msg}), 200


# ---- API: TAREFAS ----

@app.route('/api/tarefas', methods=['GET', 'POST'])
@require_login
def api_tarefas():
    conn = get_db()
    if request.method == 'POST':
        row = conn.execute('SELECT subscribed,saldo FROM users WHERE username=?', (session['user'],)).fetchone()
        if not row['subscribed']:
            conn.close()
            return jsonify({"error": "Assine o NichoPost Pro para contratar tarefas"}), 403

        data       = request.json or {}
        tipo       = data.get('tipo', '').strip()
        quantidade = max(1, int(data.get('quantidade', 1)))
        nicho      = data.get('nicho', 'geral').strip()
        conta      = data.get('conta', '').strip()

        if not tipo or not conta:
            conn.close()
            return jsonify({"error": "tipo e conta sao obrigatorios"}), 400
        if tipo not in PRECOS:
            conn.close()
            return jsonify({"error": "Tipo invalido. Use: curtida, comentario, seguir, stories ou story"}), 400

        conta_existe = conn.execute(
            'SELECT COUNT(*) FROM contas WHERE nome=? AND usuario=?',
            (conta, session['user'])
        ).fetchone()[0]
        if conta_existe == 0:
            conn.close()
            return jsonify({"error": "Conta nao encontrada ou nao pertence ao usuario"}), 403

        recompensa  = PRECOS[tipo]
        subtotal    = recompensa * quantidade
        valor_total = round(subtotal + subtotal * TAXA_PLATAFORMA, 6)

        if row['saldo'] < valor_total:
            conn.close()
            return jsonify({"error": "Saldo insuficiente. Necessario: " + str(valor_total) + " USDT"}), 403

        conn.execute('UPDATE users SET saldo=saldo-? WHERE username=?', (valor_total, session['user']))
        conn.execute(
            'INSERT INTO tarefas (tipo,quantidade,nicho,contratante,valor_total,recompensa,conta,status) VALUES(?,?,?,?,?,?,?,?)',
            (tipo, quantidade, nicho, session['user'], valor_total, recompensa, conta, 'pendente')
        )
        conn.commit()
        conn.close()
        return jsonify({
            "message": str(quantidade) + " " + tipo + "(s) contratada(s) para " + conta,
            "valor_total": valor_total,
            "recompensa_por_acao": recompensa,
        }), 201

    rows = conn.execute(
        "SELECT id,tipo,quantidade,nicho,status,recompensa,valor_total,conta,trabalhador FROM tarefas WHERE (trabalhador IS NULL OR trabalhador=?) AND status != 'cancelada' ORDER BY data_criacao DESC",
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/tarefas/disponiveis')
@require_login
def tarefas_disponiveis():
    conn = get_db()

    row = conn.execute('SELECT is_worker FROM users WHERE username=?', (session['user'],)).fetchone()
    if not row or not row['is_worker']:
        conn.close()
        return jsonify({
            "blocked": True,
            "message": "Seu perfil nao esta cadastrado como trabalhador.",
            "tasks": []
        })

    tarefa_em_andamento = conn.execute(
        "SELECT id,tipo,quantidade,conta,nicho,recompensa,valor_total FROM tarefas WHERE trabalhador=? AND status='em_andamento' ORDER BY data_criacao DESC LIMIT 1",
        (session['user'],)
    ).fetchone()

    if tarefa_em_andamento:
        conn.close()
        return jsonify({
            "blocked": True,
            "message": "Voce ja possui uma tarefa em andamento. Conclua ela antes de pegar outra.",
            "current_task": dict(tarefa_em_andamento),
            "tasks": []
        })

    rows = conn.execute(
        "SELECT id,tipo,quantidade,conta,nicho,recompensa,valor_total FROM tarefas WHERE trabalhador IS NULL AND status='pendente' ORDER BY data_criacao DESC"
    ).fetchall()
    conn.close()
    return jsonify({
        "blocked": False,
        "tasks": [dict(r) for r in rows]
    })


@app.route('/api/minhas_tarefas')
@require_login
def minhas_tarefas():
    conn = get_db()
    rows = conn.execute(
        'SELECT id,tipo,quantidade,nicho,status,recompensa,valor_total,conta FROM tarefas WHERE trabalhador=? ORDER BY data_criacao DESC',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/minhas_tarefas_contratadas')
@require_login
def minhas_tarefas_contratadas():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,tipo,quantidade,status,recompensa,valor_total,conta,trabalhador FROM tarefas WHERE contratante=? AND status != 'cancelada' ORDER BY data_criacao DESC",
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---- API: PEGAR TAREFA ----

@app.route('/api/pegar_tarefa/<int:tarefa_id>', methods=['POST'])
@require_login
def pegar_tarefa(tarefa_id):
    conn = get_db()
    row = conn.execute('SELECT is_worker FROM users WHERE username=?', (session['user'],)).fetchone()
    if not row or not row['is_worker']:
        conn.close()
        return jsonify({"error": "Registre-se como trabalhador para pegar tarefas"}), 403

    tarefa_em_andamento = conn.execute(
        "SELECT id FROM tarefas WHERE trabalhador=? AND status='em_andamento' LIMIT 1",
        (session['user'],)
    ).fetchone()
    if tarefa_em_andamento:
        conn.close()
        return jsonify({"error": "Voce ja possui uma tarefa em andamento. Conclua ela antes de pegar outra."}), 403

    tarefa = conn.execute(
        "SELECT id FROM tarefas WHERE id=? AND trabalhador IS NULL AND status='pendente'",
        (tarefa_id,)
    ).fetchone()
    if not tarefa:
        conn.close()
        return jsonify({"error": "Tarefa nao disponivel"}), 404
    conn.execute("UPDATE tarefas SET trabalhador=?, status='em_andamento' WHERE id=?", (session['user'], tarefa_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "Tarefa iniciada! Envie o comprovante para receber."}), 200


# ---- API: VERIFICAR TAREFA ----

@app.route('/api/verificar/<int:tarefa_id>', methods=['POST'])
@require_login
def verificar_tarefa(tarefa_id):
    if 'proof' not in request.files:
        return jsonify({"error": "Envie o comprovante (screenshot)"}), 400
    arquivo = request.files['proof']
    if not arquivo.filename:
        return jsonify({"error": "Arquivo vazio"}), 400
    ext = os.path.splitext(arquivo.filename)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif'):
        return jsonify({"error": "Formato invalido. Use png/jpg/jpeg/webp"}), 400
    filename = str(uuid.uuid4()) + ext
    arquivo.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    conn = get_db()
    tarefa = conn.execute(
        "SELECT * FROM tarefas WHERE id=? AND trabalhador=? AND status='em_andamento'",
        (tarefa_id, session['user'])
    ).fetchone()
    if not tarefa:
        conn.close()
        return jsonify({"error": "Tarefa nao encontrada ou nao pertence a voce"}), 404
    ganho = round(tarefa['recompensa'] * tarefa['quantidade'], 6)
    conn.execute("UPDATE tarefas SET status='concluida', proof=? WHERE id=?", (filename, tarefa_id))
    conn.execute('UPDATE users SET saldo=saldo+? WHERE username=?', (ganho, session['user']))
    conn.commit()
    conn.close()
    return jsonify({"message": "Comprovante aceito! Voce ganhou " + str(ganho) + " USDT", "ganho": ganho}), 200


# ---- API: SALDO ----

@app.route('/api/saldo_trabalhador')
@require_login
def saldo_trabalhador():
    conn = get_db()
    row    = conn.execute('SELECT saldo FROM users WHERE username=?', (session['user'],)).fetchone()
    ganhos = conn.execute(
        "SELECT COALESCE(SUM(recompensa*quantidade),0) FROM tarefas WHERE trabalhador=? AND status='concluida'",
        (session['user'],)
    ).fetchone()[0]
    total  = conn.execute(
        "SELECT COUNT(*) FROM tarefas WHERE trabalhador=? AND status='concluida'",
        (session['user'],)
    ).fetchone()[0]
    conn.close()
    return jsonify({
        "saldo":             round(row['saldo'], 4),
        "ganhos_totais":     round(ganhos, 4),
        "tarefas_completas": total,
    })


# ---- API: DEPOSITO ----

@app.route('/api/deposito', methods=['POST'])
@require_login
def solicitar_deposito():
    valor_txt = (request.form.get('valor') or '').strip()
    txid = (request.form.get('txid') or '').strip()
    if not valor_txt:
        return jsonify({"error": "Informe o valor do aporte"}), 400
    try:
        valor = float(valor_txt)
    except ValueError:
        return jsonify({"error": "Valor invalido"}), 400
    if valor <= 0:
        return jsonify({"error": "O valor precisa ser maior que zero"}), 400
    if len(txid) < 8:
        return jsonify({"error": "Informe um TXID valido"}), 400
    if 'proof' not in request.files:
        return jsonify({"error": "Envie o comprovante do deposito"}), 400

    arquivo = request.files['proof']
    if not arquivo.filename:
        return jsonify({"error": "Arquivo de comprovante vazio"}), 400
    ext = os.path.splitext(arquivo.filename)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.pdf'):
        return jsonify({"error": "Formato invalido. Use png/jpg/jpeg/webp/gif/pdf"}), 400

    filename = str(uuid.uuid4()) + ext
    arquivo.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    conn = get_db()
    ja_existe = conn.execute('SELECT id FROM depositos WHERE txid=?', (txid,)).fetchone()
    if ja_existe:
        conn.close()
        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        except OSError:
            pass
        return jsonify({"error": "Esse TXID ja foi enviado para analise"}), 409

    conn.execute(
        'INSERT INTO depositos (usuario,valor,txid,proof,status) VALUES(?,?,?,?,?)',
        (session['user'], valor, txid, filename, 'pendente')
    )
    conn.commit()
    conn.close()
    return jsonify({
        "message": "Aporte enviado com sucesso. Aguarde a analise do admin.",
        "status": "pendente",
        "valor": round(valor, 4),
        "txid": txid
    }), 201


@app.route('/api/historico_depositos')
@require_login
def historico_depositos():
    conn = get_db()
    rows = conn.execute(
        'SELECT id,valor,txid,proof,status,observacao,data_solicitacao,data_processamento FROM depositos WHERE usuario=? ORDER BY data_solicitacao DESC LIMIT 20',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])



# ---- API: SAQUE ----

@app.route('/api/saque', methods=['POST'])
@require_login
def solicitar_saque():
    data     = request.json or {}
    valor    = float(data.get('valor', 0))
    carteira = data.get('carteira', '').strip()
    if valor < 0.01:
        return jsonify({"error": "Valor minimo para saque: 0.01 USDT"}), 400
    if not carteira.startswith('0x') or len(carteira) != 42:
        return jsonify({"error": "Endereco invalido. Use carteira ERC-20 (0x... 42 caracteres)"}), 400
    conn = get_db()
    row  = conn.execute('SELECT saldo FROM users WHERE username=?', (session['user'],)).fetchone()
    if row['saldo'] < valor:
        conn.close()
        return jsonify({"error": "Saldo insuficiente. Disponivel: " + str(round(row['saldo'], 4)) + " USDT"}), 403
    taxa          = round(valor * TAXA_SAQUE, 6)
    valor_liquido = round(valor - taxa, 6)
    conn.execute('UPDATE users SET saldo=saldo-? WHERE username=?', (valor, session['user']))
    conn.execute(
        'INSERT INTO saques (usuario,valor,carteira,taxa,valor_liquido,status) VALUES(?,?,?,?,?,?)',
        (session['user'], valor, carteira, taxa, valor_liquido, 'pendente')
    )
    conn.commit()
    conn.close()
    return jsonify({
        "message":       "Saque solicitado! Voce recebera " + str(valor_liquido) + " USDT apos taxa de " + str(taxa) + " USDT",
        "valor_bruto":   valor,
        "taxa_2pct":     taxa,
        "valor_liquido": valor_liquido,
        "carteira":      carteira,
        "status":        "pendente",
    }), 200


@app.route('/api/historico_saques')
@require_login
def historico_saques():
    conn = get_db()
    rows = conn.execute(
        'SELECT valor,carteira,taxa,valor_liquido,status,data_solicitacao FROM saques WHERE usuario=? ORDER BY data_solicitacao DESC LIMIT 20',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---- UPLOADS ----

@app.route('/api/uploads/<filename>')
@require_login
def serve_upload(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ---- API: ADMIN ----

@app.route('/api/admin/depositos_pendentes')
@require_login
def admin_depositos_pendentes():
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    conn = get_db()
    rows = conn.execute(
        'SELECT id,usuario,valor,txid,proof,status,data_solicitacao FROM depositos WHERE status=? ORDER BY data_solicitacao DESC',
        ('pendente',)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/admin/aprovar_deposito/<int:deposito_id>', methods=['POST'])
@require_login
def admin_aprovar_deposito(deposito_id):
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    data = request.get_json(silent=True) or {}
    observacao = (data.get('observacao') or '').strip()

    conn = get_db()
    deposito = conn.execute(
        'SELECT * FROM depositos WHERE id=? AND status=?',
        (deposito_id, 'pendente')
    ).fetchone()
    if not deposito:
        conn.close()
        return jsonify({"error": "Deposito nao encontrado ou ja processado"}), 404

    conn.execute('UPDATE users SET saldo=saldo+? WHERE username=?', (deposito['valor'], deposito['usuario']))
    conn.execute(
        """UPDATE depositos
           SET status='aprovado', observacao=?, aprovado_por=?, data_processamento=CURRENT_TIMESTAMP
           WHERE id=?""",
        (observacao, session['user'], deposito_id)
    )
    conn.commit()
    conn.close()
    return jsonify({
        "message": f"Deposito aprovado. {deposito['valor']} USDT creditados para {deposito['usuario']}.",
        "usuario": deposito['usuario'],
        "valor": deposito['valor']
    }), 200


@app.route('/api/admin/rejeitar_deposito/<int:deposito_id>', methods=['POST'])
@require_login
def admin_rejeitar_deposito(deposito_id):
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    data = request.get_json(silent=True) or {}
    observacao = (data.get('observacao') or '').strip()

    conn = get_db()
    deposito = conn.execute(
        'SELECT * FROM depositos WHERE id=? AND status=?',
        (deposito_id, 'pendente')
    ).fetchone()
    if not deposito:
        conn.close()
        return jsonify({"error": "Deposito nao encontrado ou ja processado"}), 404

    conn.execute(
        """UPDATE depositos
           SET status='rejeitado', observacao=?, aprovado_por=?, data_processamento=CURRENT_TIMESTAMP
           WHERE id=?""",
        (observacao, session['user'], deposito_id)
    )
    conn.commit()
    conn.close()
    return jsonify({
        "message": f"Deposito de {deposito['valor']} USDT rejeitado para {deposito['usuario']}.",
        "usuario": deposito['usuario'],
        "valor": deposito['valor']
    }), 200



@app.route('/api/admin/saques_pendentes')
@require_login
def admin_saques_pendentes():
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    conn = get_db()
    rows = conn.execute(
        'SELECT id,usuario,valor,carteira,taxa,valor_liquido,data_solicitacao FROM saques WHERE status=? ORDER BY data_solicitacao DESC',
        ('pendente',)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/admin/aprovar_saque/<int:saque_id>', methods=['POST'])
@require_login
def admin_aprovar_saque(saque_id):
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    conn = get_db()
    saque = conn.execute('SELECT * FROM saques WHERE id=? AND status=?', (saque_id, 'pendente')).fetchone()
    if not saque:
        conn.close()
        return jsonify({"error": "Saque não encontrado ou já processado"}), 404
    conn.execute('UPDATE saques SET status=? WHERE id=?', ('aprovado', saque_id))
    bonus = 0
    dono_ref = conn.execute('SELECT indicado_por FROM users WHERE username=?', (saque['usuario'],)).fetchone()
    if dono_ref and dono_ref['indicado_por']:
        bonus = round(float(saque['taxa']) * COMISSAO_INDICACAO_TAXA_SAQUE, 6)
        registrar_comissao(conn, dono_ref['indicado_por'], saque['usuario'], 'taxa_saque', float(saque['taxa']),
                           COMISSAO_INDICACAO_TAXA_SAQUE, bonus, 'saques', saque_id)
    conn.commit()
    conn.close()
    msg = f"Saque de {saque['valor']} USDT aprovado para {saque['usuario']}"
    if bonus > 0:
        msg += f" | Comissão do indicador: {bonus:.6f} USDT"
    return jsonify({"message": msg}), 200


@app.route('/api/admin/rejeitar_saque/<int:saque_id>', methods=['POST'])
@require_login
def admin_rejeitar_saque(saque_id):
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    conn = get_db()
    saque = conn.execute('SELECT * FROM saques WHERE id=? AND status=?', (saque_id, 'pendente')).fetchone()
    if not saque:
        conn.close()
        return jsonify({"error": "Saque não encontrado ou já processado"}), 404
    # Devolver o valor ao usuário
    conn.execute('UPDATE users SET saldo=saldo+? WHERE username=?', (saque['valor'], saque['usuario']))
    conn.execute('UPDATE saques SET status=? WHERE id=?', ('rejeitado', saque_id))
    conn.commit()
    conn.close()
    return jsonify({"message": f"Saque rejeitado. {saque['valor']} USDT devolvidos para {saque['usuario']}"}), 200


@app.route('/api/admin/tarefas_concluidas')
@require_login
def admin_tarefas_concluidas():
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    conn = get_db()
    rows = conn.execute(
        'SELECT id,tipo,quantidade,nicho,status,trabalhador,recompensa,conta,proof FROM tarefas WHERE status=? ORDER BY data_criacao DESC',
        ('concluida',)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/admin/aprovar_tarefa/<int:tarefa_id>', methods=['POST'])
@require_login
def admin_aprovar_tarefa(tarefa_id):
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    conn = get_db()
    tarefa = conn.execute('SELECT * FROM tarefas WHERE id=? AND status=?', (tarefa_id, 'concluida')).fetchone()
    if not tarefa:
        conn.close()
        return jsonify({"error": "Tarefa não encontrada ou já processada"}), 404
    # Já foi paga quando concluída, só marcar como aprovada
    conn.execute('UPDATE tarefas SET status=? WHERE id=?', ('aprovada', tarefa_id))
    conn.commit()
    conn.close()
    return jsonify({"message": f"Tarefa {tarefa_id} aprovada. Pagamento já foi realizado."}), 200


@app.route('/api/admin/rejeitar_tarefa/<int:tarefa_id>', methods=['POST'])
@require_login
def admin_rejeitar_tarefa(tarefa_id):
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    conn = get_db()
    tarefa = conn.execute('SELECT * FROM tarefas WHERE id=? AND status=?', (tarefa_id, 'concluida')).fetchone()
    if not tarefa:
        conn.close()
        return jsonify({"error": "Tarefa não encontrada ou já processada"}), 404
    # Devolver tarefa para pendente e remover trabalhador
    conn.execute('UPDATE tarefas SET status=?, trabalhador=NULL, proof=NULL WHERE id=?', ('pendente', tarefa_id))
    # Devolver o dinheiro ao trabalhador
    ganho = round(tarefa['recompensa'] * tarefa['quantidade'], 6)
    conn.execute('UPDATE users SET saldo=saldo-? WHERE username=?', (ganho, tarefa['trabalhador']))
    conn.commit()
    conn.close()
    return jsonify({"message": f"Tarefa rejeitada. {ganho} USDT devolvidos do trabalhador {tarefa['trabalhador']}"}), 200


@app.route('/api/admin/usuarios')
@require_login
def admin_usuarios():
    if session['user'] != 'admin':
        return jsonify({"error": "Acesso negado"}), 403
    conn = get_db()
    rows = conn.execute('SELECT username,saldo,is_worker,subscribed,indicado_por,ref_code,ganhos_indicacao FROM users ORDER BY username').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---- RUN ----

if __name__ == '__main__':
    app.run(
        debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true',
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000))
    )
