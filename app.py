from flask import Flask, jsonify, request, make_response, session, redirect, url_for, send_file
from functools import wraps
import sqlite3
import os
import uuid
import time
import re
from decimal import Decimal, ROUND_HALF_UP
import urllib3
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from collections import defaultdict
import threading

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

secret = os.environ.get('SECRET_KEY', '')
if len(secret) < 32:
    import secrets as _s
    secret = _s.token_hex(32)
    print(f'[VOLTRIX] AVISO: defina SECRET_KEY no Railway com pelo menos 32 chars!')
app.secret_key = secret
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

PLATAFORMA_NOME   = 'VOLTRIX'
CARTEIRA_DEPOSITO = os.environ.get('CARTEIRA_DEPOSITO', '0xBa4D5e87e8bcaA85bF29105AB3171b9fDb2eF9dd')
APORTE_MINIMO     = float(os.environ.get('APORTE_MINIMO', '10'))
SAQUE_MINIMO      = float(os.environ.get('SAQUE_MINIMO', '10'))
TAXA_SAQUE        = float(os.environ.get('TAXA_SAQUE', '0.05'))
COMISSAO_N1       = 0.20
COMISSAO_N2       = 0.10

# ── RATE LIMITING ──
_rate_store  = defaultdict(list)
_blocked_ips = {}
_rate_lock   = threading.Lock()

def get_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    return fwd.split(',')[0].strip() if fwd else (request.remote_addr or '0.0.0.0')

def is_blocked(ip):
    with _rate_lock:
        if ip in _blocked_ips:
            if time.time() < _blocked_ips[ip]: return True
            del _blocked_ips[ip]
    return False

def block_ip(ip, secs=900):
    with _rate_lock:
        _blocked_ips[ip] = time.time() + secs
        _rate_store.pop(ip, None)

def rate_limit(max_calls=10, window=300, block_secs=900):
    def deco(f):
        @wraps(f)
        def wrap(*a, **kw):
            if request.method == 'GET': return f(*a, **kw)
            ip = get_ip()
            if is_blocked(ip):
                rem = int((_blocked_ips.get(ip, 0) - time.time()) / 60) + 1
                msg = f'Muitas tentativas. Aguarde {rem} minuto(s).'
                if request.path.startswith('/api/'): return jsonify({'error': msg}), 429
                return html_resp(f'<p style="color:red;font-family:sans-serif;padding:20px">{msg}</p>'), 429
            now = time.time()
            with _rate_lock:
                calls = [t for t in _rate_store[ip] if now - t < window]
                calls.append(now)
                _rate_store[ip] = calls
                n = len(calls)
            if n > max_calls * 3:
                block_ip(ip, 3600)
                msg = 'IP bloqueado por 1h.'
                if request.path.startswith('/api/'): return jsonify({'error': msg}), 429
                return html_resp(f'<p style="color:red;font-family:sans-serif;padding:20px">{msg}</p>'), 429
            if n > max_calls:
                msg = f'Muitas tentativas. Aguarde {window//60} min.'
                if request.path.startswith('/api/'): return jsonify({'error': msg}), 429
                return html_resp(f'<p style="color:red;font-family:sans-serif;padding:20px">{msg}</p>'), 429
            return f(*a, **kw)
        return wrap
    return deco

@app.after_request
def sec_headers(resp):
    resp.headers['X-Frame-Options']        = 'DENY'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-XSS-Protection']       = '1; mode=block'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    resp.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none';"
    )
    resp.headers.pop('Server', None)
    return resp

def san(s, n=100):
    if not isinstance(s, str): return ''
    return re.sub(r'[<>\'";&|`$\\]', '', s.strip()[:n])

def valid_user(u): return bool(re.match(r'^[a-zA-Z0-9_]{3,30}$', u))
def valid_email(e): return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', e))
def valid_eth(a): return bool(re.match(r'^0x[0-9a-fA-F]{40}$', a))
def valid_cpf(c): return bool(re.match(r'^\d{11}$', re.sub(r'\D','',c)))

def log_sec(ev, user=None, det=None):
    try:
        conn = get_db()
        conn.execute('INSERT INTO logs_seguranca (ip,evento,usuario,detalhes) VALUES(?,?,?,?)',
                     (get_ip(), ev, user, det))
        conn.commit(); conn.close()
    except: pass

def get_db():
    conn = sqlite3.connect('voltrix.db', timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            nome_completo TEXT,
            cpf TEXT,
            email TEXT,
            password TEXT NOT NULL,
            carteira TEXT,
            saldo_disponivel REAL DEFAULT 0,
            total_depositado REAL DEFAULT 0,
            total_sacado REAL DEFAULT 0,
            total_ganho REAL DEFAULT 0,
            total_comissao REAL DEFAULT 0,
            referido_por TEXT,
            codigo_indicacao TEXT UNIQUE,
            data_cadastro DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_admin INTEGER DEFAULT 0,
            ativo INTEGER DEFAULT 1,
            tentativas_login INTEGER DEFAULT 0,
            bloqueado_ate DATETIME
        );
        CREATE TABLE IF NOT EXISTS projetos (
            id INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            descricao TEXT,
            categoria TEXT DEFAULT 'crypto',
            meta_usdt REAL NOT NULL,
            captado_usdt REAL DEFAULT 0,
            rendimento_diario REAL NOT NULL,
            status TEXT DEFAULT 'pendente',
            imagem_url TEXT,
            data_criacao DATETIME DEFAULT CURRENT_TIMESTAMP,
            data_aprovacao DATETIME,
            data_encerramento DATETIME,
            criado_por TEXT
        );
        CREATE TABLE IF NOT EXISTS aportes_projeto (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            projeto_id INTEGER NOT NULL,
            valor REAL NOT NULL,
            txid TEXT,
            status TEXT DEFAULT 'pendente',
            data_aporte DATETIME DEFAULT CURRENT_TIMESTAMP,
            data_aprovacao DATETIME,
            ultimo_collect DATETIME,
            lucro_coletado REAL DEFAULT 0,
            data_conclusao DATETIME,
            aprovado_por TEXT,
            FOREIGN KEY(projeto_id) REFERENCES projetos(id)
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
            projeto_id INTEGER,
            valor REAL NOT NULL,
            percentual REAL NOT NULL,
            saldo_base REAL NOT NULL,
            data_evento DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS comissoes (
            id INTEGER PRIMARY KEY,
            beneficiario TEXT NOT NULL,
            originador TEXT NOT NULL,
            nivel INTEGER NOT NULL,
            valor REAL NOT NULL,
            data_evento DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS indicacoes (
            id INTEGER PRIMARY KEY,
            indicador TEXT NOT NULL,
            indicado TEXT NOT NULL,
            nivel INTEGER DEFAULT 1,
            data_indicacao DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS movimentacoes (
            id INTEGER PRIMARY KEY,
            usuario TEXT NOT NULL,
            tipo TEXT NOT NULL,
            valor REAL NOT NULL,
            descricao TEXT,
            data_evento DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS config_plataforma (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS logs_seguranca (
            id INTEGER PRIMARY KEY,
            ip TEXT, evento TEXT, usuario TEXT, detalhes TEXT,
            data_evento DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        codigo = str(uuid.uuid4())[:8].upper()
        conn.execute(
            'INSERT INTO users (username,nome_completo,email,password,is_admin,codigo_indicacao) VALUES(?,?,?,?,?,?)',
            ('admin','Administrador','admin@voltrix.io', generate_password_hash('87347748'), 1, codigo)
        )

    for k, v in [
        ('aporte_minimo', str(APORTE_MINIMO)),
        ('saque_minimo', str(SAQUE_MINIMO)),
        ('taxa_saque', str(TAXA_SAQUE)),
        ('carteira_deposito', CARTEIRA_DEPOSITO),
        ('comissao_n1', str(COMISSAO_N1)),
        ('comissao_n2', str(COMISSAO_N2)),
    ]:
        conn.execute('INSERT OR IGNORE INTO config_plataforma (chave,valor) VALUES(?,?)', (k, v))

    # Migração automática: prazo até dobrar o aporte.
    # Cada aporte para de render quando o lucro coletado alcançar 100% do valor aportado.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(aportes_projeto)").fetchall()]
    if 'lucro_coletado' not in cols:
        conn.execute('ALTER TABLE aportes_projeto ADD COLUMN lucro_coletado REAL DEFAULT 0')
    if 'data_conclusao' not in cols:
        conn.execute('ALTER TABLE aportes_projeto ADD COLUMN data_conclusao DATETIME')

    conn.commit(); conn.close()

init_db()

def gcfg(k, d=None):
    conn = get_db()
    r = conn.execute('SELECT valor FROM config_plataforma WHERE chave=?', (k,)).fetchone()
    conn.close()
    return r['valor'] if r else d

def scfg(k, v):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO config_plataforma (chave,valor) VALUES(?,?)', (k, str(v)))
    conn.commit(); conn.close()

# ── REGRA DE PRAZO: ATÉ DOBRAR ──
def calc_rendimento_pendente_aporte(ap, agora_dt=None):
    """
    Calcula rendimento pendente com trava de dobra.
    Regra: cada aporte pode gerar no máximo 100% de lucro.
    Ex.: aporte de 10 USDT rende até 10 USDT de lucro. Total final = 20 USDT.
    """
    agora_dt = agora_dt or datetime.now()
    valor = float(ap['valor'] or 0)
    taxa_dia = float(ap['rendimento_diario'] or 0)
    try:
        lucro_coletado = float(ap['lucro_coletado'] or 0)
    except Exception:
        lucro_coletado = 0.0

    limite_lucro = valor
    restante = max(0.0, limite_lucro - lucro_coletado)
    if valor <= 0 or taxa_dia <= 0 or restante <= 0:
        return 0.0

    ultimo = ap['ultimo_collect'] or ap['data_aprovacao']
    if not ultimo:
        return 0.0
    try:
        dt = datetime.fromisoformat(ultimo)
    except Exception:
        return 0.0

    horas = max(0.0, (agora_dt - dt).total_seconds() / 3600)
    bruto = valor * taxa_dia * (horas / 24)
    return round(min(max(0.0, bruto), restante), 6)

def require_login(f):
    @wraps(f)
    def d(*a, **kw):
        if 'user' not in session:
            if request.path.startswith('/api/'): return jsonify({'error': 'Não autenticado'}), 401
            return redirect(url_for('login_page'))
        return f(*a, **kw)
    return d

def require_admin(f):
    @wraps(f)
    def d(*a, **kw):
        if 'user' not in session: return jsonify({'error': 'Não autenticado'}), 401
        if not session.get('is_admin'):
            log_sec('ACESSO_ADMIN_NEGADO', session.get('user'))
            return jsonify({'error': 'Acesso negado'}), 403
        return f(*a, **kw)
    return d

@app.before_request
def check_session():
    if 'user' in session:
        stored_ip = session.get('_ip')
        current_ip = get_ip()
        if stored_ip and stored_ip != current_ip:
            log_sec('SESSION_IP_MISMATCH', session.get('user'), f'{stored_ip}→{current_ip}')
            session.clear()
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Sessão inválida'}), 401

def check_user_blocked(username):
    conn = get_db()
    u = conn.execute('SELECT bloqueado_ate FROM users WHERE username=?', (username,)).fetchone()
    conn.close()
    if u and u['bloqueado_ate']:
        try:
            if datetime.now() < datetime.fromisoformat(u['bloqueado_ate']): return True
        except: pass
    return False

def inc_attempts(username):
    conn = get_db()
    conn.execute('UPDATE users SET tentativas_login=tentativas_login+1 WHERE username=?', (username,))
    u = conn.execute('SELECT tentativas_login FROM users WHERE username=?', (username,)).fetchone()
    if u and u['tentativas_login'] >= 5:
        bl = (datetime.now() + timedelta(minutes=15)).isoformat()
        conn.execute('UPDATE users SET bloqueado_ate=? WHERE username=?', (bl, username))
        log_sec('CONTA_BLOQUEADA', username)
    conn.commit(); conn.close()

def reset_attempts(username):
    conn = get_db()
    conn.execute('UPDATE users SET tentativas_login=0,bloqueado_ate=NULL WHERE username=?', (username,))
    conn.commit(); conn.close()

def pagar_comissoes(conn, usuario, valor):
    c1 = float(gcfg('comissao_n1', '0.20'))
    c2 = float(gcfg('comissao_n2', '0.10'))
    ind1 = conn.execute('SELECT indicador FROM indicacoes WHERE indicado=? AND nivel=1', (usuario,)).fetchone()
    if not ind1: return
    i1 = ind1['indicador']
    v1 = round(valor * c1, 6)
    conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel+?,total_comissao=total_comissao+? WHERE username=?', (v1, v1, i1))
    conn.execute('INSERT INTO comissoes (beneficiario,originador,nivel,valor) VALUES(?,?,?,?)', (i1, usuario, 1, v1))
    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
                 (i1, 'COMISSAO_N1', v1, f'Comissão {int(c1*100)}% — indicado: {usuario}'))
    ind2 = conn.execute('SELECT indicador FROM indicacoes WHERE indicado=? AND nivel=1', (i1,)).fetchone()
    if not ind2: return
    i2 = ind2['indicador']
    v2 = round(valor * c2, 6)
    conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel+?,total_comissao=total_comissao+? WHERE username=?', (v2, v2, i2))
    conn.execute('INSERT INTO comissoes (beneficiario,originador,nivel,valor) VALUES(?,?,?,?)', (i2, usuario, 2, v2))
    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
                 (i2, 'COMISSAO_N2', v2, f'Comissão {int(c2*100)}% — rede: {usuario}'))

def html_resp(html, code=200):
    r = make_response(html, code)
    r.headers['Content-Type'] = 'text/html; charset=utf-8'
    return r

AUTH_TMPL = """<!doctype html><html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__T__ — VOLTRIX</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Montserrat:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Montserrat',sans-serif;background:#050505;color:#e0d5c0;min-height:100vh;
display:flex;align-items:center;justify-content:center;
background-image:radial-gradient(ellipse at 15% 50%,rgba(212,175,55,.05),transparent 55%),
radial-gradient(ellipse at 85% 20%,rgba(212,175,55,.03),transparent 50%);}
.w{width:420px;padding:20px}
.logo{font-family:'Bebas Neue';font-size:54px;letter-spacing:10px;text-align:center;
background:linear-gradient(135deg,#b8920a,#d4af37,#f0d060,#d4af37);
-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:9px;letter-spacing:5px;color:#6a5a2a;text-align:center;margin-bottom:32px}
.card{background:linear-gradient(160deg,#0c0c08,#101008);border:1px solid rgba(212,175,55,.18);
border-radius:3px;padding:36px 32px;position:relative;overflow:hidden;
box-shadow:0 0 80px rgba(0,0,0,.8)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
background:linear-gradient(90deg,transparent,rgba(212,175,55,.4),transparent)}
.ct{font-size:10px;letter-spacing:4px;color:#7a6a3a;text-transform:uppercase;text-align:center;margin-bottom:28px}
.f{margin-bottom:14px}
label{display:block;font-size:9px;letter-spacing:2px;color:#6a5a2a;text-transform:uppercase;margin-bottom:6px}
input{width:100%;padding:11px 13px;background:rgba(212,175,55,.04);border:1px solid rgba(212,175,55,.14);
border-radius:2px;color:#e0d5c0;font-family:'Montserrat';font-size:13px;outline:none;transition:border-color .2s}
input:focus{border-color:rgba(212,175,55,.4)}
button{width:100%;padding:13px;margin-top:6px;background:linear-gradient(135deg,#b8920a,#d4af37);
color:#050505;font-family:'Montserrat';font-size:10px;font-weight:700;letter-spacing:4px;
text-transform:uppercase;border:none;border-radius:2px;cursor:pointer;transition:opacity .2s}
button:hover{opacity:.85}
.err{background:rgba(224,82,82,.1);border:1px solid rgba(224,82,82,.25);border-radius:2px;
padding:10px 14px;color:#e05252;font-size:11px;text-align:center;margin-bottom:16px}
.lk{text-align:center;margin-top:18px;font-size:11px;color:#6a5a2a}
.lk a{color:#d4af37;text-decoration:none}
.hint{background:rgba(212,175,55,.05);border:1px solid rgba(212,175,55,.12);border-radius:2px;
padding:10px 14px;font-size:10px;color:#7a6a3a;text-align:center;margin-top:16px;line-height:1.6}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
</style></head><body><div class="w">
<div class="logo">VOLTRIX</div>
<div class="sub">USDT Lending Protocol</div>
<div class="card"><div class="ct">__S__</div>__E____F__</div>
</div></body></html>"""

def render_auth(t, s, form, err=''):
    e = f'<div class="err">{err}</div>' if err else ''
    return AUTH_TMPL.replace('__T__',t).replace('__S__',s).replace('__E__',e).replace('__F__',form)

LOGIN_FORM = """<form method="post">
<div class="f"><label>Usuário</label><input name="username" required maxlength="30" autocomplete="username"></div>
<div class="f"><label>Senha</label><input name="password" type="password" required maxlength="72" autocomplete="current-password"></div>
<button type="submit">Acessar plataforma</button></form>
<div class="lk">Novo? <a href="/register">Criar conta</a></div>"""

REGISTER_FORM = """<form method="post">
<div class="row2">
<div class="f"><label>Usuário</label><input name="username" required maxlength="30" pattern="[a-zA-Z0-9_]{3,30}"></div>
<div class="f"><label>E-mail</label><input name="email" type="email" required maxlength="100"></div>
</div>
<div class="f"><label>Nome Completo</label><input name="nome_completo" required maxlength="100"></div>
<div class="row2">
<div class="f"><label>CPF (somente números)</label><input name="cpf" required maxlength="11" placeholder="00000000000" pattern="[0-9]{11}"></div>
<div class="f"><label>Carteira ERC-20 (USDT)</label><input name="carteira" required maxlength="42" placeholder="0x..."></div>
</div>
<div class="f"><label>Senha (mín. 8 caracteres)</label><input name="password" type="password" required minlength="8" maxlength="72"></div>
<div class="f"><label>Código de indicação (opcional)</label><input name="ref" maxlength="20" placeholder="XXXXXXXX" id="ref-input"></div>
<button type="submit">Criar conta gratuita</button></form>
<div class="lk">Já tem conta? <a href="/login">Entrar</a></div>
<div class="hint">🔗 Seu indicador recebe <strong style="color:#d4af37">20%</strong> e a rede <strong style="color:#d4af37">10%</strong> sobre seus aportes</div>
<script>const p=new URLSearchParams(location.search);const r=p.get('ref');if(r)document.getElementById('ref-input').value=r;</script>"""

# ── AUTH ROUTES ──
@app.route('/login', methods=['GET','POST'])
@rate_limit(max_calls=30, window=300, block_secs=600)
def login_page():
    if request.method == 'POST':
        username = san(request.form.get('username',''))
        password = request.form.get('password','')[:72]
        if not username or not password:
            return html_resp(render_auth('Login','Acesso à conta',LOGIN_FORM,'Preencha todos os campos'))
        if check_user_blocked(username):
            return html_resp(render_auth('Login','Acesso à conta',LOGIN_FORM,'Conta bloqueada por 15 min.'))
        conn = get_db()
        u = conn.execute('SELECT * FROM users WHERE username=? AND ativo=1', (username,)).fetchone()
        conn.close()
        if u and check_password_hash(u['password'], password):
            reset_attempts(username)
            session.permanent = True
            session['user'] = u['username']
            session['is_admin'] = bool(u['is_admin'])
            session['_ip'] = get_ip()
            log_sec('LOGIN_OK', username)
            return redirect(url_for('dashboard'))
        if u: inc_attempts(username)
        log_sec('LOGIN_FALHOU', username)
        time.sleep(1)
        return html_resp(render_auth('Login','Acesso à conta',LOGIN_FORM,'Credenciais inválidas'))
    return html_resp(render_auth('Login','Acesso à conta',LOGIN_FORM))

@app.route('/register', methods=['GET','POST'])
@rate_limit(max_calls=50, window=600, block_secs=120)
def register_page():
    if request.method == 'POST':
        username     = san(request.form.get('username',''))
        email        = san(request.form.get('email',''), 100)
        nome_completo= san(request.form.get('nome_completo',''), 100)
        cpf          = re.sub(r'\D','', request.form.get('cpf',''))[:11]
        carteira     = san(request.form.get('carteira',''), 42)
        password     = request.form.get('password','')[:72]
        ref_code     = san(request.form.get('ref',''), 20).upper()

        if not valid_user(username):
            return html_resp(render_auth('Registro','Criar conta',REGISTER_FORM,'Usuário: 3-30 chars, letras/números/_'))
        if not valid_email(email):
            return html_resp(render_auth('Registro','Criar conta',REGISTER_FORM,'E-mail inválido'))
        if not nome_completo or len(nome_completo) < 3:
            return html_resp(render_auth('Registro','Criar conta',REGISTER_FORM,'Informe seu nome completo'))
        if not valid_cpf(cpf):
            return html_resp(render_auth('Registro','Criar conta',REGISTER_FORM,'CPF inválido (somente 11 dígitos)'))
        if not valid_eth(carteira):
            return html_resp(render_auth('Registro','Criar conta',REGISTER_FORM,'Carteira ERC-20 inválida (0x + 40 hex)'))
        if len(password) < 8:
            return html_resp(render_auth('Registro','Criar conta',REGISTER_FORM,'Senha mínima: 8 caracteres'))

        conn = get_db()
        indicador = None
        if ref_code:
            row = conn.execute('SELECT username FROM users WHERE codigo_indicacao=?', (ref_code,)).fetchone()
            if row: indicador = row['username']
        codigo = str(uuid.uuid4())[:8].upper()
        try:
            conn.execute(
                'INSERT INTO users (username,nome_completo,cpf,email,password,carteira,referido_por,codigo_indicacao) VALUES(?,?,?,?,?,?,?,?)',
                (username, nome_completo, cpf, email, generate_password_hash(password), carteira, indicador, codigo)
            )
            if indicador:
                conn.execute('INSERT INTO indicacoes (indicador,indicado,nivel) VALUES(?,?,1)', (indicador, username))
                ind2 = conn.execute('SELECT referido_por FROM users WHERE username=?', (indicador,)).fetchone()
                if ind2 and ind2['referido_por']:
                    conn.execute('INSERT INTO indicacoes (indicador,indicado,nivel) VALUES(?,?,2)', (ind2['referido_por'], username))
            conn.commit()
            session.permanent = True
            session['user'] = username
            session['is_admin'] = False
            session['_ip'] = get_ip()
            log_sec('REGISTRO', username, f'ref={indicador}')
            return redirect(url_for('dashboard'))
        except sqlite3.IntegrityError:
            return html_resp(render_auth('Registro','Criar conta',REGISTER_FORM,'Usuário, e-mail ou CPF já cadastrado'))
        finally:
            conn.close()
    return html_resp(render_auth('Registro','Criar conta',REGISTER_FORM))

@app.route('/logout')
def logout():
    log_sec('LOGOUT', session.get('user'))
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/')
def root():
    return redirect(url_for('dashboard') if 'user' in session else url_for('login_page'))

@app.route('/dashboard')
@require_login
def dashboard():
    return send_file('voltrix_dashboard.html')

# ── API ME ──
@app.route('/api/me')
@require_login
def api_me():
    conn = get_db()
    u = conn.execute('SELECT * FROM users WHERE username=?', (session['user'],)).fetchone()
    # Calcular rendimento pendente de todos aportes aprovados
    aportes = conn.execute(
        "SELECT ap.*, p.rendimento_diario FROM aportes_projeto ap JOIN projetos p ON p.id=ap.projeto_id "
        "WHERE ap.usuario=? AND ap.status='aprovado' AND p.status='ativo'",
        (session['user'],)
    ).fetchall()
    rendimento_pendente = 0.0
    for ap in aportes:
        ultimo = ap['ultimo_collect'] or ap['data_aprovacao']
        if not ultimo: continue
        rendimento_pendente += calc_rendimento_pendente_aporte(ap)

    saldo_em_projetos = conn.execute(
        "SELECT COALESCE(SUM(valor),0) FROM aportes_projeto WHERE usuario=? AND status='aprovado' AND COALESCE(lucro_coletado,0) < valor",
        (session['user'],)
    ).fetchone()[0]

    n1 = conn.execute('SELECT COUNT(*) FROM indicacoes WHERE indicador=? AND nivel=1', (session['user'],)).fetchone()[0]
    n2 = conn.execute('SELECT COUNT(*) FROM indicacoes WHERE indicador=? AND nivel=2', (session['user'],)).fetchone()[0]
    conn.close()
    return jsonify({
        'user': u['username'],
        'nome_completo': u['nome_completo'],
        'email': u['email'],
        'cpf': u['cpf'],
        'carteira': u['carteira'],
        'is_admin': bool(u['is_admin']),
        'saldo_disponivel': round(float(u['saldo_disponivel']),6),
        'saldo_em_projetos': round(float(saldo_em_projetos),6),
        'total_ganho': round(float(u['total_ganho']),6),
        'total_depositado': round(float(u['total_depositado']),6),
        'total_sacado': round(float(u['total_sacado']),6),
        'total_comissao': round(float(u['total_comissao']),6),
        'rendimento_pendente': round(rendimento_pendente, 6),
        'aporte_minimo': float(gcfg('aporte_minimo','10')),
        'saque_minimo': float(gcfg('saque_minimo','10')),
        'taxa_saque_pct': round(float(gcfg('taxa_saque','0.05'))*100,2),
        'carteira_deposito': gcfg('carteira_deposito', CARTEIRA_DEPOSITO),
        'codigo_indicacao': u['codigo_indicacao'],
        'total_indicados_n1': n1,
        'total_indicados_n2': n2,
        'comissao_n1_pct': round(float(gcfg('comissao_n1','0.20'))*100),
        'comissao_n2_pct': round(float(gcfg('comissao_n2','0.10'))*100),
    })

# ── API PROJETOS ──
@app.route('/api/projetos')
@require_login
def api_projetos():
    conn = get_db()
    rows = conn.execute(
        "SELECT p.*, "
        "(SELECT COALESCE(SUM(ap.valor),0) FROM aportes_projeto ap WHERE ap.projeto_id=p.id AND ap.status='aprovado') as captado_real "
        "FROM projetos p WHERE p.status='ativo' ORDER BY p.data_aprovacao DESC"
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['progresso_pct'] = round((d['captado_real'] / d['meta_usdt'] * 100), 1) if d['meta_usdt'] > 0 else 0
        result.append(d)
    conn.close()
    return jsonify(result)

@app.route('/api/projetos/meus_aportes')
@require_login
def api_meus_aportes_projeto():
    conn = get_db()
    rows = conn.execute(
        "SELECT ap.*, p.nome as projeto_nome, p.rendimento_diario, p.status as projeto_status "
        "FROM aportes_projeto ap JOIN projetos p ON p.id=ap.projeto_id "
        "WHERE ap.usuario=? ORDER BY ap.data_aporte DESC",
        (session['user'],)
    ).fetchall()
    result = []
    for ap in rows:
        d = dict(ap)
        if d['status'] == 'aprovado' and d['projeto_status'] == 'ativo':
            ultimo = d['ultimo_collect'] or d['data_aprovacao']
            if ultimo:
                try:
                    d['rendimento_pendente'] = calc_rendimento_pendente_aporte(ap)
                except: d['rendimento_pendente'] = 0
            else: d['rendimento_pendente'] = 0
        else: d['rendimento_pendente'] = 0
        lucro = float(d.get('lucro_coletado') or 0)
        valor = float(d.get('valor') or 0)
        d['lucro_limite'] = round(valor, 6)
        d['dobrou'] = bool(valor > 0 and lucro >= valor)
        d['progresso_dobra_pct'] = round(min(100, (lucro / valor * 100)) if valor > 0 else 0, 2)
        result.append(d)
    conn.close()
    return jsonify(result)

@app.route('/api/projetos/aportar', methods=['POST'])
@require_login
@rate_limit(max_calls=10, window=300)
def api_aportar_projeto():
    data = request.get_json(silent=True) or {}
    try: valor = float(data.get('valor', 0))
    except: return jsonify({'error': 'Valor inválido'}), 400
    projeto_id = data.get('projeto_id')
    txid = san(str(data.get('txid','')), 100)
    amin = float(gcfg('aporte_minimo','10'))
    if valor < amin: return jsonify({'error': f'Mínimo: {amin} USDT'}), 400
    if not txid: return jsonify({'error': 'Informe o TXID da transação'}), 400
    if not projeto_id: return jsonify({'error': 'Selecione um projeto'}), 400
    conn = get_db()
    p = conn.execute("SELECT * FROM projetos WHERE id=? AND status='ativo'", (projeto_id,)).fetchone()
    if not p:
        conn.close(); return jsonify({'error': 'Projeto não encontrado ou inativo'}), 404
    if conn.execute('SELECT id FROM aportes_projeto WHERE txid=?', (txid,)).fetchone():
        conn.close(); return jsonify({'error': 'TXID já registrado'}), 409
    conn.execute(
        'INSERT INTO aportes_projeto (usuario,projeto_id,valor,txid) VALUES(?,?,?,?)',
        (session['user'], projeto_id, valor, txid)
    )
    conn.commit(); conn.close()
    log_sec('APORTE_PROJETO', session['user'], f'{valor} USDT → projeto #{projeto_id}')
    return jsonify({'message': 'Aporte registrado! Aguarde aprovação do administrador.', 'valor': valor}), 201

@app.route('/api/projetos/coletar', methods=['POST'])
@require_login
@rate_limit(max_calls=60, window=3600)
def api_coletar_projeto():
    """
    Coleta rendimento de UM aporte ou de TODOS os aportes ativos.
    - Se enviar {"aporte_id": 123}, coleta apenas esse aporte.
    - Se não enviar aporte_id, coleta tudo que estiver pendente.
    """
    data = request.get_json(silent=True) or {}
    aporte_id = data.get('aporte_id')

    conn = get_db()

    if aporte_id:
        rows = conn.execute(
            "SELECT ap.*, p.rendimento_diario, p.status as projeto_status FROM aportes_projeto ap "
            "JOIN projetos p ON p.id=ap.projeto_id "
            "WHERE ap.id=? AND ap.usuario=? AND ap.status='aprovado'",
            (aporte_id, session['user'])
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ap.*, p.rendimento_diario, p.status as projeto_status FROM aportes_projeto ap "
            "JOIN projetos p ON p.id=ap.projeto_id "
            "WHERE ap.usuario=? AND ap.status='aprovado' AND p.status='ativo'",
            (session['user'],)
        ).fetchall()

    if not rows:
        conn.close()
        return jsonify({'error': 'Nenhum aporte aprovado encontrado'}), 404

    total = 0.0
    coletados = 0
    agora_dt = datetime.now()
    agora = agora_dt.isoformat()

    for ap in rows:
        if ap['projeto_status'] != 'ativo':
            continue

        ultimo = ap['ultimo_collect'] or ap['data_aprovacao']
        if not ultimo:
            continue

        taxa_dia = float(ap['rendimento_diario'])
        rend = calc_rendimento_pendente_aporte(ap, agora_dt)

        # Prazo até a dobra: se já bateu 100% de lucro, não rende mais.
        if rend <= 0:
            continue

        lucro_atual = float(ap['lucro_coletado'] or 0)
        valor_aporte = float(ap['valor'] or 0)
        novo_lucro = round(min(valor_aporte, lucro_atual + rend), 6)
        concluiu = valor_aporte > 0 and novo_lucro >= valor_aporte
        data_conclusao = agora if concluiu else ap['data_conclusao']

        total += rend
        coletados += 1

        conn.execute(
            'UPDATE aportes_projeto SET ultimo_collect=?, lucro_coletado=?, data_conclusao=? WHERE id=?',
            (agora, novo_lucro, data_conclusao, ap['id'])
        )
        conn.execute(
            'INSERT INTO rendimentos (usuario,projeto_id,valor,percentual,saldo_base) VALUES(?,?,?,?,?)',
            (session['user'], ap['projeto_id'], rend, (rend / float(ap['valor'])) if float(ap['valor']) > 0 else 0, float(ap['valor']))
        )
        conn.execute(
            'INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
            (session['user'], 'RENDIMENTO', rend, f'Rendimento projeto #{ap["projeto_id"]} — {round(taxa_dia*100,2)}%/dia')
        )

    if total <= 0:
        conn.close()
        return jsonify({'error': 'Rendimento ainda não disponível ou aporte já dobrou'}), 400

    total = round(total, 6)
    conn.execute(
        'UPDATE users SET saldo_disponivel=saldo_disponivel+?, total_ganho=total_ganho+? WHERE username=?',
        (total, total, session['user'])
    )
    conn.commit()
    conn.close()

    return jsonify({
        'message': f'✓ {total:.6f} USDT coletado!',
        'valor': total,
        'coletados': coletados
    })

# ── API SAQUE ──
@app.route('/api/saque', methods=['POST'])
@require_login
@rate_limit(max_calls=5, window=600)
def api_saque():
    data = request.get_json(silent=True) or {}
    try: valor = float(data.get('valor',0))
    except: return jsonify({'error': 'Valor inválido'}), 400
    carteira = san(str(data.get('carteira','')), 42)
    smin = float(gcfg('saque_minimo','10'))
    tp   = float(gcfg('taxa_saque','0.05'))
    if valor < smin: return jsonify({'error': f'Mínimo: {smin} USDT'}), 400
    if not valid_eth(carteira): return jsonify({'error': 'Carteira ERC-20 inválida'}), 400
    conn = get_db()
    u = conn.execute('SELECT saldo_disponivel FROM users WHERE username=?', (session['user'],)).fetchone()
    if float(u['saldo_disponivel']) < valor:
        conn.close(); return jsonify({'error': f'Saldo insuficiente: {round(float(u["saldo_disponivel"]),4)} USDT'}), 400
    taxa = round(valor*tp, 6); liquido = round(valor-taxa, 6)
    conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel-? WHERE username=?', (valor, session['user']))
    conn.execute('INSERT INTO saques (usuario,valor,carteira,taxa,valor_liquido) VALUES(?,?,?,?,?)',
                 (session['user'], valor, carteira, taxa, liquido))
    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
                 (session['user'],'SAQUE',valor,f'→ {carteira[:8]}...{carteira[-4:]}'))
    conn.commit(); conn.close()
    return jsonify({'message': f'Saque de {liquido:.4f} USDT solicitado!', 'valor_liquido': liquido}), 201

# ── API HISTÓRICO / REDE ──
@app.route('/api/historico')
@require_login
def api_historico():
    conn = get_db()
    rows = conn.execute('SELECT tipo,valor,descricao,data_evento FROM movimentacoes WHERE usuario=? ORDER BY id DESC LIMIT 60', (session['user'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/meus_saques')
@require_login
def api_meus_saques():
    conn = get_db()
    rows = conn.execute('SELECT valor,carteira,taxa,valor_liquido,status,data_solicitacao FROM saques WHERE usuario=? ORDER BY id DESC LIMIT 20', (session['user'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/rede')
@require_login
def api_rede():
    conn = get_db()
    n1 = conn.execute(
        'SELECT i.indicado,u.saldo_disponivel,u.total_depositado,u.total_ganho FROM indicacoes i JOIN users u ON u.username=i.indicado WHERE i.indicador=? AND i.nivel=1 ORDER BY u.total_depositado DESC',
        (session['user'],)
    ).fetchall()
    n2 = conn.execute(
        'SELECT i.indicado,u.total_depositado FROM indicacoes i JOIN users u ON u.username=i.indicado WHERE i.indicador=? AND i.nivel=2 ORDER BY u.total_depositado DESC',
        (session['user'],)
    ).fetchall()
    comissoes = conn.execute(
        'SELECT nivel,SUM(valor) as total FROM comissoes WHERE beneficiario=? GROUP BY nivel',
        (session['user'],)
    ).fetchall()
    codigo = conn.execute('SELECT codigo_indicacao FROM users WHERE username=?', (session['user'],)).fetchone()
    conn.close()
    return jsonify({
        'codigo_indicacao': codigo['codigo_indicacao'] if codigo else '',
        'nivel1': [dict(r) for r in n1],
        'nivel2': [dict(r) for r in n2],
        'comissoes': [dict(r) for r in comissoes],
    })

# ── ADMIN ──
@app.route('/api/admin/dashboard')
@require_admin
def admin_dash():
    conn = get_db()
    d = {
        'total_usuarios': conn.execute('SELECT COUNT(*) FROM users WHERE is_admin=0').fetchone()[0],
        'total_em_projetos': round(float(conn.execute("SELECT COALESCE(SUM(ap.valor),0) FROM aportes_projeto ap WHERE ap.status='aprovado'").fetchone()[0]),2),
        'total_disponivel': round(float(conn.execute('SELECT COALESCE(SUM(saldo_disponivel),0) FROM users').fetchone()[0]),2),
        'aportes_pendentes': conn.execute("SELECT COUNT(*) FROM aportes_projeto WHERE status='pendente'").fetchone()[0],
        'saques_pendentes': conn.execute("SELECT COUNT(*) FROM saques WHERE status='pendente'").fetchone()[0],
        'projetos_ativos': conn.execute("SELECT COUNT(*) FROM projetos WHERE status='ativo'").fetchone()[0],
        'projetos_pendentes': conn.execute("SELECT COUNT(*) FROM projetos WHERE status='pendente'").fetchone()[0],
        'total_rendimentos_pagos': round(float(conn.execute('SELECT COALESCE(SUM(total_ganho),0) FROM users').fetchone()[0]),2),
        'total_comissoes': round(float(conn.execute('SELECT COALESCE(SUM(total_comissao),0) FROM users').fetchone()[0]),2),
    }
    conn.close(); return jsonify(d)

@app.route('/api/admin/projetos', methods=['GET'])
@require_admin
def admin_projetos_list():
    conn = get_db()
    rows = conn.execute('SELECT * FROM projetos ORDER BY data_criacao DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/projetos/criar', methods=['POST'])
@require_admin
def admin_criar_projeto():
    data = request.get_json(silent=True) or {}
    nome      = san(str(data.get('nome','')), 100)
    descricao = san(str(data.get('descricao','')), 500)
    categoria = san(str(data.get('categoria','crypto')), 30)
    try:
        meta      = float(data.get('meta_usdt', 0))
        rendimento= float(data.get('rendimento_diario', 0))
    except: return jsonify({'error': 'Valores inválidos'}), 400
    if not nome: return jsonify({'error': 'Nome obrigatório'}), 400
    if meta <= 0: return jsonify({'error': 'Meta deve ser > 0'}), 400
    if rendimento <= 0 or rendimento > 0.10: return jsonify({'error': 'Rendimento diário entre 0.001 e 0.10 (10%)'}), 400
    conn = get_db()
    conn.execute(
        'INSERT INTO projetos (nome,descricao,categoria,meta_usdt,rendimento_diario,status,criado_por) VALUES(?,?,?,?,?,?,?)',
        (nome, descricao, categoria, meta, rendimento, 'pendente', session['user'])
    )
    conn.commit(); conn.close()
    log_sec('PROJETO_CRIADO', session['user'], nome)
    return jsonify({'message': f'Projeto "{nome}" criado!'}), 201

@app.route('/api/admin/projetos/aprovar/<int:pid>', methods=['POST'])
@require_admin
def admin_aprovar_projeto(pid):
    conn = get_db()
    p = conn.execute("SELECT * FROM projetos WHERE id=? AND status='pendente'", (pid,)).fetchone()
    if not p: conn.close(); return jsonify({'error': 'Projeto não encontrado'}), 404
    conn.execute("UPDATE projetos SET status='ativo',data_aprovacao=? WHERE id=?", (datetime.now().isoformat(), pid))
    conn.commit(); conn.close()
    log_sec('PROJETO_APROVADO', session['user'], f'#{pid}')
    return jsonify({'message': f'Projeto "{p["nome"]}" ativado!'})

@app.route('/api/admin/projetos/encerrar/<int:pid>', methods=['POST'])
@require_admin
def admin_encerrar_projeto(pid):
    conn = get_db()
    conn.execute("UPDATE projetos SET status='encerrado',data_encerramento=? WHERE id=?", (datetime.now().isoformat(), pid))
    conn.commit(); conn.close()
    return jsonify({'message': 'Projeto encerrado.'})

@app.route('/api/admin/aportes_pendentes')
@require_admin
def admin_aportes():
    conn = get_db()
    rows = conn.execute(
        "SELECT ap.*,p.nome as projeto_nome FROM aportes_projeto ap JOIN projetos p ON p.id=ap.projeto_id WHERE ap.status='pendente' ORDER BY ap.data_aporte"
    ).fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/admin/aprovar_aporte/<int:aid>', methods=['POST'])
@require_admin
def admin_aprovar_aporte(aid):
    conn = get_db()
    a = conn.execute("SELECT * FROM aportes_projeto WHERE id=? AND status='pendente'", (aid,)).fetchone()
    if not a: conn.close(); return jsonify({'error': 'Não encontrado'}), 404
    agora = datetime.now().isoformat()
    conn.execute("UPDATE aportes_projeto SET status='aprovado',data_aprovacao=?,ultimo_collect=?,aprovado_por=? WHERE id=?",
                 (agora, agora, session['user'], aid))
    conn.execute('UPDATE users SET total_depositado=total_depositado+? WHERE username=?', (a['valor'], a['usuario']))
    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
                 (a['usuario'],'APORTE',a['valor'],f'Aporte aprovado no projeto #{a["projeto_id"]}'))
    pagar_comissoes(conn, a['usuario'], a['valor'])
    conn.commit(); conn.close()
    log_sec('APORTE_APROVADO', session['user'], f'#{aid} {a["usuario"]}')
    return jsonify({'message': f'{a["valor"]} USDT aprovado para {a["usuario"]}!'})

@app.route('/api/admin/rejeitar_aporte/<int:aid>', methods=['POST'])
@require_admin
def admin_rejeitar_aporte(aid):
    conn = get_db()
    conn.execute("UPDATE aportes_projeto SET status='rejeitado' WHERE id=? AND status='pendente'", (aid,))
    conn.commit(); conn.close()
    return jsonify({'message': 'Rejeitado.'})

@app.route('/api/admin/saques_pendentes')
@require_admin
def admin_saques():
    conn = get_db()
    rows = conn.execute("SELECT * FROM saques WHERE status='pendente' ORDER BY data_solicitacao").fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/admin/aprovar_saque/<int:sid>', methods=['POST'])
@require_admin
def admin_aprovar_saque(sid):
    data = request.get_json(silent=True) or {}
    txid_s = san(str(data.get('txid','')), 100)
    conn = get_db()
    s = conn.execute("SELECT * FROM saques WHERE id=? AND status='pendente'", (sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error': 'Não encontrado'}), 404
    conn.execute("UPDATE saques SET status='aprovado',txid_saida=?,data_processamento=? WHERE id=?",
                 (txid_s, datetime.now().isoformat(), sid))
    conn.execute('UPDATE users SET total_sacado=total_sacado+? WHERE username=?', (s['valor_liquido'], s['usuario']))
    conn.commit(); conn.close()
    return jsonify({'message': f'{s["valor_liquido"]} USDT aprovado!'})

@app.route('/api/admin/rejeitar_saque/<int:sid>', methods=['POST'])
@require_admin
def admin_rejeitar_saque(sid):
    conn = get_db()
    s = conn.execute("SELECT * FROM saques WHERE id=? AND status='pendente'", (sid,)).fetchone()
    if not s: conn.close(); return jsonify({'error': 'Não encontrado'}), 404
    conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel+? WHERE username=?', (s['valor'], s['usuario']))
    conn.execute("UPDATE saques SET status='rejeitado' WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({'message': f'{s["valor"]} USDT devolvidos.'})

@app.route('/api/admin/usuarios')
@require_admin
def admin_users():
    conn = get_db()
    rows = conn.execute(
        'SELECT username,nome_completo,cpf,email,carteira,saldo_disponivel,total_depositado,total_ganho,total_sacado,total_comissao,referido_por,data_cadastro FROM users WHERE is_admin=0 ORDER BY data_cadastro DESC'
    ).fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/admin/config', methods=['GET','POST'])
@require_admin
def admin_config():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        for k in ['aporte_minimo','saque_minimo','taxa_saque','carteira_deposito','comissao_n1','comissao_n2']:
            if k in data: scfg(k, san(str(data[k]),100))
        log_sec('CONFIG_ATUALIZADA', session['user'])
        return jsonify({'message': 'Salvo!'})
    ks = ['aporte_minimo','saque_minimo','taxa_saque','carteira_deposito','comissao_n1','comissao_n2']
    return jsonify({k: gcfg(k) for k in ks})

@app.route('/api/admin/creditar_manual', methods=['POST'])
@require_admin
def admin_creditar():
    data = request.get_json(silent=True) or {}
    usuario = san(str(data.get('usuario','')), 30)
    try: valor = float(data.get('valor',0))
    except: return jsonify({'error': 'Valor inválido'}), 400
    if valor <= 0: return jsonify({'error': 'Valor inválido'}), 400
    conn = get_db()
    if not conn.execute('SELECT username FROM users WHERE username=?', (usuario,)).fetchone():
        conn.close(); return jsonify({'error': 'Usuário não encontrado'}), 404
    conn.execute('UPDATE users SET saldo_disponivel=saldo_disponivel+? WHERE username=?', (valor, usuario))
    conn.execute('INSERT INTO movimentacoes (usuario,tipo,valor,descricao) VALUES(?,?,?,?)',
                 (usuario,'CREDITO_MANUAL',valor,'Admin: crédito manual'))
    conn.commit(); conn.close()
    log_sec('CREDITO_MANUAL', session['user'], f'{valor}→{usuario}')
    return jsonify({'message': f'{valor} USDT creditados para {usuario}!'})

@app.route('/api/admin/logs')
@require_admin
def admin_logs():
    conn = get_db()
    rows = conn.execute('SELECT ip,evento,usuario,detalhes,data_evento FROM logs_seguranca ORDER BY id DESC LIMIT 300').fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG','false').lower()=='true',host='0.0.0.0',port=int(os.environ.get('PORT',5000)))
