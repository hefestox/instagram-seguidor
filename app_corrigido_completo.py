from flask import Flask, jsonify, request, make_response, send_from_directory, session, redirect, url_for, send_file
from functools import wraps
import sqlite3
import os
import uuid
import base64
from decimal import Decimal, ROUND_HALF_UP
import requests
import urllib3
from werkzeug.security import generate_password_hash, check_password_hash

# Suprimir warnings de SSL em sandbox
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nichopost_secret_key')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

CARTEIRA_ADMIN  = '0xBa4D5e87e8bcaA85bF29105AB3171b9fDb2eF9dd'
PLANO_PRECO     = 9.99
PLANO_NOME      = 'NichoPost Pro'
TAXA_PLATAFORMA = 0.10
TAXA_SAQUE      = 0.02
EFI_PIX_EXPIRACAO = 3600

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


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            saldo REAL DEFAULT 0,
            is_worker INTEGER DEFAULT 0,
            subscribed INTEGER DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS pix_cobrancas (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            valor REAL NOT NULL,
            txid TEXT UNIQUE NOT NULL,
            loc_id INTEGER,
            status TEXT DEFAULT 'ATIVA',
            pix_copia_e_cola TEXT,
            imagem_qrcode TEXT,
            link_visualizacao TEXT,
            e2eid TEXT,
            webhook_recebido INTEGER DEFAULT 0,
            creditado INTEGER DEFAULT 0,
            origem TEXT DEFAULT 'efi_pix',
            data_criacao DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS movimentacoes (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            tipo TEXT NOT NULL,
            valor REAL NOT NULL,
            descricao TEXT,
            referencia TEXT,
            data_evento DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        for u in [
            ('admin',   generate_password_hash('87347748'), 1000, 1, 1),
            ('worker1', generate_password_hash('pass'),       10, 1, 0),
            ('user1',   generate_password_hash('pass'),       50, 0, 1),
        ]:
            conn.execute('INSERT INTO users (username,password,saldo,is_worker,subscribed) VALUES(?,?,?,?,?)', u)

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
        is_worker = 1 if role == 'worker' else 0
        if not username or not password or not role:
            error = '<p class="err">Preencha todos os campos.</p>'
        else:
            conn = get_db()
            try:
                conn.execute(
                    'INSERT INTO users (username,password,is_worker) VALUES(?,?,?)',
                    (username, generate_password_hash(password), is_worker)
                )
                conn.commit()
                session['user'] = username
                return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                error = '<p class="err">Usuário já existe.</p>'
            finally:
                conn.close()
    return html_resp(REGISTER_HTML.replace('__ERROR__', error))


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))


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
    row = conn.execute('SELECT username,saldo,is_worker,subscribed FROM users WHERE username=?', (session['user'],)).fetchone()
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


# ---- API: DASHBOARD ----

@app.route('/api/dashboard')
@require_login
def api_dashboard():
    conn = get_db()
    num_contas  = conn.execute('SELECT COUNT(*) FROM contas WHERE usuario=?', (session['user'],)).fetchone()[0]
    num_posts   = conn.execute('SELECT COUNT(*) FROM agendamentos WHERE conta IN (SELECT nome FROM contas WHERE usuario=?)', (session['user'],)).fetchone()[0]
    # A tabela campanhas não possui a coluna "conta".
    # Mantemos o somatório global das campanhas cadastradas.
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
    row  = conn.execute('SELECT saldo,subscribed FROM users WHERE username=?', (session['user'],)).fetchone()
    if row['subscribed']:
        conn.close()
        return jsonify({"message": "Voce ja possui o NichoPost Pro"}), 200
    if row['saldo'] < PLANO_PRECO:
        conn.close()
        return jsonify({"error": "Saldo insuficiente. Deposite USDT e tente novamente."}), 403
    conn.execute('UPDATE users SET saldo=saldo-?, subscribed=1 WHERE username=?', (PLANO_PRECO, session['user']))
    conn.commit()
    conn.close()
    return jsonify({"message": "Assinatura NichoPost Pro ativada com sucesso!"}), 200


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
    rows = conn.execute(
        "SELECT id,tipo,quantidade,conta,nicho,recompensa,valor_total FROM tarefas WHERE trabalhador IS NULL AND status='pendente' AND conta IN (SELECT nome FROM contas WHERE usuario=?) ORDER BY data_criacao DESC",
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


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
    conn.commit()
    conn.close()
    return jsonify({"message": f"Saque de {saque['valor']} USDT aprovado para {saque['usuario']}"}), 200


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
    conn.execute('UPDATE tarefas SET status=?, trabalhador=NULL, proof=NULL WHERE id=?', ('pendente', tarefa_id))
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
    rows = conn.execute('SELECT username,saldo,is_worker,subscribed FROM users ORDER BY username').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---- EFI PIX HELPERS ----

def _money2(v):
    return str(Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def efi_is_enabled():
    required = ['EFI_CLIENT_ID', 'EFI_CLIENT_SECRET', 'EFI_CERTIFICATE_PATH', 'EFI_PIX_KEY']
    return all(os.environ.get(k) for k in required)

def efi_is_sandbox():
    return os.environ.get('EFI_USE_SANDBOX', 'true').lower() == 'true'

def efi_base_url():
    return 'https://pix-h.api.efipay.com.br' if efi_is_sandbox() else 'https://pix.api.efipay.com.br'

def efi_certificate_path():
    return os.environ.get('EFI_CERTIFICATE_PATH', '').strip()

def efi_webhook_proxy_note():
    return 'O webhook Pix da Efí usa mTLS. Em Railway puro, o ideal é usar proxy mTLS/Nginx na frente antes de apontar o webhook automático.'

def efi_get_cert():
    """
    Retorna o cert para o requests.
    O arquivo PEM da Efí contém tanto o certificado quanto a chave privada.
    Passamos como tupla (cert, key) apontando para o mesmo arquivo.
    """
    cert_path = efi_certificate_path()
    if not cert_path:
        raise RuntimeError('EFI_CERTIFICATE_PATH não configurado.')
    if not os.path.exists(cert_path):
        raise RuntimeError(
            f'Certificado PEM da Efí não encontrado em: {cert_path}. '
            'Verifique se o arquivo efi_cert.pem foi enviado ao repositório e se EFI_CERTIFICATE_PATH está correto.'
        )
    # O PEM da Efí contém cert + key no mesmo arquivo
    return (cert_path, cert_path)

def efi_access_token():
    """
    Obtém o access token da API Efí usando autenticação Basic + mTLS.
    CORREÇÕES aplicadas:
    - cert passado como tupla (cert_path, cert_path) — necessário para o requests
    - verify=False em sandbox (a Efí homologação usa certificado auto-assinado)
    - client_id e client_secret limpos de prefixos extras
    """
    cert = efi_get_cert()

    client_id = os.environ.get('EFI_CLIENT_ID', '').strip()
    client_secret = os.environ.get('EFI_CLIENT_SECRET', '').strip()

    # Limpar apenas prefixos acidentais de colagem nas env vars.
    # O ideal é que as variáveis no Railway contenham SOMENTE o valor puro.
    if client_id.startswith('EFI_CLIENT_ID='):
        client_id = client_id.split('=', 1)[1].strip()
    if client_secret.startswith('EFI_CLIENT_SECRET='):
        client_secret = client_secret.split('=', 1)[1].strip()

    if not client_id or not client_secret:
        raise RuntimeError('EFI_CLIENT_ID e EFI_CLIENT_SECRET são obrigatórios.')

    credentials = f'{client_id}:{client_secret}'
    auth = base64.b64encode(credentials.encode()).decode()

    try:
        resp = requests.post(
            f'{efi_base_url()}/oauth/token',
            headers={
                'Authorization': f'Basic {auth}',
                'Content-Type': 'application/json',
            },
            json={'grant_type': 'client_credentials'},
            cert=cert,
            verify=False,  # Necessário em sandbox — certificado auto-assinado da Efí
            timeout=30,
        )
    except requests.exceptions.SSLError as e:
        raise RuntimeError(f'Erro SSL ao conectar na Efí: {e}. Verifique o certificado PEM.')
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f'Erro de conexão com a Efí: {e}. Verifique a URL e a rede.')

    if not resp.ok:
        raise RuntimeError(
            f'Falha na autenticação Efí: {resp.status_code} {resp.text}. '
            'Verifique se EFI_CLIENT_ID e EFI_CLIENT_SECRET estão com os valores puros '
            '(sem prefixos), e se as credenciais são do ambiente correto (Sandbox x Produção).'
        )

    token = resp.json().get('access_token')
    if not token:
        raise RuntimeError(f'Efí não retornou access_token. Resposta: {resp.text}')
    return token

def efi_request(method, path, *, json_body=None):
    """
    Faz uma requisição autenticada à API Efí.
    CORREÇÕES aplicadas:
    - cert passado como tupla (cert_path, cert_path)
    - verify=False em sandbox
    """
    token = efi_access_token()
    cert = efi_get_cert()

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    try:
        resp = requests.request(
            method,
            f'{efi_base_url()}{path}',
            headers=headers,
            json=json_body,
            cert=cert,
            verify=False,  # Necessário em sandbox
            timeout=30,
        )
    except requests.exceptions.SSLError as e:
        raise RuntimeError(f'Erro SSL na requisição Efí {method} {path}: {e}')
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f'Erro de conexão na requisição Efí {method} {path}: {e}')

    if not resp.ok:
        raise RuntimeError(f'Efí {method} {path} -> {resp.status_code}: {resp.text}')

    if resp.text.strip():
        return resp.json()
    return {}

def creditar_saldo_pix(conn, cobranca_row, e2eid=None, origem='verificacao_manual'):
    if cobranca_row['creditado']:
        return False
    valor = float(cobranca_row['valor'])
    conn.execute('UPDATE users SET saldo=saldo+? WHERE username=?', (valor, cobranca_row['usuario']))
    conn.execute(
        "UPDATE pix_cobrancas SET creditado=1, status='CONCLUIDA', e2eid=COALESCE(?, e2eid), webhook_recebido=CASE WHEN ?='webhook' THEN 1 ELSE webhook_recebido END WHERE id=?",
        (e2eid, origem, cobranca_row['id'])
    )
    conn.execute(
        'INSERT INTO movimentacoes (usuario,tipo,valor,descricao,referencia) VALUES(?,?,?,?,?)',
        (cobranca_row['usuario'], 'CREDITO_PIX_EFI', valor, 'Crédito automático via Pix Efí', cobranca_row['txid'])
    )
    return True


# ---- API: EFI PIX ----

@app.route('/api/efi/config')
@require_login
def api_efi_config():
    return jsonify({
        'enabled': efi_is_enabled(),
        'sandbox': efi_is_sandbox(),
        'pix_key_configured': bool(os.environ.get('EFI_PIX_KEY')),
        'webhook_url': os.environ.get('EFI_WEBHOOK_URL', ''),
        'note': efi_webhook_proxy_note()
    })

@app.route('/api/efi/pix/criar', methods=['POST'])
@require_login
def api_efi_pix_criar():
    if not efi_is_enabled():
        return jsonify({'error': 'Efí não configurada no servidor. Defina EFI_CLIENT_ID, EFI_CLIENT_SECRET, EFI_CERTIFICATE_PATH e EFI_PIX_KEY.'}), 503
    data = request.json or {}
    valor = float(data.get('valor', 0))
    if valor <= 0:
        return jsonify({'error': 'Informe um valor válido para o depósito.'}), 400

    body = {
        'calendario': {'expiracao': EFI_PIX_EXPIRACAO},
        'valor': {'original': _money2(valor)},
        'chave': os.environ.get('EFI_PIX_KEY', '').strip(),
        'solicitacaoPagador': f'Depósito NichoPost do usuário {session["user"]}'
    }
    try:
        cob = efi_request('POST', '/v2/cob', json_body=body)
        loc_id = ((cob.get('loc') or {}).get('id')) or cob.get('loc', {}).get('id')
        txid = cob.get('txid')
        if not txid or not loc_id:
            return jsonify({'error': 'A Efí não retornou txid/loc esperados.', 'raw': cob}), 502
        qr = efi_request('GET', f'/v2/loc/{loc_id}/qrcode')
        copia = qr.get('qrcode') or cob.get('pixCopiaECola')
        imagem = qr.get('imagemQrcode')
        link = qr.get('linkVisualizacao', '')
        conn = get_db()
        conn.execute(
            '''INSERT INTO pix_cobrancas (usuario,valor,txid,loc_id,status,pix_copia_e_cola,imagem_qrcode,link_visualizacao)
               VALUES(?,?,?,?,?,?,?,?)''',
            (session['user'], valor, txid, loc_id, cob.get('status', 'ATIVA'), copia, imagem, link)
        )
        conn.commit()
        row_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return jsonify({
            'message': 'PIX Efí gerado com sucesso.',
            'id': row_id,
            'txid': txid,
            'status': cob.get('status', 'ATIVA'),
            'valor': valor,
            'qrcode': copia,
            'imagemQrcode': imagem,
            'linkVisualizacao': link,
            'loc_id': loc_id
        }), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/efi/pix/minhas')
@require_login
def api_efi_pix_minhas():
    conn = get_db()
    rows = conn.execute(
        'SELECT id,usuario,valor,txid,loc_id,status,pix_copia_e_cola,imagem_qrcode,link_visualizacao,e2eid,creditado,data_criacao FROM pix_cobrancas WHERE usuario=? ORDER BY id DESC LIMIT 20',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/efi/pix/verificar/<int:cobranca_id>', methods=['POST'])
@require_login
def api_efi_pix_verificar(cobranca_id):
    if not efi_is_enabled():
        return jsonify({'error': 'Efí não configurada no servidor.'}), 503
    conn = get_db()
    row = conn.execute('SELECT * FROM pix_cobrancas WHERE id=? AND usuario=?', (cobranca_id, session['user'])).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Cobrança Pix não encontrada.'}), 404
    try:
        cob = efi_request('GET', f'/v2/cob/{row["txid"]}')
        status = cob.get('status', row['status'])
        conn.execute('UPDATE pix_cobrancas SET status=? WHERE id=?', (status, cobranca_id))
        creditou = False
        e2eid = None
        if status == 'CONCLUIDA':
            pix_list = cob.get('pix') or []
            if pix_list:
                e2eid = pix_list[0].get('endToEndId')
            creditou = creditar_saldo_pix(conn, row, e2eid=e2eid, origem='verificacao_manual')
        conn.commit()
        saldo = conn.execute('SELECT saldo FROM users WHERE username=?', (session['user'],)).fetchone()['saldo']
        conn.close()
        return jsonify({
            'status': status,
            'creditado_agora': creditou,
            'saldo_atual': round(float(saldo), 4),
            'message': 'Pagamento confirmado e saldo creditado.' if creditou else ('Pagamento já estava creditado.' if row['creditado'] else 'Cobrança ainda não foi concluída.')
        })
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/efi/webhook/pix', methods=['POST'])
def api_efi_webhook_pix():
    payload = request.get_json(silent=True) or {}
    pix_list = payload.get('pix') or []
    conn = get_db()
    creditados = 0
    for pix in pix_list:
        txid = pix.get('txid')
        if not txid:
            continue
        row = conn.execute('SELECT * FROM pix_cobrancas WHERE txid=?', (txid,)).fetchone()
        if not row:
            continue
        conn.execute("UPDATE pix_cobrancas SET status='CONCLUIDA', e2eid=?, webhook_recebido=1 WHERE id=?", (pix.get('endToEndId'), row['id']))
        if creditar_saldo_pix(conn, row, e2eid=pix.get('endToEndId'), origem='webhook'):
            creditados += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'creditados': creditados})

@app.route('/api/movimentacoes')
@require_login
def api_movimentacoes():
    conn = get_db()
    rows = conn.execute(
        'SELECT tipo,valor,descricao,referencia,data_evento FROM movimentacoes WHERE usuario=? ORDER BY id DESC LIMIT 50',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/efi/pix')
@require_login
def admin_efi_pix():
    if session['user'] != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    conn = get_db()
    rows = conn.execute(
        'SELECT id,usuario,valor,txid,status,creditado,e2eid,data_criacao FROM pix_cobrancas ORDER BY id DESC LIMIT 100'
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---- RUN ----

if __name__ == '__main__':
    app.run(
        debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true',
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000))
    )
