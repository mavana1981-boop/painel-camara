"""Importação dos usuários do app Minha Pauta (plenario-assessoria).

Lê a tabela `users` do banco do Minha Pauta (MINHA_PAUTA_DATABASE_URL, o mesmo usado pela aba Plenário) e
cria no painel cada usuário que ainda não existe, com o mesmo login (em minúsculas), o nome exibido, a mesma
senha e a foto. Para quem já existe com o mesmo login, atualiza o nome e, se a senha mudou no Minha Pauta
desde a última importação, passa a valer a senha de lá (`senha_mp` guarda o último hash importado).

As senhas do Minha Pauta são hashes bcrypt: o painel guarda o hash como veio e o confere no login
(senha_confere); no primeiro login certo, troca pelo hash padrão do painel. Nenhuma senha é lida em texto.

Administradores: depois da importação, só os logins de ADMINS_PAINEL ficam como administradores
(padrão: marcelo.uvara) — e só se pelo menos um deles existir e estiver ativo, para ninguém perder o acesso.

Roda sozinha uma vez quando o app sobe (marca `usuarios_mp:importado` na tabela meta) e pode ser repetida em
Usuários → "Importar do Minha Pauta".
"""
import logging
import os
from datetime import datetime

from sqlalchemy import insert, select, update
from werkzeug.security import check_password_hash, generate_password_hash

from db import engine, meta, norm_nome, usuarios

log = logging.getLogger("usuarios_mp")
MARCA = "usuarios_mp:importado:v2"      # v2: traz também a equipe (Oposição/Minoria) — reimporta uma vez


def admins_do_painel():
    v = os.environ.get("ADMINS_PAINEL", "marcelo.uvara")
    return [x.strip().lower() for x in v.split(",") if x.strip()]


def eh_bcrypt(h):
    return isinstance(h, str) and h[:4] in ("$2a$", "$2b$", "$2y$")


def senha_confere(hash_, senha):
    """Confere a senha no hash do painel (werkzeug) ou num hash bcrypt vindo do Minha Pauta."""
    if not hash_ or not senha:
        return False
    if eh_bcrypt(hash_):
        try:
            import bcrypt
            return bcrypt.checkpw(senha.encode("utf-8"), hash_.encode("utf-8"))
        except Exception:
            return False
    try:
        return check_password_hash(hash_, senha)
    except Exception:
        return False


EQUIPES = [("oposicao", "Oposição"), ("minoria", "Minoria"), ("geral", "Geral"), ("restrito", "Restrito")]


def equipe_de(categoria):
    """Categoria do Minha Pauta → equipe no painel ('admin' de lá conta como geral)."""
    c = (categoria or "").strip().lower()
    return c if c in dict(EQUIPES) else ("geral" if c else None)


def ler_usuarios(conn):
    import plenario
    cols = plenario._colunas(conn, "users")
    if not cols:
        raise ValueError("O banco do Minha Pauta não tem a tabela de usuários.")
    extra = ", ".join(c for c in ("nome_display", "role", "categoria") if c in cols)
    return plenario._consulta(conn, f"SELECT username, password{', ' + extra if extra else ''} FROM users "
                                    "ORDER BY username")


def importar(conn_fonte=None):
    """Importa os usuários. Devolve um resumo {criados, atualizados, admins, ignorados}."""
    import plenario
    if conn_fonte is None:
        if not plenario.configurado():
            raise ValueError("Falta a variável MINHA_PAUTA_DATABASE_URL (a mesma da aba Plenário).")
        with plenario.fonte().connect() as c:
            return importar(c)
    linhas = ler_usuarios(conn_fonte)
    criados, atualizados, ignorados = [], [], []
    agora = datetime.now()
    with engine.begin() as c:
        existentes = {r["login"]: dict(r) for r in c.execute(
            select(usuarios.c.id, usuarios.c.login, usuarios.c.nome, usuarios.c.senha_mp, usuarios.c.equipe)).mappings()}
        for r in linhas:
            login = (r.get("username") or "").strip().lower()
            senha = (r.get("password") or "").strip()
            if not login or not senha:
                ignorados.append(login or "(sem nome)")
                continue
            nome = (r.get("nome_display") or "").strip() or r["username"].strip()
            equipe = equipe_de(r.get("categoria"))
            atual = existentes.get(login)
            if atual is None:
                c.execute(insert(usuarios).values(login=login[:60], nome=nome[:120], senha_hash=senha[:300],
                                                  senha_mp=senha[:300], equipe=equipe, admin=False, ativo=True,
                                                  criado=agora))
                criados.append(login)
            else:
                valores = {}
                if (atual["nome"] or "") != nome:
                    valores["nome"] = nome[:120]
                if equipe and atual["equipe"] != equipe:
                    valores["equipe"] = equipe
                if atual["senha_mp"] != senha:     # senha nova no Minha Pauta (ou 1ª importação): vale a de lá
                    valores.update(senha_hash=senha[:300], senha_mp=senha[:300])
                if valores:
                    c.execute(update(usuarios).where(usuarios.c.id == atual["id"]).values(**valores))
                    atualizados.append(login)
        admins = definir_admins(c)
        c.execute(meta.delete().where(meta.c.chave == MARCA))
        c.execute(insert(meta).values(chave=MARCA, valor=agora.isoformat(timespec="seconds")))
    try:                                   # fotos (casando pelo login ou pelo nome)
        import fotos
        fotos.sincronizar(conn_fonte)
    except Exception:
        log.exception("fotos do Minha Pauta")
    log.info("Usuários do Minha Pauta: %d criado(s), %d atualizado(s); administradores: %s",
             len(criados), len(atualizados), ", ".join(admins) or "sem mudança")
    return {"criados": criados, "atualizados": atualizados, "admins": admins, "ignorados": ignorados}


def definir_admins(c):
    """Deixa como administradores só os logins de ADMINS_PAINEL (casando também pelo nome, ex.: 'Marcelo Uvara')."""
    alvo = admins_do_painel()
    todos = [dict(r) for r in c.execute(select(usuarios.c.id, usuarios.c.login, usuarios.c.nome, usuarios.c.ativo)).mappings()]
    nomes_alvo = {norm_nome(a.replace(".", " ")) for a in alvo}
    escolhidos = [u for u in todos if u["ativo"] and (u["login"] in alvo or norm_nome(u["nome"] or "") in nomes_alvo)]
    if not escolhidos:
        log.warning("Nenhum dos administradores %s existe: administradores mantidos como estavam.", alvo)
        return []
    ids = {u["id"] for u in escolhidos}
    for u in todos:
        c.execute(update(usuarios).where(usuarios.c.id == u["id"]).values(admin=u["id"] in ids))
    return [u["login"] for u in escolhidos]


def ja_importou():
    with engine.connect() as c:
        return c.execute(select(meta.c.valor).where(meta.c.chave == MARCA)).scalar() is not None


def importar_se_preciso():
    """Na subida do app: importa uma vez, se o Minha Pauta estiver configurado."""
    import plenario
    if not plenario.configurado() or ja_importou():
        return None
    try:
        return importar()
    except Exception:
        log.exception("importação dos usuários do Minha Pauta")
        return None


def rehash_se_preciso(uid, hash_, senha):
    """Depois de um login certo com hash bcrypt, grava o hash padrão do painel. Devolve o hash em uso."""
    if not eh_bcrypt(hash_):
        return hash_
    novo = generate_password_hash(senha)
    with engine.begin() as c:
        c.execute(update(usuarios).where(usuarios.c.id == uid).values(senha_hash=novo))
    return novo
