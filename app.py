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
from datetime import datetime, timedelta
import math

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'staking_secret_key_2024')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ---- CONFIGURAÇÕES DA PLATAFORMA ----
PLATAFORMA_NOME     = os.environ.get('PLATAFORMA_NOME', 'CryptoYield')
CARTEIRA_DEPOSITO   = os.environ.get('CARTEIRA_DEPOSITO', '0xBa4D5e87e8bcaA85bF29105AB3171b9fDb2eF9dd')
RENDIMENTO_DIARIO   = float(os.environ.get('RENDIMENTO_DIARIO', '0.02'))   # 2% padrão, admin ajusta
APORTE_MINIMO       = float(os.environ.get('APORTE_MINIMO', '50'))
SAQUE_MINIMO        = float(os.environ.get('SAQUE_MINIMO', '10'))
TAXA_SAQUE          = float(os.environ.get('TAXA_SAQUE', '0.03'))           # 3% taxa de saque
LOCK_DIAS           = int(os.environ.get('LOCK_DIAS', '7'))                 # dias bloqueado após aporte

# ---- EFI PIX ----
EFI_PIX_EXPIRACAO   = 3600

# ---- DB ----

def get_db():
    conn = sqlite3.connect('staking.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            password TEXT NOT NULL,
            saldo_disponivel REAL DEFAULT 0,
            saldo_em_staking REAL DEFAULT 0,
            total_ganho REAL DEFAULT 0,
            total_depositado REAL DEFAULT 0,
            total_sacado REAL DEFAULT 0,
            ultimo_collect DATETIME,
            data_cadastro DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_admin INTEGER DEFAULT 0,
            ativo INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS aportes (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            valor REAL NOT NULL,
            txid TEXT,
            metodo TEXT DEFAULT 'usdt',
            status TEXT DEFAULT 'pendente',
            data_aporte DATETIME DEFAULT CURRENT_TIMESTAMP,
            data_aprovacao DATETIME,
            aprovado_por TEXT,
            lock_ate DATETIME
        );
        CREATE TABLE IF NOT EXISTS saques (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            valor REAL NOT NULL,
            carteira TEXT NOT NULL,
            taxa REAL NOT NULL,
            valor_liquido REAL NOT NULL,
            status TEXT DEFAULT 'pendente',
            txid_saida TEXT,
            data_solicitacao DATETIME DEFAULT CURRENT_TIMESTAMP,
            data_processamento DATETIME
        );
        CREATE TABLE IF NOT EXISTS rendimentos (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            valor REAL NOT NULL,
            percentual REAL NOT NULL,
            saldo_base REAL NOT NULL,
            tipo TEXT DEFAULT 'diario',
            data_evento DATETIME DEFAULT CURRENT_TIMESTAMP
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
            data_criacao DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS config_plataforma (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL,
            atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    # Admin padrão
    if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        conn.execute(
            'INSERT INTO users (username,email,password,saldo_disponivel,is_admin) VALUES(?,?,?,?,?)',
            ('admin', 'admin@admin.com', generate_password_hash('87347748'), 0, 1)
        )

    # Config padrão
    defaults = [
        ('rendimento_diario', str(RENDIMENTO_DIARIO)),
        ('aporte_minimo', str(APORTE_MINIMO)),
        ('saque_minimo', str(SAQUE_MINIMO)),
        ('taxa_saque', str(TAXA_SAQUE)),
        ('lock_dias', str(LOCK_DIAS)),
        ('carteira_deposito', CARTEIRA_DEPOSITO),
        ('plataforma_nome', PLATAFORMA_NOME),
        ('total_usuarios', '0'),
        ('total_em_staking', '0'),
    ]
    for chave, valor in defaults:
        conn.execute('INSERT OR IGNORE INTO config_plataforma (chave,valor) VALUES(?,?)', (chave, valor))

    conn.commit()
    conn.close()

init_db()

def get_config(chave, default=None):
    conn = get_db()
    row = conn.execute('SELECT valor FROM config_plataforma WHERE chave=?', (chave,)).fetchone()
    conn.close()
    return row['valor'] if row else default

def set_config(chave, valor):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO config_plataforma (chave,valor,atualizado_em) VALUES(?,?,?)',
                 (chave, str(valor), datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ---- DECORATORS ----

def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            if request.path.startswith('/api/'):
                return jsonify({"error": "Não autenticado"}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({"error": "Não autenticado"}), 401
        conn = get_db()
        row = conn.execute('SELECT is_admin FROM users WHERE username=?', (session['user'],)).fetchone()
        conn.close()
        if not row or not row['is_admin']:
            return jsonify({"error": "Acesso negado"}), 403
        return f(*args, **kwargs)
    return decorated

# ---- HTML PAGES ----

def html_resp(html, code=200):
    r = make_response(html, code)
    r.headers['Content-Type'] = 'text/html; charset=utf-8'
    return r

AUTH_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — CryptoYield</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300;400;600&family=Montserrat:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:'Montserrat',sans-serif;
  background:#080808;
  color:#e8d5a3;
  min-height:100vh;
  display:flex;align-items:center;justify-content:center;
  background-image:
    radial-gradient(ellipse at 20% 50%, rgba(212,175,55,0.04) 0%, transparent 60%),
    radial-gradient(ellipse at 80% 20%, rgba(212,175,55,0.03) 0%, transparent 50%);
}
.card{
  background:linear-gradient(135deg,#0f0f0f 0%,#111008 100%);
  border:1px solid rgba(212,175,55,0.2);
  border-radius:4px;
  padding:48px 40px;
  width:380px;
  box-shadow:0 0 60px rgba(212,175,55,0.05), inset 0 1px 0 rgba(212,175,55,0.1);
  position:relative;
}
.card::before{
  content:'';
  position:absolute;top:0;left:50%;transform:translateX(-50%);
  width:60px;height:2px;
  background:linear-gradient(90deg,transparent,#d4af37,transparent);
}
.logo{
  font-family:'Cormorant Garamond',serif;
  font-size:28px;font-weight:300;
  color:#d4af37;
  letter-spacing:4px;
  text-align:center;
  margin-bottom:8px;
}
.logo-sub{font-size:10px;letter-spacing:6px;color:#7a6a3a;text-align:center;margin-bottom:36px;text-transform:uppercase}
h2{font-size:12px;letter-spacing:3px;color:#7a6a3a;text-transform:uppercase;margin-bottom:28px;text-align:center}
.field{margin-bottom:16px}
label{display:block;font-size:10px;letter-spacing:2px;color:#7a6a3a;text-transform:uppercase;margin-bottom:6px}
input{
  width:100%;padding:12px 14px;
  background:rgba(212,175,55,0.04);
  border:1px solid rgba(212,175,55,0.15);
  border-radius:2px;
  color:#e8d5a3;font-family:'Montserrat',sans-serif;font-size:13px;
  outline:none;transition:border-color .2s;
}
input:focus{border-color:rgba(212,175,55,0.4)}
button{
  width:100%;padding:14px;margin-top:8px;
  background:linear-gradient(135deg,#c9a227,#d4af37);
  color:#080808;font-family:'Montserrat',sans-serif;
  font-size:11px;font-weight:600;letter-spacing:3px;text-transform:uppercase;
  border:none;border-radius:2px;cursor:pointer;
  transition:opacity .2s;
}
button:hover{opacity:.85}
.err{color:#e05252;font-size:11px;margin-bottom:16px;letter-spacing:1px;text-align:center}
.link{text-align:center;margin-top:20px;font-size:11px;color:#7a6a3a}
.link a{color:#d4af37;text-decoration:none}
.divider{height:1px;background:rgba(212,175,55,0.1);margin:20px 0}
</style>
</head>
<body>
<div class="card">
  <div class="logo">CRYPTOYIELD</div>
  <div class="logo-sub">Digital Asset Staking</div>
  <h2>__SUBTITLE__</h2>
  __ERROR__
  __FORM__
</div>
</body>
</html>"""

LOGIN_FORM = """
<form method="post">
<div class="field"><label>Usuário</label><input name="username" placeholder="seu_usuario" required autocomplete="username"></div>
<div class="field"><label>Senha</label><input name="password" type="password" placeholder="••••••••" required autocomplete="current-password"></div>
<button type="submit">Acessar plataforma</button>
</form>
<div class="link">Novo investidor? <a href="/register">Criar conta</a></div>
"""

REGISTER_FORM = """
<form method="post">
<div class="field"><label>Usuário</label><input name="username" placeholder="seu_usuario" required></div>
<div class="field"><label>E-mail</label><input name="email" type="email" placeholder="email@exemplo.com" required></div>
<div class="field"><label>Senha</label><input name="password" type="password" placeholder="Mínimo 6 caracteres" required></div>
<button type="submit">Criar conta gratuita</button>
</form>
<div class="link">Já tem conta? <a href="/login">Entrar</a></div>
"""

def render_auth(title, subtitle, form, error=''):
    err_html = f'<div class="err">{error}</div>' if error else ''
    return AUTH_HTML\
        .replace('__TITLE__', title)\
        .replace('__SUBTITLE__', subtitle)\
        .replace('__ERROR__', err_html)\
        .replace('__FORM__', form)

# ---- ROUTES: AUTH ----

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username=? AND ativo=1', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user'] = user['username']
            session['is_admin'] = bool(user['is_admin'])
            return redirect(url_for('dashboard'))
        return html_resp(render_auth('Login', 'Acesso à conta', LOGIN_FORM, 'Credenciais inválidas'))
    return html_resp(render_auth('Login', 'Acesso à conta', LOGIN_FORM))

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if len(password) < 6:
            return html_resp(render_auth('Registro', 'Criar conta', REGISTER_FORM, 'Senha mínima: 6 caracteres'))
        conn = get_db()
        try:
            conn.execute('INSERT INTO users (username,email,password) VALUES(?,?,?)',
                         (username, email, generate_password_hash(password)))
            conn.commit()
            session['user'] = username
            session['is_admin'] = False
            return redirect(url_for('dashboard'))
        except sqlite3.IntegrityError:
            return html_resp(render_auth('Registro', 'Criar conta', REGISTER_FORM, 'Usuário já existe'))
        finally:
            conn.close()
    return html_resp(render_auth('Registro', 'Criar conta', REGISTER_FORM))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/')
def root():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login_page'))

@app.route('/dashboard')
@require_login
def dashboard():
    return send_file('staking_dashboard.html')

# ---- API: ME ----

@app.route('/api/me')
@require_login
def api_me():
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE username=?', (session['user'],)).fetchone()

    # Calcular rendimento disponível para coletar
    rendimento_pendente = 0.0
    if user['saldo_em_staking'] > 0:
        ultimo = user['ultimo_collect']
        if ultimo:
            try:
                dt_ultimo = datetime.fromisoformat(ultimo)
            except:
                dt_ultimo = datetime.now() - timedelta(days=1)
        else:
            # Verificar data do primeiro aporte aprovado
            primeiro_aporte = conn.execute(
                "SELECT data_aprovacao FROM aportes WHERE usuario=? AND status='aprovado' ORDER BY data_aprovacao ASC LIMIT 1",
                (session['user'],)
            ).fetchone()
            if primeiro_aporte and primeiro_aporte['data_aprovacao']:
                try:
                    dt_ultimo = datetime.fromisoformat(primeiro_aporte['data_aprovacao'])
                except:
                    dt_ultimo = datetime.now() - timedelta(days=1)
            else:
                dt_ultimo = datetime.now()

        agora = datetime.now()
        diff = agora - dt_ultimo
        horas = diff.total_seconds() / 3600
        dias_fracionados = horas / 24
        taxa_diaria = float(get_config('rendimento_diario', '0.02'))
        rendimento_pendente = round(user['saldo_em_staking'] * taxa_diaria * dias_fracionados, 6)
        rendimento_pendente = max(0, rendimento_pendente)

    conn.close()

    taxa_diaria = float(get_config('rendimento_diario', '0.02'))
    aporte_min = float(get_config('aporte_minimo', '50'))
    saque_min = float(get_config('saque_minimo', '10'))
    taxa_saque = float(get_config('taxa_saque', '0.03'))
    carteira = get_config('carteira_deposito', CARTEIRA_DEPOSITO)

    return jsonify({
        'user': user['username'],
        'email': user['email'],
        'is_admin': bool(user['is_admin']),
        'saldo_disponivel': round(float(user['saldo_disponivel']), 6),
        'saldo_em_staking': round(float(user['saldo_em_staking']), 6),
        'total_ganho': round(float(user['total_ganho']), 6),
        'total_depositado': round(float(user['total_depositado']), 6),
        'total_sacado': round(float(user['total_sacado']), 6),
        'rendimento_pendente': round(rendimento_pendente, 6),
        'taxa_diaria': taxa_diaria,
        'taxa_diaria_pct': round(taxa_diaria * 100, 2),
        'aporte_minimo': aporte_min,
        'saque_minimo': saque_min,
        'taxa_saque_pct': round(taxa_saque * 100, 2),
        'carteira_deposito': carteira,
        'lock_dias': int(get_config('lock_dias', '7')),
    })

# ---- API: COLETAR RENDIMENTO ----

@app.route('/api/coletar', methods=['POST'])
@require_login
def api_coletar():
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE username=?', (session['user'],)).fetchone()

    if user['saldo_em_staking'] <= 0:
        conn.close()
        return jsonify({'error': 'Você não possui saldo em staking.'}), 400

    ultimo = user['ultimo_collect']
    if ultimo:
        try:
            dt_ultimo = datetime.fromisoformat(ultimo)
        except:
            dt_ultimo = datetime.now() - timedelta(days=1)
    else:
        primeiro_aporte = conn.execute(
            "SELECT data_aprovacao FROM aportes WHERE usuario=? AND status='aprovado' ORDER BY data_aprovacao ASC LIMIT 1",
            (session['user'],)
        ).fetchone()
        if primeiro_aporte and primeiro_aporte['data_aprovacao']:
            try:
                dt_ultimo = datetime.fromisoformat(primeiro_aporte['data_aprovacao'])
            except:
                dt_ultimo = datetime.now() - timedelta(days=1)
        else:
            conn.close()
            return jsonify({'error': 'Nenhum aporte aprovado encontrado.'}), 400

    agora = datetime.now()
    diff = agora - dt_ultimo
    horas = diff.total_seconds() / 3600

    if horas < 1:
        mins = int((1 - horas) * 60)
        conn.close()
        return jsonify({'error': f'Aguarde {mins} minuto(s) para coletar novamente.'}), 400

    dias_fracionados = horas / 24
    taxa_diaria = float(get_config('rendimento_diario', '0.02'))
    rendimento = round(user['saldo_em_staking'] * taxa_diaria * dias_fracionados, 6)

    if rendimento <= 0:
        conn.close()
        return jsonify({'error': 'Rendimento ainda não disponível.'}), 400

    conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel+?, total_ganho=total_ganho+?, ultimo_collect=? WHERE username=?',
                 (rendimento, rendimento, agora.isoformat(), session['user']))
    conn.execute('INSERT INTO rendimentos (usuario,valor,percentual,saldo_base) VALUES(?,?,?,?)',
                 (session['user'], rendimento, taxa_diaria * dias_fracionados, user['saldo_em_staking']))
    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
                 (session['user'], 'RENDIMENTO', rendimento, f'Rendimento coletado — {round(taxa_diaria*100,2)}%/dia × {round(dias_fracionados,2)} dias'))
    conn.commit()
    conn.close()

    return jsonify({
        'message': f'Rendimento de {rendimento:.6f} USDT coletado com sucesso!',
        'valor': rendimento,
        'horas': round(horas, 2),
    })

# ---- API: SOLICITAR APORTE ----

@app.route('/api/aporte', methods=['POST'])
@require_login
def api_aporte():
    data = request.json or {}
    valor = float(data.get('valor', 0))
    txid = data.get('txid', '').strip()
    metodo = data.get('metodo', 'usdt')

    aporte_min = float(get_config('aporte_minimo', '50'))
    if valor < aporte_min:
        return jsonify({'error': f'Aporte mínimo: {aporte_min} USDT'}), 400
    if not txid:
        return jsonify({'error': 'Informe o TXID da transação para comprovação.'}), 400

    conn = get_db()
    # Verificar TXID duplicado
    existe = conn.execute('SELECT id FROM aportes WHERE txid=?', (txid,)).fetchone()
    if existe:
        conn.close()
        return jsonify({'error': 'TXID já registrado.'}), 409

    conn.execute('INSERT INTO aportes (usuario,valor,txid,metodo,status) VALUES(?,?,?,?,?)',
                 (session['user'], valor, txid, metodo, 'pendente'))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Aporte registrado! Aguarde a confirmação do administrador.', 'valor': valor}), 201

# ---- API: SOLICITAR SAQUE ----

@app.route('/api/saque', methods=['POST'])
@require_login
def api_saque():
    data = request.json or {}
    valor = float(data.get('valor', 0))
    carteira = data.get('carteira', '').strip()

    saque_min = float(get_config('saque_minimo', '10'))
    taxa_pct = float(get_config('taxa_saque', '0.03'))

    if valor < saque_min:
        return jsonify({'error': f'Saque mínimo: {saque_min} USDT'}), 400
    if not carteira.startswith('0x') or len(carteira) != 42:
        return jsonify({'error': 'Carteira ERC-20 inválida. Use 0x... com 42 caracteres.'}), 400

    conn = get_db()
    user = conn.execute('SELECT saldo_disponivel FROM users WHERE username=?', (session['user'],)).fetchone()
    if float(user['saldo_disponivel']) < valor:
        conn.close()
        return jsonify({'error': f'Saldo insuficiente. Disponível: {round(float(user["saldo_disponivel"]),4)} USDT'}), 400

    taxa = round(valor * taxa_pct, 6)
    liquido = round(valor - taxa, 6)

    conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel-? WHERE username=?', (valor, session['user']))
    conn.execute('INSERT INTO saques (usuario,valor,carteira,taxa,valor_liquido,status) VALUES(?,?,?,?,?,?)',
                 (session['user'], valor, carteira, taxa, liquido, 'pendente'))
    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
                 (session['user'], 'SAQUE', valor, f'Saque para {carteira[:8]}...{carteira[-4:]}'))
    conn.commit()
    conn.close()

    return jsonify({
        'message': f'Saque de {liquido:.4f} USDT solicitado!',
        'valor_bruto': valor,
        'taxa': taxa,
        'valor_liquido': liquido,
        'status': 'pendente',
    }), 201

# ---- API: HISTÓRICO ----

@app.route('/api/historico')
@require_login
def api_historico():
    conn = get_db()
    movs = conn.execute(
        'SELECT tipo,valor,descricao,data_evento FROM movimentacoes WHERE usuario=? ORDER BY id DESC LIMIT 50',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in movs])

@app.route('/api/meus_aportes')
@require_login
def api_meus_aportes():
    conn = get_db()
    rows = conn.execute(
        'SELECT valor,txid,metodo,status,data_aporte FROM aportes WHERE usuario=? ORDER BY id DESC LIMIT 20',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/meus_saques')
@require_login
def api_meus_saques():
    conn = get_db()
    rows = conn.execute(
        'SELECT valor,carteira,taxa,valor_liquido,status,data_solicitacao FROM saques WHERE usuario=? ORDER BY id DESC LIMIT 20',
        (session['user'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ---- API: STATS PÚBLICAS ----

@app.route('/api/stats')
def api_stats():
    conn = get_db()
    total_usuarios = conn.execute('SELECT COUNT(*) FROM users WHERE is_admin=0').fetchone()[0]
    total_staking = conn.execute('SELECT COALESCE(SUM(saldo_em_staking),0) FROM users').fetchone()[0]
    total_pago = conn.execute('SELECT COALESCE(SUM(total_ganho),0) FROM users').fetchone()[0]
    conn.close()
    return jsonify({
        'total_usuarios': total_usuarios,
        'total_em_staking': round(float(total_staking), 2),
        'total_rendimentos_pagos': round(float(total_pago), 2),
        'taxa_diaria_pct': round(float(get_config('rendimento_diario', '0.02')) * 100, 2),
    })

# ---- API: ADMIN ----

@app.route('/api/admin/dashboard')
@require_admin
def admin_dashboard():
    conn = get_db()
    total_usuarios = conn.execute('SELECT COUNT(*) FROM users WHERE is_admin=0').fetchone()[0]
    total_staking = conn.execute('SELECT COALESCE(SUM(saldo_em_staking),0) FROM users').fetchone()[0]
    total_disponivel = conn.execute('SELECT COALESCE(SUM(saldo_disponivel),0) FROM users').fetchone()[0]
    aportes_pendentes = conn.execute("SELECT COUNT(*) FROM aportes WHERE status='pendente'").fetchone()[0]
    saques_pendentes = conn.execute("SELECT COUNT(*) FROM saques WHERE status='pendente'").fetchone()[0]
    total_pago = conn.execute('SELECT COALESCE(SUM(total_ganho),0) FROM users').fetchone()[0]
    conn.close()
    return jsonify({
        'total_usuarios': total_usuarios,
        'total_em_staking': round(float(total_staking), 2),
        'total_disponivel': round(float(total_disponivel), 2),
        'aportes_pendentes': aportes_pendentes,
        'saques_pendentes': saques_pendentes,
        'total_rendimentos_pagos': round(float(total_pago), 2),
        'taxa_diaria_pct': round(float(get_config('rendimento_diario', '0.02')) * 100, 2),
    })

@app.route('/api/admin/aportes_pendentes')
@require_admin
def admin_aportes_pendentes():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,usuario,valor,txid,metodo,data_aporte FROM aportes WHERE status='pendente' ORDER BY data_aporte DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/aprovar_aporte/<int:aporte_id>', methods=['POST'])
@require_admin
def admin_aprovar_aporte(aporte_id):
    conn = get_db()
    aporte = conn.execute("SELECT * FROM aportes WHERE id=? AND status='pendente'", (aporte_id,)).fetchone()
    if not aporte:
        conn.close()
        return jsonify({'error': 'Aporte não encontrado ou já processado'}), 404

    agora = datetime.now()
    lock_dias = int(get_config('lock_dias', '7'))
    lock_ate = agora + timedelta(days=lock_dias)

    conn.execute("UPDATE aportes SET status='aprovado', data_aprovacao=?, aprovado_por=?, lock_ate=? WHERE id=?",
                 (agora.isoformat(), session['user'], lock_ate.isoformat(), aporte_id))
    conn.execute('UPDATE users SET saldo_em_staking=saldo_em_staking+?, total_depositado=total_depositado+? WHERE username=?',
                 (aporte['valor'], aporte['valor'], aporte['usuario']))
    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao,referencia) VALUES(?,?,?,?,?)',
                 (aporte['usuario'], 'APORTE', aporte['valor'], 'Aporte aprovado e ativado em staking', aporte['txid']))
    conn.commit()
    conn.close()
    return jsonify({'message': f'Aporte de {aporte["valor"]} USDT aprovado para {aporte["usuario"]}!'})

@app.route('/api/admin/rejeitar_aporte/<int:aporte_id>', methods=['POST'])
@require_admin
def admin_rejeitar_aporte(aporte_id):
    conn = get_db()
    aporte = conn.execute("SELECT * FROM aportes WHERE id=? AND status='pendente'", (aporte_id,)).fetchone()
    if not aporte:
        conn.close()
        return jsonify({'error': 'Aporte não encontrado'}), 404
    conn.execute("UPDATE aportes SET status='rejeitado' WHERE id=?", (aporte_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Aporte rejeitado.'})

@app.route('/api/admin/saques_pendentes')
@require_admin
def admin_saques_pendentes():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,usuario,valor,carteira,taxa,valor_liquido,data_solicitacao FROM saques WHERE status='pendente' ORDER BY data_solicitacao DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/aprovar_saque/<int:saque_id>', methods=['POST'])
@require_admin
def admin_aprovar_saque(saque_id):
    data = request.json or {}
    txid_saida = data.get('txid', '')
    conn = get_db()
    saque = conn.execute("SELECT * FROM saques WHERE id=? AND status='pendente'", (saque_id,)).fetchone()
    if not saque:
        conn.close()
        return jsonify({'error': 'Saque não encontrado'}), 404
    conn.execute("UPDATE saques SET status='aprovado', txid_saida=?, data_processamento=? WHERE id=?",
                 (txid_saida, datetime.now().isoformat(), saque_id))
    conn.execute('UPDATE users SET total_sacado=total_sacado+? WHERE username=?',
                 (saque['valor_liquido'], saque['usuario']))
    conn.commit()
    conn.close()
    return jsonify({'message': f'Saque de {saque["valor_liquido"]} USDT aprovado para {saque["usuario"]}!'})

@app.route('/api/admin/rejeitar_saque/<int:saque_id>', methods=['POST'])
@require_admin
def admin_rejeitar_saque(saque_id):
    conn = get_db()
    saque = conn.execute("SELECT * FROM saques WHERE id=? AND status='pendente'", (saque_id,)).fetchone()
    if not saque:
        conn.close()
        return jsonify({'error': 'Saque não encontrado'}), 404
    conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel+? WHERE username=?',
                 (saque['valor'], saque['usuario']))
    conn.execute("UPDATE saques SET status='rejeitado' WHERE id=?", (saque_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': f'Saque rejeitado. {saque["valor"]} USDT devolvidos.'})

@app.route('/api/admin/usuarios')
@require_admin
def admin_usuarios():
    conn = get_db()
    rows = conn.execute(
        'SELECT username,email,saldo_disponivel,saldo_em_staking,total_ganho,total_depositado,total_sacado,data_cadastro,ativo FROM users WHERE is_admin=0 ORDER BY data_cadastro DESC'
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/config', methods=['GET', 'POST'])
@require_admin
def admin_config():
    if request.method == 'POST':
        data = request.json or {}
        campos_permitidos = ['rendimento_diario', 'aporte_minimo', 'saque_minimo', 'taxa_saque', 'lock_dias', 'carteira_deposito']
        for campo in campos_permitidos:
            if campo in data:
                set_config(campo, data[campo])
        return jsonify({'message': 'Configurações salvas!'})

    chaves = ['rendimento_diario', 'aporte_minimo', 'saque_minimo', 'taxa_saque', 'lock_dias', 'carteira_deposito', 'plataforma_nome']
    result = {}
    for c in chaves:
        result[c] = get_config(c)
    return jsonify(result)

@app.route('/api/admin/creditar_manual', methods=['POST'])
@require_admin
def admin_creditar_manual():
    data = request.json or {}
    usuario = data.get('usuario', '').strip()
    valor = float(data.get('valor', 0))
    tipo = data.get('tipo', 'disponivel')  # 'disponivel' ou 'staking'

    if valor <= 0:
        return jsonify({'error': 'Valor inválido'}), 400

    conn = get_db()
    user = conn.execute('SELECT username FROM users WHERE username=?', (usuario,)).fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'Usuário não encontrado'}), 404

    if tipo == 'staking':
        conn.execute('UPDATE users SET saldo_em_staking=saldo_em_staking+? WHERE username=?', (valor, usuario))
    else:
        conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel+? WHERE username=?', (valor, usuario))

    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
                 (usuario, 'CREDITO_MANUAL', valor, f'Crédito manual pelo admin — {tipo}'))
    conn.commit()
    conn.close()
    return jsonify({'message': f'{valor} USDT creditado em {tipo} para {usuario}!'})

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

def efi_get_cert():
    cert_path = efi_certificate_path()
    if not cert_path or not os.path.exists(cert_path):
        raise RuntimeError(f'Certificado PEM não encontrado em: {cert_path}')
    return (cert_path, cert_path)

def efi_access_token():
    cert = efi_get_cert()
    client_id = os.environ.get('EFI_CLIENT_ID', '').strip()
    client_secret = os.environ.get('EFI_CLIENT_SECRET', '').strip()
    if client_id.startswith('EFI_CLIENT_ID='):
        client_id = client_id.split('=', 1)[1].strip()
    if client_secret.startswith('Client_Secret_'):
        client_secret = client_secret.replace('Client_Secret_', '', 1).strip()
    auth = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    resp = requests.post(
        f'{efi_base_url()}/oauth/token',
        headers={'Authorization': f'Basic {auth}', 'Content-Type': 'application/json'},
        json={'grant_type': 'client_credentials'},
        cert=cert, verify=False, timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f'Falha na autenticação Efí: {resp.status_code} {resp.text}')
    return resp.json().get('access_token')

def efi_request(method, path, *, json_body=None):
    token = efi_access_token()
    cert = efi_get_cert()
    resp = requests.request(
        method, f'{efi_base_url()}{path}',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=json_body, cert=cert, verify=False, timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f'Efí {method} {path} -> {resp.status_code}: {resp.text}')
    return resp.json() if resp.text.strip() else {}

# ---- API: EFI PIX ----

@app.route('/api/efi/pix/criar', methods=['POST'])
@require_login
def api_efi_pix_criar():
    if not efi_is_enabled():
        return jsonify({'error': 'PIX não configurado.'}), 503
    data = request.json or {}
    valor = float(data.get('valor', 0))
    aporte_min = float(get_config('aporte_minimo', '50'))
    if valor < aporte_min:
        return jsonify({'error': f'Valor mínimo: {aporte_min}'}), 400
    try:
        cob = efi_request('POST', '/v2/cob', json_body={
            'calendario': {'expiracao': EFI_PIX_EXPIRACAO},
            'valor': {'original': _money2(valor)},
            'chave': os.environ.get('EFI_PIX_KEY', '').strip(),
            'solicitacaoPagador': f'Aporte CryptoYield — {session["user"]}'
        })
        loc_id = (cob.get('loc') or {}).get('id')
        txid = cob.get('txid')
        if not txid or not loc_id:
            return jsonify({'error': 'Resposta inválida da Efí', 'raw': cob}), 502
        qr = efi_request('GET', f'/v2/loc/{loc_id}/qrcode')
        conn = get_db()
        conn.execute(
            'INSERT INTO pix_cobrancas (usuario,valor,txid,loc_id,status,pix_copia_e_cola,imagem_qrcode,link_visualizacao) VALUES(?,?,?,?,?,?,?,?)',
            (session['user'], valor, txid, loc_id, cob.get('status', 'ATIVA'),
             qr.get('qrcode'), qr.get('imagemQrcode'), qr.get('linkVisualizacao', ''))
        )
        conn.commit()
        conn.close()
        return jsonify({
            'txid': txid, 'qrcode': qr.get('qrcode'),
            'imagemQrcode': qr.get('imagemQrcode'),
            'valor': valor, 'status': 'ATIVA'
        }), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/efi/webhook/pix', methods=['POST'])
def api_efi_webhook_pix():
    payload = request.get_json(silent=True) or {}
    pix_list = payload.get('pix') or []
    conn = get_db()
    for pix in pix_list:
        txid = pix.get('txid')
        if not txid:
            continue
        row = conn.execute('SELECT * FROM pix_cobrancas WHERE txid=? AND creditado=0', (txid,)).fetchone()
        if not row:
            continue
        conn.execute("UPDATE pix_cobrancas SET status='CONCLUIDA', e2eid=?, webhook_recebido=1, creditado=1 WHERE id=?",
                     (pix.get('endToEndId'), row['id']))
        # Criar aporte automaticamente
        conn.execute('INSERT INTO aportes (usuario,valor,txid,metodo,status) VALUES(?,?,?,?,?)',
                     (row['usuario'], row['valor'], txid, 'pix', 'pendente'))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ---- RUN ----

if __name__ == '__main__':
    app.run(
        debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true',
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000))
    )
