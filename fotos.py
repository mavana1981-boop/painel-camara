"""Fotos dos usuários.

As fotos vêm do app Minha Pauta (plenario-assessoria), onde o administrador já cadastrou a foto de cada
assessor: a cada sincronização do Plenário o painel copia as fotos da tabela `users` de lá, casando pelo
login (usuário igual) ou, se não houver, pelo nome exibido. O administrador também pode enviar ou trocar a
foto em Usuários; uma foto enviada aqui não é sobrescrita pela do Minha Pauta.

As imagens ficam em `fotos_usuarios` (reduzidas para 256 px) e são servidas por /foto/<id>.
"""
import base64
import binascii
import io
import logging
import re
from datetime import datetime

from flask import Blueprint, abort, g, make_response, url_for
from markupsafe import Markup, escape
from sqlalchemy import delete, insert, select, update

from db import engine, fotos_usuarios, norm_nome, usuarios

log = logging.getLogger("fotos")
bp = Blueprint("fotos", __name__)

LADO = 256
MAX_FOTO = 8 * 1024 * 1024
TIPOS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
CORES = ["#0b5d3b", "#2563c9", "#8a6d22", "#7b3fa0", "#b03021", "#1f7a8c", "#5b6779", "#a0522d"]


def reduzir(dados):
    """Reduz a imagem para no máximo 256 px (JPEG). Devolve (mime, bytes) ou None se não for imagem."""
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(dados))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            fundo = Image.new("RGB", im.size, (255, 255, 255))
            fundo.paste(im, mask=im.split()[-1])
            im = fundo
        else:
            im = im.convert("RGB")
        im.thumbnail((LADO, LADO))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return "image/jpeg", out.getvalue()
    except Exception:
        return None


def _de_data_uri(valor):
    """'data:image/png;base64,....' -> bytes (ou None)."""
    m = re.match(r"data:image/[\w.+-]+;base64,(.+)", (valor or "").strip(), re.S)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1), validate=False)
    except (binascii.Error, ValueError):
        return None


def _gravar(c, uid, mime, dados, origem):
    agora = datetime.now()
    atual = c.execute(select(fotos_usuarios.c.conteudo, fotos_usuarios.c.origem)
                      .where(fotos_usuarios.c.usuario_id == uid)).first()
    if atual is None:
        c.execute(insert(fotos_usuarios).values(usuario_id=uid, mime=mime, conteudo=dados, origem=origem,
                                                atualizado_em=agora))
        return True
    if atual[0] == dados and atual[1] == origem:
        return False
    c.execute(update(fotos_usuarios).where(fotos_usuarios.c.usuario_id == uid)
              .values(mime=mime, conteudo=dados, origem=origem, atualizado_em=agora))
    return True


# ---------------- cópia do Minha Pauta ----------------

def sincronizar(conn_fonte):
    """Copia as fotos dos usuários do Minha Pauta. Devolve quantas fotos foram gravadas ou trocadas."""
    import plenario
    linhas = plenario._consulta(conn_fonte, "SELECT username, nome_display, foto FROM users "
                                            "WHERE foto IS NOT NULL AND foto <> ''")
    if not linhas:
        return 0
    por_login, por_nome = {}, {}
    for r in linhas:
        if r.get("username"):
            por_login[str(r["username"]).strip().lower()] = r["foto"]
        if r.get("nome_display"):
            por_nome.setdefault(norm_nome(r["nome_display"]), r["foto"])
    n = 0
    with engine.begin() as c:
        manuais = {x[0] for x in c.execute(select(fotos_usuarios.c.usuario_id)
                                           .where(fotos_usuarios.c.origem == "painel"))}
        for u in c.execute(select(usuarios.c.id, usuarios.c.login, usuarios.c.nome)).mappings():
            if u["id"] in manuais:
                continue
            uri = por_login.get((u["login"] or "").lower()) or por_nome.get(norm_nome(u["nome"] or ""))
            dados = _de_data_uri(uri)
            red = reduzir(dados) if dados else None
            if red and _gravar(c, u["id"], red[0], red[1], "minha_pauta"):
                n += 1
    if n:
        log.info("Fotos: %d copiada(s) do Minha Pauta", n)
    return n


# ---------------- envio pelo administrador ----------------

def salvar(uid, arquivo):
    if not arquivo or not arquivo.filename:
        raise ValueError("Escolha a imagem.")
    ext = arquivo.filename.rsplit(".", 1)[-1].lower() if "." in arquivo.filename else ""
    if ext not in TIPOS:
        raise ValueError("A foto precisa ser JPG, PNG, GIF ou WEBP.")
    dados = arquivo.read(MAX_FOTO + 1)
    if len(dados) > MAX_FOTO:
        raise ValueError("A foto passa de 8 MB.")
    red = reduzir(dados)
    if not red:
        raise ValueError("Não consegui ler a imagem.")
    with engine.begin() as c:
        _gravar(c, uid, red[0], red[1], "painel")


def remover(uid):
    with engine.begin() as c:
        c.execute(delete(fotos_usuarios).where(fotos_usuarios.c.usuario_id == uid))


# ---------------- leitura ----------------

def _fotos():
    if "_fotos" not in g:
        with engine.connect() as c:
            g._fotos = {r[0]: r[1] for r in c.execute(select(fotos_usuarios.c.usuario_id, fotos_usuarios.c.atualizado_em))}
    return g._fotos


def usuarios_ativos():
    """Usuários ativos (para escolher o responsável), com a indicação de foto."""
    if "_usuarios_ativos" not in g:
        with engine.connect() as c:
            lista = [dict(r) for r in c.execute(select(usuarios.c.id, usuarios.c.login, usuarios.c.nome, usuarios.c.equipe)
                                                .where(usuarios.c.ativo.is_(True))).mappings()]
        lista.sort(key=lambda u: norm_nome(u["nome"] or u["login"]))
        g._usuarios_ativos = lista
    return g._usuarios_ativos


def por_login(login):
    """Dados de um usuário pelo login (inclusive inativo), ou None."""
    if not login:
        return None
    if "_por_login" not in g:
        with engine.connect() as c:
            g._por_login = {r["login"]: dict(r) for r in c.execute(
                select(usuarios.c.id, usuarios.c.login, usuarios.c.nome, usuarios.c.ativo)).mappings()}
    return g._por_login.get(login)


def iniciais(nome):
    partes = [p for p in re.split(r"[\s._-]+", nome or "") if p]
    if not partes:
        return "?"
    return (partes[0][0] + (partes[-1][0] if len(partes) > 1 else "")).upper()


def avatar(u, tam=28, classe=""):
    """<img> com a foto do usuário ou um círculo com as iniciais. `u` pode ser o login ou um dict com id/nome."""
    if isinstance(u, str):
        u = por_login(u) or {"id": None, "login": u, "nome": u}
    if not u:
        return Markup("")
    nome = u.get("nome") or u.get("login") or ""
    estilo = f"width:{tam}px;height:{tam}px"
    ver = _fotos().get(u.get("id"))
    if u.get("id") and u.get("id") in _fotos():
        src = url_for("fotos.foto", uid=u["id"], v=int(ver.timestamp()) if ver else 0)
        return Markup(f'<img class="av {escape(classe)}" src="{src}" alt="{escape(nome)}" title="{escape(nome)}" '
                      f'style="{estilo}" loading="lazy">')
    cor = CORES[sum(map(ord, u.get("login") or nome)) % len(CORES)]
    return Markup(f'<span class="av av-ini {escape(classe)}" title="{escape(nome)}" '
                  f'style="{estilo};font-size:{max(10, int(tam * .4))}px;background:{cor}">{escape(iniciais(nome))}</span>')


@bp.app_context_processor
def _ctx():
    return {"avatar": avatar}


@bp.route("/foto/<int:uid>")
def foto(uid):
    with engine.connect() as c:
        r = c.execute(select(fotos_usuarios.c.mime, fotos_usuarios.c.conteudo)
                      .where(fotos_usuarios.c.usuario_id == uid)).first()
    if not r:
        abort(404)
    resp = make_response(bytes(r[1]))
    resp.headers["Content-Type"] = r[0] or "image/jpeg"
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp
