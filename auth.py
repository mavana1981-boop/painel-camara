"""Autenticação: usuários no banco, senha com hash (scrypt), sessão assinada, CSRF e limite de tentativas."""
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, session,
                   url_for)
from sqlalchemy import delete, func, insert, select, update
from werkzeug.security import check_password_hash, generate_password_hash

from db import engine, usuarios

log = logging.getLogger("auth")
bp = Blueprint("auth", __name__)

ROTAS_PUBLICAS = {"auth.login", "static", "saude", "api_coletar"}
MAX_TENTATIVAS, JANELA = 5, 15 * 60
_falhas = defaultdict(deque)
SENHA_MIN = 8


# ---------------- configuração ----------------

def configurar(app):
    chave = os.environ.get("SECRET_KEY") or _chave_persistente()
    app.config.update(
        SECRET_KEY=chave,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT")),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    app.register_blueprint(bp)
    app.before_request(_exigir_login)
    app.context_processor(lambda: {"usuario": g.get("usuario"), "csrf_token": csrf_token})
    criar_admin_inicial()


def _chave_persistente():
    """Sem SECRET_KEY no ambiente, usa uma chave gerada uma única vez e guardada no banco.
    Assim o login sobrevive a reinícios e funciona com mais de um processo/réplica."""
    from db import meta
    with engine.begin() as c:
        v = c.execute(select(meta.c.valor).where(meta.c.chave == "secret_key")).scalar()
        if not v:
            try:
                c.execute(insert(meta).values(chave="secret_key", valor=secrets.token_hex(32)))
            except Exception:
                pass  # outro processo criou ao mesmo tempo
    with engine.connect() as c:
        v = c.execute(select(meta.c.valor).where(meta.c.chave == "secret_key")).scalar()
    log.info("SECRET_KEY não definida no ambiente: usando chave persistente guardada no banco.")
    return v


def criar_admin_inicial():
    """Cria o administrador a partir de ADMIN_USER / ADMIN_PASSWORD, se ele ainda não existir."""
    login, senha = os.environ.get("ADMIN_USER"), os.environ.get("ADMIN_PASSWORD")
    if not login or not senha:
        return
    login = login.strip().lower()
    with engine.begin() as c:
        existe = c.execute(select(usuarios.c.id).where(usuarios.c.login == login)).first()
        if not existe:
            c.execute(insert(usuarios).values(
                login=login, nome=os.environ.get("ADMIN_NOME", "Administrador"),
                senha_hash=generate_password_hash(senha), admin=True, ativo=True,
                criado=datetime.now()))
            log.info("administrador inicial '%s' criado", login)


def ha_usuarios():
    with engine.connect() as c:
        return (c.execute(select(func.count()).select_from(usuarios)).scalar() or 0) > 0


# ---------------- CSRF ----------------

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def _csrf_ok():
    enviado = request.form.get("_csrf", "")
    return bool(enviado) and hmac.compare_digest(enviado, session.get("_csrf", ""))


def _checar_csrf():
    if not _csrf_ok():
        abort(400, "Formulário expirado. Recarregue a página e tente de novo.")


# ---------------- guarda de rotas ----------------

def _exigir_login():
    if request.endpoint in ROTAS_PUBLICAS or request.endpoint is None:
        return None
    uid = session.get("uid")
    if uid:
        with engine.connect() as c:
            u = c.execute(select(usuarios).where(usuarios.c.id == uid)).mappings().first()
        if u and u["ativo"] and session.get("ver") == _versao(u):
            g.usuario = dict(u)
            if request.method == "POST":
                _checar_csrf()
            return None
        session.clear()
    destino = request.full_path if request.method == "GET" else None
    return redirect(url_for("auth.login", proximo=destino if destino and destino != "/?" else None))


def _versao(u):
    """Muda quando a senha muda — derruba sessões antigas."""
    return u["senha_hash"][-12:]


def exigir_admin(fn):
    @wraps(fn)
    def _w(*a, **k):
        if not (g.get("usuario") or {}).get("admin"):
            abort(403)
        return fn(*a, **k)
    return _w


def _destino_seguro(url):
    return url if url and url.startswith("/") and not url.startswith("//") else url_for("index")


def _bloqueado(chave):
    agora = time.time()
    fila = _falhas[chave]
    while fila and agora - fila[0] > JANELA:
        fila.popleft()
    return len(fila) >= MAX_TENTATIVAS


# ---------------- rotas ----------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("uid") and request.method == "GET":
        return redirect(url_for("index"))
    erro = None
    if request.method == "POST" and not _csrf_ok():
        session.pop("_csrf", None)
        return render_template("login.html", sem_usuarios=not ha_usuarios(),
                               erro="A tela de login ficou aberta por muito tempo ou o servidor foi "
                                    "reiniciado. Digite usuário e senha novamente.",
                               proximo=request.form.get("proximo") or "")
    if request.method == "POST":
        login_ = (request.form.get("login") or "").strip().lower()
        senha = request.form.get("senha") or ""
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        chave = f"{ip}|{login_}"
        if _bloqueado(chave):
            erro = "Muitas tentativas. Aguarde 15 minutos e tente novamente."
        else:
            with engine.connect() as c:
                u = c.execute(select(usuarios).where(usuarios.c.login == login_)).mappings().first()
            if u and u["ativo"] and check_password_hash(u["senha_hash"], senha):
                _falhas.pop(chave, None)
                session.clear()
                session["uid"] = u["id"]
                session["ver"] = _versao(u)
                session.permanent = bool(request.form.get("lembrar"))
                with engine.begin() as c:
                    c.execute(update(usuarios).where(usuarios.c.id == u["id"])
                              .values(ultimo_acesso=datetime.now()))
                return redirect(_destino_seguro(request.form.get("proximo")))
            _falhas[chave].append(time.time())
            erro = "Usuário ou senha inválidos."
    return render_template("login.html", erro=erro, sem_usuarios=not ha_usuarios(),
                           proximo=request.values.get("proximo") or "")


@bp.post("/sair")
def sair():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/conta", methods=["GET", "POST"])
def conta():
    if request.method == "POST":
        u = g.usuario
        atual, nova, conf = (request.form.get(k) or "" for k in ("atual", "nova", "confirma"))
        if not check_password_hash(u["senha_hash"], atual):
            flash("Senha atual incorreta.", "erro")
        elif len(nova) < SENHA_MIN:
            flash(f"A nova senha precisa ter ao menos {SENHA_MIN} caracteres.", "erro")
        elif nova != conf:
            flash("A confirmação não confere.", "erro")
        else:
            h = generate_password_hash(nova)
            with engine.begin() as c:
                c.execute(update(usuarios).where(usuarios.c.id == u["id"]).values(senha_hash=h))
            session["ver"] = h[-12:]
            flash("Senha alterada.", "ok")
        return redirect(url_for("auth.conta"))
    return render_template("conta.html", aba=None)


@bp.route("/usuarios", methods=["GET", "POST"])
@exigir_admin
def gerenciar():
    if request.method == "POST":
        acao = request.form.get("acao")
        uid = request.form.get("id", type=int)
        with engine.begin() as c:
            if acao == "criar":
                login_ = (request.form.get("login") or "").strip().lower()
                senha = request.form.get("senha") or ""
                if not login_ or len(senha) < SENHA_MIN:
                    flash(f"Informe o usuário e uma senha de ao menos {SENHA_MIN} caracteres.", "erro")
                elif c.execute(select(usuarios.c.id).where(usuarios.c.login == login_)).first():
                    flash("Esse usuário já existe.", "erro")
                else:
                    c.execute(insert(usuarios).values(
                        login=login_, nome=(request.form.get("nome") or "").strip() or login_,
                        senha_hash=generate_password_hash(senha),
                        admin=bool(request.form.get("admin")), ativo=True, criado=datetime.now()))
                    flash(f"Usuário {login_} criado.", "ok")
            elif uid and uid == g.usuario["id"] and acao in ("alternar", "excluir", "admin"):
                flash("Você não pode desativar, excluir ou rebaixar o próprio usuário.", "erro")
            elif acao == "alternar" and uid:
                c.execute(update(usuarios).where(usuarios.c.id == uid).values(ativo=~usuarios.c.ativo))
            elif acao == "admin" and uid:
                c.execute(update(usuarios).where(usuarios.c.id == uid).values(admin=~usuarios.c.admin))
            elif acao == "senha" and uid:
                senha = request.form.get("senha") or ""
                if len(senha) < SENHA_MIN:
                    flash(f"A senha precisa ter ao menos {SENHA_MIN} caracteres.", "erro")
                else:
                    c.execute(update(usuarios).where(usuarios.c.id == uid)
                              .values(senha_hash=generate_password_hash(senha)))
                    flash("Senha redefinida.", "ok")
            elif acao == "excluir" and uid:
                c.execute(delete(usuarios).where(usuarios.c.id == uid))
                flash("Usuário excluído.", "ok")
        return redirect(url_for("auth.gerenciar"))
    with engine.connect() as c:
        lista = [dict(r) for r in c.execute(select(usuarios).order_by(usuarios.c.login)).mappings()]
    return render_template("usuarios.html", aba=None, lista=lista)
