"""Meu Parlamentar: área por deputado, aberta só a quem o administrador vinculou (admin vê todos).

Seções: visão geral, agenda, notificações, solicitações (com anexos), questões de ordem por IA,
radar de notícias, rankings (posição + sugestões) e clipping diário de aprovações.
"""
import io
import logging
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta, timezone

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, send_file,
                   url_for)
from sqlalchemy import and_, delete, insert, or_, select, text, update

import metrics
import paineis
import questoes
from db import (agenda, anexos, deputados, engine, norm_nome, relatorias, solicitacoes,
                temas_radar, usuario_deputados, usuarios)

log = logging.getLogger("meu")
bp = Blueprint("meu", __name__, url_prefix="/meu")

SECOES = [("visao", "Visão geral"), ("agenda", "Agenda"), ("notificacoes", "Notificações"),
          ("solicitacoes", "Solicitações"), ("questoes", "Questões de ordem"), ("radar", "Radar de notícias"),
          ("rankings", "Rankings"), ("clipping", "Clipping")]
STATUS = [("aberta", "Aberta"), ("andamento", "Em andamento"), ("concluida", "Concluída"),
          ("arquivada", "Arquivada")]
EXTENSOES = {"pdf", "doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "ods", "csv", "ppt", "pptx",
             "png", "jpg", "jpeg", "gif", "webp", "zip", "eml", "msg"}
MAX_ANEXO = 10 * 1024 * 1024

# ---------------- acesso ----------------


def deputados_do_usuario(u=None):
    """Códigos que o usuário pode abrir. Admin: todos os deputados em exercício + os vinculados."""
    u = u or g.get("usuario")
    if not u:
        return []
    with engine.connect() as c:
        vinc = {r[0] for r in c.execute(select(usuario_deputados.c.codigo_deputado)
                                        .where(usuario_deputados.c.usuario_id == u["id"]))}
        if u.get("admin"):
            vinc |= {r[0] for r in c.execute(select(deputados.c.codigo).where(deputados.c.em_exercicio))}
    return sorted(vinc)


def vinculos_do_usuario(uid):
    with engine.connect() as c:
        return {r[0] for r in c.execute(select(usuario_deputados.c.codigo_deputado)
                                        .where(usuario_deputados.c.usuario_id == uid))}


def salvar_vinculos(uid, codigos):
    with engine.begin() as c:
        c.execute(delete(usuario_deputados).where(usuario_deputados.c.usuario_id == uid))
        for cod in sorted(set(codigos)):
            c.execute(insert(usuario_deputados).values(usuario_id=uid, codigo_deputado=cod))


def todos_vinculos():
    with engine.connect() as c:
        out = {}
        for uid, cod in c.execute(select(usuario_deputados.c.usuario_id, usuario_deputados.c.codigo_deputado)):
            out.setdefault(uid, set()).add(cod)
        return out


def _exigir_acesso(cod):
    if not _acesso_pleno(cod):
        abort(403)


def _acesso_pleno(cod):
    u = g.get("usuario") or {}
    return bool(u.get("admin")) or cod in vinculos_do_usuario(u.get("id"))


def _tem_tarefa(cod, sid=None):
    """O usuário é responsável por alguma solicitação deste deputado (ou pela solicitação `sid`)?"""
    login = (g.get("usuario") or {}).get("login")
    if not login:
        return False
    q = select(solicitacoes.c.id).where(solicitacoes.c.codigo_deputado == cod,
                                        solicitacoes.c.responsavel == login)
    if sid is not None:
        q = q.where(solicitacoes.c.id == sid)
    with engine.connect() as c:
        return c.execute(q.limit(1)).first() is not None


def tem_acesso_menu():
    u = g.get("usuario")
    if not u:
        return False
    return bool(u.get("admin")) or bool(vinculos_do_usuario(u["id"]))


# ---------------- tarefas atribuídas (aviso na tela inicial) ----------------

def tarefas_do_usuario(login, limite=20):
    """Solicitações abertas ou em andamento atribuídas ao usuário, das mais urgentes para as menos."""
    if not login:
        return []
    with engine.connect() as c:
        rows = [dict(r) for r in c.execute(
            select(solicitacoes.c.id, solicitacoes.c.codigo_deputado, solicitacoes.c.titulo, solicitacoes.c.status,
                   solicitacoes.c.criado_em, solicitacoes.c.area,
                   solicitacoes.c.prazo, solicitacoes.c.data_solicitacao, solicitacoes.c.atribuido_por,
                   solicitacoes.c.atribuido_em, solicitacoes.c.visto_em, deputados.c.nome.label("deputado"))
            .select_from(solicitacoes.outerjoin(deputados, deputados.c.codigo == solicitacoes.c.codigo_deputado))
            .where(solicitacoes.c.responsavel == login, solicitacoes.c.status.in_(("aberta", "andamento")))
        ).mappings()]
    hoje = agora_brt().date()
    for t in rows:
        p = _data(t["prazo"])
        t["dias"] = (p - hoje).days if p else None
        t["atrasada"] = t["dias"] is not None and t["dias"] < 0
        t["nova"] = t["visto_em"] is None
    rows.sort(key=lambda t: (t["prazo"] is None, t["prazo"] or "", -(t["atribuido_em"] or datetime.min).timestamp()))
    return rows[:limite]


# ---------------- utilitários ----------------

def _sem_acento(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))



def eh_aprovacao(situacao):
    from clipping_auto import eh_aprovacao as f
    return f(situacao)


def _data(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


# ---------------- agenda ----------------

BRT = timezone(timedelta(hours=-3))          # Brasília (sem horário de verão)
DIAS_HISTORICO = 60                           # quanto do passado aparece no Histórico
_SIT_FIM = ("encerrad", "cancelad", "finalizad", "realizad", "adiad", "transferid")


# ---------------- preferências do usuário ----------------

def pref(chave, padrao=None):
    import json
    try:
        return json.loads((g.get("usuario") or {}).get("preferencias") or "{}").get(chave, padrao)
    except ValueError:
        return padrao


def salvar_pref(chave, valor):
    import json
    u = g.usuario
    try:
        prefs = json.loads(u.get("preferencias") or "{}")
    except ValueError:
        prefs = {}
    prefs[chave] = valor
    u["preferencias"] = json.dumps(prefs, ensure_ascii=False)
    with engine.begin() as c:
        c.execute(update(usuarios).where(usuarios.c.id == u["id"]).values(preferencias=u["preferencias"]))


def eh_solene(e):
    """Sessão solene (homenagens) do Plenário ou de outro órgão."""
    return "solene" in norm_nome(e.get("titulo") or "").split()


def ocultar_solenes():
    """Escolha da página (?solenes=ocultar|mostrar) ou, sem ela, o padrão salvo pelo usuário."""
    v = request.args.get("solenes")
    if v in ("ocultar", "mostrar"):
        return v == "ocultar"
    return bool(pref("agenda_ocultar_solenes", False))


def agora_brt():
    """Data e hora de Brasília (o servidor do Railway roda em UTC)."""
    return datetime.now(BRT).replace(tzinfo=None)


def ja_passou(r, agora=None):
    """Compromisso que já aconteceu: dia anterior, sessão encerrada/cancelada ou horário já passado.
    Reuniões e sessões ganham 3 h de tolerância (podem estar em andamento); compromissos da equipe não."""
    agora = agora or agora_brt()
    dia, hoje = r.get("data") or "", agora.date().isoformat()
    if dia != hoje:
        return dia < hoje
    sit = (r.get("situacao") or "").lower()
    if "andamento" in sit:
        return False
    if sit.startswith(_SIT_FIM):
        return True
    try:
        inicio = datetime.strptime(f"{dia} {(r.get('hora') or '')[:5]}", "%Y-%m-%d %H:%M")
    except ValueError:                        # sem horário: fica em "próximos" até o fim do dia
        return False
    folga = timedelta(0) if r.get("origem") == "equipe" else timedelta(hours=3)
    return inicio + folga <= agora


DIA_MINIMO = date(2019, 2, 1)                 # início da 56ª legislatura
DIAS_MAX_FUTURO = 365


def dia_consultado(hoje):
    """?dia=AAAA-MM-DD da navegação da agenda (None = visão normal: próximos + histórico)."""
    v = (request.args.get("dia") or "").strip()
    if not v:
        return None
    try:
        d = date.fromisoformat(v)
    except ValueError:
        return None
    return min(max(d, DIA_MINIMO), hoje + timedelta(days=DIAS_MAX_FUTURO))


def separar_agenda(dias, agora=None):
    """Divide a agenda (agrupada por dia) em próximos (ordem cronológica) e histórico (mais recente primeiro)."""
    agora = agora or agora_brt()
    prox, hist = [], []
    for d in dias:
        fut = [e for e in d["itens"] if not ja_passou(e, agora)]
        pas = [e for e in d["itens"] if ja_passou(e, agora)]
        if fut:
            prox.append({"data": d["data"], "itens": fut})
        if pas:
            hist.append({"data": d["data"], "itens": pas[::-1]})
    return prox, hist[::-1]

def agenda_deputado(cod, inicio, fim, so_dele=True, sem_solenes=False):
    s = paineis.deputado(cod) or {}
    import coleta_meu
    cols = coleta_meu.colegiados_do_deputado(cod) | {"PLEN"}
    with engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(agenda).where(
            agenda.c.data >= inicio.isoformat(), agenda.c.data <= fim.isoformat(),
            or_(and_(agenda.c.origem == "camara", agenda.c.codigo_deputado.is_(None)),
                agenda.c.codigo_deputado == cod)).order_by(agenda.c.data, agenda.c.hora, agenda.c.id)).mappings()]
    import alertas
    nome = norm_nome(s.get("nome") or "")
    regras, autorias_cod = alertas.compilar(), alertas.autorias_do(cod)
    out = []
    for r in rows:
        # deputado na pauta (relator ou autor do item, ou nome citado) e termos sensíveis
        alertas.analisar(r, cod, nome, autorias_cod, regras)
        r["membro"] = r["origem"] == "equipe" or (r.get("colegiado") or "") in cols
        if so_dele and not (r["membro"] or r["citado"]):
            continue
        if sem_solenes and r["origem"] != "equipe" and eh_solene(r):
            continue
        out.append(r)
    alertas.completar_ementas(out)
    dias = {}
    for r in out:
        dias.setdefault(r["data"], []).append(r)
    return [{"data": d, "itens": dias[d]} for d in sorted(dias)]


def adicionar_compromisso(cod, f, autor):
    dia = _data(f.get("data"))
    titulo = (f.get("titulo") or "").strip()
    if not dia or not titulo:
        raise ValueError("Informe a data e o título do compromisso.")
    hora = (f.get("hora") or "").strip()[:5] or None
    with engine.begin() as c:
        c.execute(insert(agenda).values(
            origem="equipe", chave=None, codigo_deputado=cod, data=dia.isoformat(), hora=hora,
            titulo=titulo[:300], colegiado=None, local=(f.get("local") or "").strip()[:200] or None,
            situacao=None, descricao=(f.get("descricao") or "").strip() or None,
            criado_por=autor, criado_em=datetime.now()))


def remover_compromisso(cod, aid):
    with engine.begin() as c:
        c.execute(delete(agenda).where(agenda.c.id == aid, agenda.c.codigo_deputado == cod,
                                       agenda.c.origem == "equipe"))


# ---------------- solicitações ----------------

def lista_solicitacoes(cod, status="", responsavel=None):
    q = select(solicitacoes).where(solicitacoes.c.codigo_deputado == cod)
    if responsavel:
        q = q.where(solicitacoes.c.responsavel == responsavel)
    if status:
        q = q.where(solicitacoes.c.status == status)
    with engine.connect() as c:
        itens = [dict(r) for r in c.execute(q.order_by(solicitacoes.c.criado_em.desc(),
                                                       solicitacoes.c.id.desc())).mappings()]
        ids = [i["id"] for i in itens]
        anx = {}
        if ids:
            for r in c.execute(select(anexos.c.id, anexos.c.solicitacao_id, anexos.c.nome, anexos.c.tamanho,
                                      anexos.c.enviado_em).where(anexos.c.solicitacao_id.in_(ids))
                               .order_by(anexos.c.id)).mappings():
                anx.setdefault(r["solicitacao_id"], []).append(dict(r))
        contagem = {k: 0 for k, _ in STATUS}
        filtro_resp = " AND responsavel = :r" if responsavel else ""
        for r in c.execute(text("SELECT status, COUNT(*) n FROM solicitacoes WHERE codigo_deputado = :c"
                                + filtro_resp + " GROUP BY status"), {"c": cod, "r": responsavel}):
            contagem[r[0]] = r[1]
    hoje = agora_brt().date().isoformat()
    for i in itens:
        i["anexos"] = anx.get(i["id"], [])
        i["atrasada"] = bool(i["prazo"] and i["prazo"] < hoje and i["status"] in ("aberta", "andamento"))
        fim = i.get("data_solucao")
        i["no_prazo"] = (fim <= i["prazo"]) if (fim and i["prazo"]) else None
    return {"itens": itens, "contagem": contagem}


def _guardar_anexos(conn, sid, arquivos, autor):
    n = 0
    for arq in arquivos:
        if not arq or not arq.filename:
            continue
        nome = arq.filename.replace("\\", "/").split("/")[-1][:200]
        ext = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
        if ext not in EXTENSOES:
            raise ValueError(f"Tipo de arquivo não aceito: {nome}")
        dados = arq.read(MAX_ANEXO + 1)
        if len(dados) > MAX_ANEXO:
            raise ValueError(f"{nome} passa de 10 MB.")
        conn.execute(insert(anexos).values(solicitacao_id=sid, nome=nome,
                                           mime=arq.mimetype or "application/octet-stream",
                                           tamanho=len(dados), conteudo=dados, enviado_por=autor,
                                           enviado_em=datetime.now()))
        n += 1
    return n


def criar_solicitacao(cod, f, arquivos, autor):
    titulo = (f.get("titulo") or "").strip()
    if not titulo:
        raise ValueError("Informe o título da solicitação.")
    agora = datetime.now()
    datas = _datas_solicitacao(f, padrao_solicitacao=agora_brt().date())
    resp = _responsavel_valido(f.get("responsavel"), f"{titulo} {f.get('descricao') or ''}")
    with engine.begin() as c:
        sid = c.execute(insert(solicitacoes).values(
            codigo_deputado=cod, titulo=titulo[:200], descricao=(f.get("descricao") or "").strip() or None,
            solicitante=(f.get("solicitante") or "").strip()[:120] or None, status="aberta",
            prazo=datas["prazo"], data_solicitacao=datas["data_solicitacao"],
            area=__import__("selecao_ia")._area(f.get("area")) if f.get("area") else None,
            responsavel=resp, atribuido_por=autor if resp else None, atribuido_em=agora if resp else None,
            visto_em=agora if resp and resp == autor else None,
            criado_por=autor, criado_em=agora, atualizado_em=agora)).inserted_primary_key[0]
        _guardar_anexos(c, sid, arquivos, autor)
    return sid


def _iso(v):
    d = _data((v or "").strip())
    return d.isoformat() if d else None


def _datas_solicitacao(f, padrao_solicitacao=None):
    """Data da solicitação, prazo final e data da solução, conferindo a ordem entre elas."""
    prazo, sol = _iso(f.get("prazo")), _iso(f.get("data_solucao"))
    ds = _iso(f.get("data_solicitacao"))
    if not ds and padrao_solicitacao:      # sem data informada: hoje (ou o prazo, se já venceu)
        ds = min([padrao_solicitacao.isoformat()] + [x for x in (prazo, sol) if x])
    if ds and prazo and prazo < ds:
        raise ValueError("O prazo final não pode ser anterior à data da solicitação.")
    if ds and sol and sol < ds:
        raise ValueError("A data da solução não pode ser anterior à data da solicitação.")
    return {"data_solicitacao": ds, "prazo": prazo, "data_solucao": sol}


def _responsavel_valido(login, texto_sorteio=None):
    """Login de usuário ativo; "__sortear__" sorteia pelos critérios da Seleção da IA (comissão × outros)."""
    login = (login or "").strip()
    if login == "__sortear__":
        import selecao_ia as si
        return si.sortear_um(texto_sorteio or "")[1]
    if not login:
        return None
    with engine.connect() as c:
        ok = c.execute(select(usuarios.c.id).where(usuarios.c.login == login, usuarios.c.ativo.is_(True))).first()
    if not ok:
        raise ValueError("Escolha como responsável um usuário ativo do sistema.")
    return login


def _solicitacao_do_deputado(c, cod, sid):
    return c.execute(select(solicitacoes.c.id).where(solicitacoes.c.id == sid,
                                                     solicitacoes.c.codigo_deputado == cod)).first()


def alterar_solicitacao(cod, sid, acao, f, arquivos, autor):
    with engine.begin() as c:
        if not _solicitacao_do_deputado(c, cod, sid):
            abort(404)
        if acao == "status" and f.get("status") in dict(STATUS):
            novo = f["status"]
            atual = c.execute(select(solicitacoes.c.data_solucao, solicitacoes.c.data_solicitacao)
                              .where(solicitacoes.c.id == sid)).first()
            valores = {"status": novo, "atualizado_em": datetime.now()}
            if novo == "concluida" and not atual[0]:
                hoje = agora_brt().date().isoformat()
                valores["data_solucao"] = max(hoje, atual[1] or hoje)
            elif novo in ("aberta", "andamento"):
                valores["data_solucao"] = None          # reaberta: ainda sem solução
            c.execute(update(solicitacoes).where(solicitacoes.c.id == sid).values(**valores))
        elif acao == "responsavel":
            sol = c.execute(select(solicitacoes.c.titulo, solicitacoes.c.descricao, solicitacoes.c.responsavel)
                            .where(solicitacoes.c.id == sid)).first()
            resp = _responsavel_valido(f.get("responsavel"), f"{sol[0]} {sol[1] or ''}")
            if resp != sol[2]:
                agora = datetime.now()
                c.execute(update(solicitacoes).where(solicitacoes.c.id == sid).values(
                    responsavel=resp, atribuido_por=autor if resp else None, atribuido_em=agora if resp else None,
                    visto_em=agora if resp and resp == autor else None, atualizado_em=agora))
                _sincronizar_responsavel_noticia(c, sid, resp)
        elif acao == "concluir":
            atual = c.execute(select(solicitacoes.c.data_solicitacao).where(solicitacoes.c.id == sid)).scalar()
            hoje = agora_brt().date().isoformat()
            sol = _iso(f.get("data_solucao")) or max(hoje, atual or hoje)
            if atual and sol < atual:
                raise ValueError("A data da solução não pode ser anterior à data da solicitação.")
            _guardar_anexos(c, sid, arquivos, autor)
            c.execute(update(solicitacoes).where(solicitacoes.c.id == sid).values(
                status="concluida", data_solucao=sol, resumo_realizado=(f.get("resumo") or "").strip() or None,
                no_clipping=f.get("no_clipping") == "1", atualizado_em=datetime.now()))
        elif acao == "editar":
            datas = _datas_solicitacao(f)
            valores = {"atualizado_em": datetime.now(), **datas}
            if "area" in f:
                import selecao_ia as si
                valores["area"] = si._area(f.get("area")) if f.get("area") else None
            if "resumo_realizado" in f:
                valores["resumo_realizado"] = (f.get("resumo_realizado") or "").strip() or None
                valores["no_clipping"] = f.get("no_clipping") == "1"
            if "responsavel" in f:
                sol = c.execute(select(solicitacoes.c.titulo, solicitacoes.c.descricao).where(solicitacoes.c.id == sid)).first()
                resp = _responsavel_valido(f.get("responsavel"), f"{sol[0]} {sol[1] or ''}")
                antes = c.execute(select(solicitacoes.c.responsavel).where(solicitacoes.c.id == sid)).scalar()
                if resp != antes:
                    agora = datetime.now()
                    valores.update(responsavel=resp, atribuido_por=autor if resp else None,
                                   atribuido_em=agora if resp else None,
                                   visto_em=agora if resp and resp == autor else None)
                    _sincronizar_responsavel_noticia(c, sid, resp)
            if datas["data_solucao"]:
                st = c.execute(select(solicitacoes.c.status).where(solicitacoes.c.id == sid)).scalar()
                if st in ("aberta", "andamento"):
                    valores["status"] = "concluida"     # informou a solução: está concluída
            c.execute(update(solicitacoes).where(solicitacoes.c.id == sid).values(**valores))
        elif acao == "anexar":
            _guardar_anexos(c, sid, arquivos, autor)
            c.execute(update(solicitacoes).where(solicitacoes.c.id == sid).values(atualizado_em=datetime.now()))
        elif acao == "excluir":
            from db import noticias
            c.execute(update(noticias).where(noticias.c.solicitacao_id == sid).values(solicitacao_id=None))
            c.execute(delete(anexos).where(anexos.c.solicitacao_id == sid))
            c.execute(delete(solicitacoes).where(solicitacoes.c.id == sid))
        elif acao == "excluir_anexo":
            c.execute(delete(anexos).where(anexos.c.id == f.get("anexo", type=int),
                                           anexos.c.solicitacao_id == sid))
        if acao in ("status", "concluir", "editar"):
            _sincronizar_noticia(c, sid)


# ---------------- radar ----------------

def radar(cod, termo="", filtro="", busca=""):
    import radar as rd
    itens = rd.lista(cod, termo, filtro, busca=busca)
    with engine.connect() as c:
        temas = [r[0] for r in c.execute(select(temas_radar.c.termo).where(temas_radar.c.codigo_deputado == cod)
                                         .order_by(temas_radar.c.termo))]
        termos = [r[0] for r in c.execute(text("SELECT termo, COUNT(*) n FROM noticias WHERE codigo_deputado = :c "
                                               "GROUP BY termo ORDER BY n DESC"), {"c": cod})]
        cont = c.execute(text(
            "SELECT COUNT(*) n, SUM(CASE WHEN relevante THEN 1 ELSE 0 END) rel, "
            "SUM(CASE WHEN analisado_em IS NULL THEN 1 ELSE 0 END) pend, "
            "SUM(CASE WHEN origem = 'portal' THEN 1 ELSE 0 END) portal, "
            "SUM(CASE WHEN origem = 'google' OR origem IS NULL THEN 1 ELSE 0 END) google, "
            "SUM(CASE WHEN origem = 'manual' THEN 1 ELSE 0 END) manual, "
            "SUM(CASE WHEN relevante AND COALESCE(status, 'Pendente') = 'Pendente' THEN 1 ELSE 0 END) pendentes "
            "FROM noticias WHERE codigo_deputado = :c"), {"c": cod}).mappings().first()
    return {"itens": itens, "temas": temas, "termos": termos,
            "cont": {k: (cont[k] or 0) for k in ("n", "rel", "pend", "portal", "google", "manual", "pendentes")}}


def alterar_temas(cod, acao, termo):
    termo = re.sub(r"\s+", " ", (termo or "")).strip()[:120]
    if not termo:
        return
    with engine.begin() as c:
        c.execute(delete(temas_radar).where(temas_radar.c.codigo_deputado == cod, temas_radar.c.termo == termo))
        if acao == "add":
            c.execute(insert(temas_radar).values(codigo_deputado=cod, termo=termo))


# ---------------- clipping ----------------

def clipping(cod, inicio, fim):
    """Aprovações por data: proposições do deputado (autor/coautor) e matérias que ele relatou
    (item da pauta com parecer aprovado, na data da reunião). Vêm do histórico permanente
    (clipping_auto): uma aprovação continua no clipping mesmo depois que a situação muda."""
    import clipping_auto
    try:
        clipping_auto.registrar(cod)        # o que apareceu desde a última coleta entra já
    except Exception:
        logging.getLogger(__name__).exception("clipping automático")
    eventos = []
    for x in clipping_auto.do_periodo(cod, inicio, fim):
        x["data_situacao"] = x["data"]
        if x["tipo"] == "autoria":
            x["rotulo"] = "Proposição aprovada" if x["principal"] else "Proposição (coautoria) aprovada"
        else:
            x["rotulo"] = "Relatoria aprovada"
        eventos.append(x)
    for x in realizacoes(cod, inicio, fim):
        eventos.append({**x, "tipo": "realizacao", "rotulo": "Solicitação atendida", "data_situacao": x["data_solucao"],
                        "identificacao": x["titulo"]})
    dias = {}
    for e in eventos:
        dias.setdefault(e["data_situacao"], []).append(e)
    ordem = {"realizacao": 0, "autoria": 1, "relatoria": 2}
    saida = []
    for d in sorted(dias, reverse=True):
        itens = sorted(dias[d], key=lambda e: ordem[e["tipo"]])
        saida.append({"data": d, "itens": itens, "blocos": blocos_clipping(itens)})
    return {"dias": saida,
            "total": len(eventos), "autoria": sum(e["tipo"] == "autoria" for e in eventos),
            "relatoria": sum(e["tipo"] == "relatoria" for e in eventos),
            "realizacao": sum(e["tipo"] == "realizacao" for e in eventos)}


def blocos_clipping(itens):
    """Itens de um dia → blocos da tela: cada solicitação atendida sozinha; aprovações do mesmo
    colegiado (comissão, Plenário…) num quadro só. Aprovação sem colegiado conhecido vai para um
    quadro "Câmara dos Deputados" quando há mais de uma."""
    blocos, grupos = [], {}
    for e in itens:
        if e["tipo"] == "realizacao":
            blocos.append({"grupo": False, "item": e})
            continue
        col = (e.get("colegiado") or "").strip() or None
        sit = e.get("situacao") or ""
        if col and sit.endswith(f" ({col})"):             # o colegiado já está no cabeçalho do quadro
            sit = sit[: -len(col) - 3]
        e["situacao_curta"] = sit
        if col not in grupos:
            grupos[col] = {"grupo": True, "colegiado": col, "itens": []}
            blocos.append(grupos[col])
        grupos[col]["itens"].append(e)
    out = []
    for b in blocos:
        if b["grupo"] and len(b["itens"]) == 1:
            out.append({"grupo": False, "item": b["itens"][0]})
        else:
            out.append(b)
    # quadros de colegiado: Plenário primeiro, depois as comissões em ordem alfabética, sem colegiado por último
    def chave(b):
        if not b["grupo"] and b["item"]["tipo"] == "realizacao":
            return (0, "")
        c = b["colegiado"] if b["grupo"] else ((b["item"].get("colegiado") or "").strip() or None)
        return (1, "") if c == "PLEN" else (2, c) if c else (3, "")
    return sorted(out, key=chave)


def realizacoes(cod, inicio, fim):
    """Solicitações concluídas no período (pela data da solução) que entram no clipping, com os anexos."""
    with engine.connect() as c:
        itens = [dict(r) for r in c.execute(
            select(solicitacoes.c.id, solicitacoes.c.titulo, solicitacoes.c.descricao, solicitacoes.c.resumo_realizado,
                   solicitacoes.c.solicitante, solicitacoes.c.responsavel, solicitacoes.c.data_solicitacao,
                   solicitacoes.c.data_solucao)
            .where(solicitacoes.c.codigo_deputado == cod, solicitacoes.c.status == "concluida",
                   or_(solicitacoes.c.no_clipping.is_(None), solicitacoes.c.no_clipping.is_(True)),
                   solicitacoes.c.data_solucao >= inicio.isoformat(), solicitacoes.c.data_solucao <= fim.isoformat())
            .order_by(solicitacoes.c.data_solucao.desc(), solicitacoes.c.id.desc())).mappings()]
        ids = [i["id"] for i in itens]
        anx = {}
        if ids:
            for r in c.execute(select(anexos.c.id, anexos.c.solicitacao_id, anexos.c.nome, anexos.c.tamanho)
                               .where(anexos.c.solicitacao_id.in_(ids)).order_by(anexos.c.id)).mappings():
                anx.setdefault(r["solicitacao_id"], []).append(dict(r))
    for i in itens:
        i["anexos"] = anx.get(i["id"], [])
    return itens


def eventos_relatorio(cod, inicio, fim, com_aprovacoes=True):
    """Lista plana para o relatório de realizações em PDF."""
    import fotos
    clip = clipping(cod, inicio, fim)
    out = []
    for d in clip["dias"]:
        for e in d["itens"]:
            if e["tipo"] != "realizacao" and not com_aprovacoes:
                continue
            if e["tipo"] == "realizacao":
                resp = fotos.por_login(e.get("responsavel")) if e.get("responsavel") else None
                out.append({"tipo": "realizacao", "data": e["data_solucao"], "titulo": e["titulo"],
                            "resumo": e.get("resumo_realizado") or e.get("descricao"),
                            "solicitante": e.get("solicitante"),
                            "responsavel": (resp or {}).get("nome") or e.get("responsavel"),
                            "anexos": [a["nome"] for a in e["anexos"]]})
            else:
                titulo = e["identificacao"] + (f" ({e['colegiado']})" if e.get("colegiado") else "")
                out.append({"tipo": e["tipo"], "data": e["data_situacao"], "titulo": titulo, "rotulo": e["rotulo"],
                            "resumo": e.get("situacao"), "ementa": e.get("ementa")})
    return out


def lista_relatorias(cod):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(select(relatorias).where(relatorias.c.codigo_deputado == cod)
                                           .order_by(relatorias.c.data_designacao.desc())).mappings()]


# ---------------- rankings + sugestões ----------------

def _mil(v):
    """1234567.8 → '1.234.568' (sem trocar outras vírgulas da frase)."""
    return f"{v:,.0f}".replace(",", ".")


def _passos_para(seguiu, total, alvo_pct):
    """Quantos acertos seguidos levam o percentual (seguiu/total) acima de alvo_pct."""
    p = (alvo_pct or 0) / 100
    if total and seguiu / total > p:
        return 0
    if p >= 1:
        return None
    k = (p * total - seguiu) / (1 - p)
    return max(int(k) + 1, 1)


def _posicao(lista, cod, chave):
    em = [r for r in lista if r.get("em_exercicio") is not False and r.get("codigo")]
    for i, r in enumerate(em):
        if r["codigo"] == cod:
            acima = em[i - 1] if i > 0 else None
            return {"pos": i + 1, "de": len(em), "valor": r.get(chave), "linha": r,
                    "acima": acima, "lider": em[0]}
    return None


def rankings(cod):
    f_op = metrics.Filtros(opostas=True)
    f = metrics.Filtros(opostas=False)
    res = []

    def _add(slug, nome, lista, chave, fmt, sentido="maior"):
        res.append({"slug": slug, "nome": nome, "fmt": fmt, "sentido": sentido,
                    "posicao": _posicao(lista, cod, chave), "chave": chave})

    _add("oposicionismo", "Oposicionismo", metrics.alinhamento("Oposição", f_op, minimo=10), "pct", "pct")
    _add("participacao", "Participação em votações", metrics.participacao(f), "pct", "pct")
    _add("projetos", "Projetos como 1º autor", paineis.ranking_proposicoes(), "n", "num")
    _add("economia", "Economia da cota", paineis.ranking_economia(), "pct", "pct")
    _add("fidelidade", "Fidelidade ao partido", [r for r in metrics.fidelidade(f) if r["total"] >= 10], "pct", "pct")
    _add("governismo", "Governismo", metrics.alinhamento("Governo", f_op, minimo=10), "pct", "pct", "neutro")
    for r in res:
        r["sugestoes"] = sugestoes(cod, r)
    return res


def sugestoes(cod, r):
    pos = r["posicao"]
    if not pos:
        return ["Ainda não há dados suficientes deste deputado neste ranking (mínimo de 10 votações, quando se aplica)."]
    linha, acima = pos["linha"], pos["acima"]
    out = []
    if r["slug"] == "oposicionismo":
        if acima:
            k = _passos_para(linha["seguiu"], linha["total"], acima["pct"])
            if k:
                out.append(f"Acompanhando a orientação da Oposição nas próximas {k} votações com orientações opostas, "
                           f"passa {acima['nome']} e sobe para {pos['pos'] - 1}º.")
        else:
            out.append("Lidera o ranking. Manter o voto alinhado nas votações com orientações opostas preserva a posição.")
        if linha.get("contra"):
            vo = paineis.votos_oposicao(cod, metrics.Filtros(opostas=True))
            mats = ", ".join(f"{v['sigla_materia']} {v['numero_materia']}/{v['ano_materia']}" for v in vo["contra"][:4]
                             if v.get("sigla_materia"))
            out.append(f"Votou contra a orientação da Oposição {linha['contra']} vez(es)"
                       + (f" — as mais recentes: {mats}." if mats else "."))
    elif r["slug"] == "participacao":
        faltas = linha["realizadas"] - linha["votou"]
        if acima:
            k = _passos_para(linha["votou"], linha["realizadas"], acima["pct"])
            if k:
                out.append(f"Registrando voto nas próximas {k} votações nominais, passa {acima['nome']} "
                           f"e sobe para {pos['pos'] - 1}º.")
        if faltas:
            out.append(f"Deixou de votar em {faltas} de {linha['realizadas']} votações nominais. Acompanhar a pauta do "
                       f"Plenário na aba Agenda ajuda a não perder votações nominais.")
    elif r["slug"] == "projetos":
        if acima and acima["n"] >= linha["n"]:
            out.append(f"Com mais {acima['n'] - linha['n'] + 1} projeto(s) como 1º autor, passa {acima['nome']} "
                       f"({acima['n']}) e sobe para {pos['pos'] - 1}º. O líder tem {pos['lider']['n']}.")
        props = paineis.proposicoes(cod, situacao="tramitando", limite=1000)["itens"]
        parados = [p for p in props if (p.get("dias_parado") or 0) >= 180 and p["principal"]]
        sem_rel = [p for p in parados if "relator" in _sem_acento(p.get("situacao")) and "aguard" in _sem_acento(p.get("situacao"))]
        if parados:
            out.append(f"{len(parados)} proposição(ões) de sua autoria estão paradas há 180 dias ou mais"
                       + (f", {len(sem_rel)} aguardando designação de relator" if sem_rel else "")
                       + ". Pedir a designação de relator ou a inclusão em pauta ajuda a convertê-las em aprovações.")
    elif r["slug"] == "economia":
        c = paineis.cota_deputado(cod)
        if acima and c and c["total"]["disponivel"]:
            disp, econ = c["total"]["disponivel"], c["total"]["economia"]
            falta = acima["pct"] / 100 * disp - econ
            if falta > 0:
                out.append(f"Gastando R$ {_mil(falta)} a menos no período, passa {acima['nome']} e sobe para "
                           f"{pos['pos'] - 1}º.")
        if c and c["categorias"]:
            top = c["categorias"][0]
            out.append(f"A maior despesa é \"{top['tipo']}\" (R$ {_mil(top['total'])}); é onde a economia rende mais.")
    elif r["slug"] == "fidelidade":
        out.append(f"Votou com a orientação do próprio partido (ou da federação/bloco) em {linha['seguiu']} de "
                   f"{linha['total']} votações.")
    elif r["slug"] == "governismo":
        out.append(f"Votou com a orientação do Governo em {linha['seguiu']} de {linha['total']} votações com orientações "
                   f"opostas. Este ranking é informativo; para a Oposição, a posição mais baixa é a mais coerente.")
    if not acima and r["sentido"] == "maior" and not any("Lidera" in x for x in out):
        out.insert(0, f"Lidera este ranking entre {pos['de']} deputado{'s' if pos['de'] != 1 else ''} em exercício.")
    return out


# ---------------- notificações agrupadas ----------------

SIGLAS_TIPO = {"REQ": "Requerimento", "RIC": "Requerimento de Informação", "INC": "Indicação", "PL": "Projeto de Lei",
               "PLP": "Projeto de Lei Complementar", "PEC": "Proposta de Emenda à Constituição",
               "PDL": "Projeto de Decreto Legislativo", "PRC": "Projeto de Resolução", "MPV": "Medida Provisória"}


def _tipo_notif(n):
    sigla = (n.get("sigla") or (n.get("identificacao") or "").split(" ")[0] or "").upper()
    nome = (n.get("tipo_documento") or "").strip() or SIGLAS_TIPO.get(sigla) or sigla or "Outros"
    return sigla, nome


def notificacoes_agrupadas(cod, dias=0):
    """Notificações por tipo de proposição (ex.: Requerimento de Urgência) > proposição > mudanças,
    e também por data. `dias` = 0 traz os 180 dias guardados."""
    base = paineis.notificacoes(cod, limite=3000)
    itens = base["itens"]
    if dias:
        corte = datetime.now() - timedelta(days=dias)
        itens = [n for n in itens if n["detectado_em"] and n["detectado_em"] >= corte]
    tipos, props = {}, {}
    for n in itens:                                   # vêm do mais recente para o mais antigo
        sigla, nome = _tipo_notif(n)
        t = tipos.setdefault(nome, {"nome": nome, "sigla": sigla, "props": [], "mudancas": 0, "recentes": 0,
                                    "ultima": n["detectado_em"]})
        t["mudancas"] += 1
        t["recentes"] += n["recente"]
        chave = n.get("processo_id") or n.get("identificacao")
        p = props.get((nome, chave))
        if not p:
            p = props[(nome, chave)] = {"identificacao": n["identificacao"], "url": n.get("url"), "ementa": n.get("ementa"),
                                        "atual": n.get("situacao_nova"), "ultima": n["detectado_em"],
                                        "recente": n["recente"], "eventos": []}
            t["props"].append(p)
        p["eventos"].append(n)
    for t in tipos.values():
        t["n_props"] = len(t["props"])
    ordem = sorted(tipos.values(), key=lambda t: (-(t["recentes"] > 0), -(t["ultima"] or datetime.min).timestamp()))
    dias_l = {}
    for n in itens:
        d = n["detectado_em"].date().isoformat() if n["detectado_em"] else ""
        dias_l.setdefault(d, []).append(n)
    por_data = [{"data": d, "itens": v, "tipos": sorted({_tipo_notif(x)[0] for x in v if _tipo_notif(x)[0]})}
                for d, v in sorted(dias_l.items(), reverse=True)]
    return {"tipos": ordem, "datas": por_data, "total": len(itens),
            "recentes": sum(1 for n in itens if n["recente"])}


# ---------------- radar → solicitações ----------------

STATUS_RADAR_SOL = {"Pendente": "aberta", "Em análise": "andamento", "Ação efetuada": "concluida"}


def radar_item(cod, nid, f, autor):
    """Acompanhamento de uma notícia do radar (qualquer notícia, não só as que a IA marcou).
    Com responsável escolhido, a notícia vira uma solicitação do parlamentar (com o aviso de tarefa na tela
    inicial do responsável); status e ação efetuada seguem para a solicitação — "Ação efetuada" a conclui e
    leva ao clipping. Devolve o id da solicitação (ou None)."""
    import radar as rd
    from db import noticias
    status = f.get("status") if f.get("status") in rd.STATUS else "Pendente"
    with engine.connect() as c:
        n0 = c.execute(select(noticias.c.titulo, noticias.c.tipo_acao, noticias.c.acao_sugerida, noticias.c.solicitacao_id)
                       .where(noticias.c.id == nid)).first()
    pedir = f.get("criar") == "1" and n0 is not None and not n0[3]
    escolha = f.get("responsavel") or ("__sortear__" if pedir else "")
    resp = _responsavel_valido(escolha, " ".join(str(x or "") for x in (n0 or [])[:3]) + " " + (f.get("tarefa") or ""))
    feita = (f.get("acao_efetuada") or "").strip()[:4000] or None
    prazo = _iso(f.get("prazo"))
    agora, hoje = datetime.now(), agora_brt().date().isoformat()
    with engine.begin() as c:
        n = c.execute(select(noticias).where(noticias.c.id == nid, noticias.c.codigo_deputado == cod)).mappings().first()
        if not n:
            abort(404)
        sid = n["solicitacao_id"]
        if sid and not c.execute(select(solicitacoes.c.id).where(solicitacoes.c.id == sid)).first():
            sid = None                                   # a solicitação foi excluída
        if not sid and (resp or pedir):
            alts = rd.alternativas_de(dict(n))
            desc = "\n\n".join(x for x in (
                f"Notícia: {n['titulo']}", n["link"], n.get("resumo"),
                (f.get("tarefa") or "").strip() and f"O que fazer: {f.get('tarefa').strip()}",
                rd.texto_alternativas(alts)) if x)
            sid = c.execute(insert(solicitacoes).values(
                codigo_deputado=cod, titulo=("Radar: " + (n["titulo"] or "notícia"))[:200], descricao=desc,
                solicitante="Radar de notícias", status="aberta", data_solicitacao=hoje, prazo=prazo,
                responsavel=resp, atribuido_por=autor, atribuido_em=agora, visto_em=agora if resp == autor else None,
                criado_por=autor, criado_em=agora, atualizado_em=agora)).inserted_primary_key[0]
        if sid:
            atual = c.execute(select(solicitacoes).where(solicitacoes.c.id == sid)).mappings().first()
            v = {"atualizado_em": agora}
            if resp and resp != atual["responsavel"]:      # reatribuição: novo aviso para o novo responsável
                v.update(responsavel=resp, atribuido_por=autor, atribuido_em=agora,
                         visto_em=agora if resp == autor else None)
            if prazo and prazo != atual["prazo"]:
                v["prazo"] = prazo
            novo_st = STATUS_RADAR_SOL[status]
            if novo_st != atual["status"] and atual["status"] != "arquivada":
                v["status"] = novo_st
                if novo_st == "concluida":
                    v.update(data_solucao=atual["data_solucao"] or max(hoje, atual["data_solicitacao"] or hoje),
                             no_clipping=True if atual["no_clipping"] is None else atual["no_clipping"])
                else:
                    v["data_solucao"] = None
            if feita and feita != atual["resumo_realizado"]:
                v["resumo_realizado"] = feita
            c.execute(update(solicitacoes).where(solicitacoes.c.id == sid).values(**v))
        nome_resp = (__import__("fotos").por_login(resp) or {}).get("nome") if resp else None
        c.execute(update(noticias).where(noticias.c.id == nid).values(
            status=status, assessor=(resp and (nome_resp or resp)) or (f.get("assessor") or "").strip()[:120] or n["assessor"],
            acao_efetuada=feita, solicitacao_id=sid))
    return sid


def _q_resp(sid):
    with engine.connect() as c:
        login = c.execute(select(solicitacoes.c.responsavel).where(solicitacoes.c.id == sid)).scalar()
    u = __import__("fotos").por_login(login) if login else None
    return (u or {}).get("nome") or login


def _sincronizar_responsavel_noticia(c, sid, resp):
    """Troca de responsável na solicitação vale também para a notícia do radar que a originou."""
    from db import noticias
    u = __import__("fotos").por_login(resp) if resp else None
    c.execute(update(noticias).where(noticias.c.solicitacao_id == sid)
              .values(assessor=((u or {}).get("nome") or resp) if resp else None))


def _sincronizar_noticia(c, sid):
    """Solicitação que veio do radar mudou de status/resumo: a notícia acompanha."""
    from db import noticias
    s = c.execute(select(solicitacoes.c.status, solicitacoes.c.resumo_realizado).where(solicitacoes.c.id == sid)).first()
    if not s:
        return
    st = {"aberta": "Pendente", "andamento": "Em análise", "concluida": "Ação efetuada"}.get(s[0])
    v = {"status": st} if st else {}
    if s[1]:
        v["acao_efetuada"] = s[1]
    if v:
        c.execute(update(noticias).where(noticias.c.solicitacao_id == sid).values(**v))


# ---------------- visão geral ----------------

def visao(cod):
    hoje = agora_brt().date()
    ag, _ = separar_agenda(agenda_deputado(cod, hoje, hoje + timedelta(days=7), sem_solenes=ocultar_solenes()))
    return {
        "agenda": ag[:4],
        "notif": paineis.notificacoes(cod, limite=6),
        "clip": clipping(cod, hoje - timedelta(days=30), hoje),
        "solic": lista_solicitacoes(cod, "")["contagem"],
        "radar": (radar(cod, filtro="relevantes")["itens"] or radar(cod)["itens"])[:6],
        "questoes": questoes.historico(cod, 3),
    }


# ---------------- coleta sob demanda ----------------
_lock = threading.Lock()


def atualizar_em_segundo_plano(cod):
    if not _lock.acquire(blocking=False):
        return False

    def _rodar():
        try:
            import coleta_meu
            coleta_meu.executar([cod])
        except Exception:
            log.exception("atualização sob demanda")
        finally:
            _lock.release()
    threading.Thread(target=_rodar, daemon=True).start()
    return True


# ---------------- rotas ----------------

TELAS_INICIAIS = {"orientacoes", "meu.inicio"}


@bp.app_context_processor
def _ctx():
    u = g.get("usuario")
    if not u:
        return {"menu_meu": False}
    if "_tarefas" not in g:
        g._tarefas = tarefas_do_usuario(u.get("login"))
    ep = request.endpoint or ""
    inicial = ep in TELAS_INICIAIS or (ep == "meu.painel" and request.args.get("secao", "visao") == "visao")
    return {"menu_meu": tem_acesso_menu() or bool(g._tarefas), "minhas_tarefas": g._tarefas,
            "mostrar_tarefas": inicial and request.method == "GET"}


def _deputados_com_tarefa(login):
    with engine.connect() as c:
        return sorted({r[0] for r in c.execute(select(solicitacoes.c.codigo_deputado)
                                               .where(solicitacoes.c.responsavel == login))})


@bp.route("/")
def inicio():
    cods = deputados_do_usuario()
    if not cods:
        tarefas = _deputados_com_tarefa(g.usuario.get("login"))
        if not tarefas:
            abort(403)
        if len(tarefas) == 1:
            return redirect(url_for("meu.painel", cod=tarefas[0], secao="solicitacoes"))
        lista = [s for s in paineis.lista_deputados() if s["codigo"] in set(tarefas)]
        return render_template("meu_escolher.html", aba="meu", lista=lista, vinc=set(), so_tarefas=True)
    u = g.usuario
    vinc = vinculos_do_usuario(u["id"])
    if len(vinc) == 1:
        return redirect(url_for("meu.painel", cod=next(iter(vinc))))
    if not u.get("admin") and len(cods) == 1:
        return redirect(url_for("meu.painel", cod=cods[0]))
    lista = [s for s in paineis.lista_deputados() if s["codigo"] in set(cods)]
    lista.sort(key=lambda s: (s["codigo"] not in vinc, s["nome"]))
    return render_template("meu_escolher.html", aba="meu", lista=lista, vinc=vinc)


@bp.route("/<int:cod>", methods=["GET", "POST"])
def painel(cod):
    pleno = _acesso_pleno(cod)
    if not pleno and not _tem_tarefa(cod):
        abort(403)
    s = paineis.deputado(cod)
    if not s:
        abort(404)
    secao = request.args.get("secao", "visao")
    if secao not in dict(SECOES):
        secao = "visao"
    if not pleno and secao != "solicitacoes":   # quem só recebeu tarefa vê apenas as próprias solicitações
        return redirect(url_for("meu.painel", cod=cod, secao="solicitacoes"))
    autor = g.usuario.get("login")
    if request.method == "POST":
        return _post(cod, secao, s, autor, pleno)
    hoje = agora_brt().date()
    meus = set(deputados_do_usuario()) | (set() if pleno else {cod})
    ctx = {"aba": "meu", "s": s, "cod": cod, "secao": secao, "pleno": pleno, "status": STATUS,
           "secoes": SECOES if pleno else [x for x in SECOES if x[0] == "solicitacoes"],
           "meus": [x for x in paineis.lista_deputados() if x["codigo"] in meus],
           "hoje": hoje}
    if secao == "visao":
        ctx["v"] = visao(cod)
    elif secao == "agenda":
        import coleta_meu
        ctx["releitura"] = coleta_meu.reler_agenda_ao_acessar()      # sempre que a agenda é aberta
        ult = coleta_meu.ultima_agenda_proxima()
        ctx["agenda_lida_em"] = ult.isoformat(timespec="seconds") if ult else ""
        ctx["agenda_lida_min"] = int((datetime.now() - ult).total_seconds() // 60) if ult else None
        so_dele = request.args.get("todos") != "1"
        ocultar = ocultar_solenes()
        dia = dia_consultado(hoje)
        ctx["dia"] = dia
        ref = dia or hoje
        ctx["dia_nav"] = {k: (ref + timedelta(days=n)).isoformat()
                          for k, n in (("sem_ant", -7), ("ant", -1), ("prox", 1), ("sem_prox", 7))}
        if dia:   # navegação: um dia qualquer, para trás ou para a frente
            ctx["dia_lendo"] = coleta_meu.ler_dia_ao_acessar(dia, hoje)
            ult_dia = coleta_meu.dia_lido_em(dia)
            ctx["dia_lido_em"] = ult_dia.isoformat(timespec="seconds") if ult_dia else ""
            ctx["dia_coberto"] = coleta_meu.dia_coberto(dia, hoje)
            agora = agora_brt()
            itens = [e for d in agenda_deputado(cod, dia, dia, so_dele, ocultar) for e in d["itens"]]
            for e in itens:
                e["passou"] = ja_passou(e, agora)
            ctx["itens_dia"] = itens
            if ocultar:
                ctx["n_solenes"] = sum(1 for d in agenda_deputado(cod, dia, dia, so_dele) for e in d["itens"]
                                       if e["origem"] != "equipe" and eh_solene(e))
            ctx["agenda"], ctx["historico"] = [], []
        else:
            ini_ag, fim_ag = hoje - timedelta(days=DIAS_HISTORICO), hoje + timedelta(days=45)
            ctx["agenda"], ctx["historico"] = separar_agenda(agenda_deputado(cod, ini_ag, fim_ag, so_dele, ocultar))
        if ocultar and not dia:   # quantas ficaram de fora (só as que ainda vão acontecer)
            futuras = separar_agenda(agenda_deputado(cod, ini_ag, fim_ag, so_dele))[0]
            ctx["n_solenes"] = sum(1 for d in futuras for e in d["itens"] if e["origem"] != "equipe" and eh_solene(e))
        ctx["ocultar_solenes"] = ocultar
        ctx["termos_alerta"] = __import__("alertas").termos()
        ctx["padrao_solenes"] = bool(pref("agenda_ocultar_solenes", False))
        ctx["n_historico"] = sum(len(d["itens"]) for d in ctx["historico"])
        ctx["dias_historico"] = DIAS_HISTORICO
        import agenda_pdf as ap
        ctx["periodos_impressao"] = ap.PERIODOS
        ctx["so_dele"] = so_dele
    elif secao == "notificacoes":
        ctx["modo_notif"] = request.args.get("modo") if request.args.get("modo") in ("tipo", "data") \
            else pref("notif_modo", "tipo")
        ctx["periodo_notif"] = request.args.get("periodo", type=int) if request.args.get("periodo", type=int) in (7, 30) else 0
        ctx["notif"] = notificacoes_agrupadas(cod, ctx["periodo_notif"])
        ctx["padrao_notif"] = pref("notif_modo", "tipo")
    elif secao == "solicitacoes":
        ctx["filtro_status"] = request.args.get("status", "")
        ctx["sol"] = lista_solicitacoes(cod, ctx["filtro_status"], None if pleno else autor)
        ctx["usuarios_resp"] = __import__("fotos").usuarios_ativos()
        ctx["areas"] = __import__("selecao_ia").AREAS
        ctx["abrir"] = request.args.get("abrir", type=int)
        with engine.begin() as c:     # o responsável abriu a lista: as tarefas dele deixam de ser "novas"
            c.execute(update(solicitacoes).where(solicitacoes.c.codigo_deputado == cod,
                                                 solicitacoes.c.responsavel == autor,
                                                 solicitacoes.c.visto_em.is_(None)).values(visto_em=datetime.now()))
    elif secao == "questoes":
        ctx["hist"] = questoes.historico(cod)
        ctx["ia_ok"] = questoes.configurado()
        reg = questoes.regimento()
        ctx["regimento"] = {"nome": reg["nome"], "atualizado": reg["atualizado"],
                            "artigos": len(questoes.artigos(reg["texto"]))} if reg else None
        ctx["gerando"] = any(q["resposta"] is None for q in ctx["hist"])
    elif secao == "radar":
        import radar as rd
        ctx["termo"] = request.args.get("termo", "")
        ctx["filtro"] = request.args.get("filtro", "")
        ctx["busca"] = (request.args.get("q") or "").strip()[:200]
        ctx["radar"] = radar(cod, ctx["termo"], ctx["filtro"], ctx["busca"])
        ctx["web"] = None
        if ctx["busca"] and request.args.get("web") == "1":      # mesma busca, na hora, no Google Notícias
            try:
                ctx["web"] = rd.buscar_google(cod, ctx["busca"])
            except Exception as e:
                logging.getLogger("radar").warning("busca avulsa no Google Notícias: %s", e)
                ctx["web_erro"] = True
                ctx["web"] = []
        ctx["fontes"] = rd.lista_fontes()
        ctx["ia_ok"] = questoes.configurado()
        ctx["status_radar"] = rd.STATUS
        ctx["categorias_radar"] = rd.CATEGORIAS
        import prelista as pl
        import selecao_ia as si
        ctx["sel_pend"] = si.n_pendentes(cod)
        ctx["pre_n"] = pl.contagem(cod)
        ctx["pre_noticias"] = pl.noticias_na_lista(cod)
        if ctx["filtro"] == "prelista":
            ctx["pre_aberta"] = pl.aberta(cod)               # junta tudo na pré-lista que a chefia já recebeu
            ctx["pre_itens"] = pl.itens(cod)
            ctx["pre_envios"] = pl.envios(cod)
            if ctx["pre_aberta"]:
                e = ctx["pre_aberta"]
                e["link"] = pl.link_publico(e)
                e["whatsapp"] = "https://wa.me/?text=" + __import__("urllib.parse").parse.quote(pl.texto_whatsapp(e, cod))
                e["depois"] = sum(1 for it in e["itens"] if it["incluido_em"] and it["incluido_em"] > e["criado_em"])
            ctx["pre_novo"] = request.args.get("envio", type=int)
            ctx["sel_equipes"], ctx["sel_faltam"] = si.equipes(), si.nao_encontrados()
        if ctx["filtro"] == "selecao":
            ctx["lotes"] = si.lotes(cod)
            ctx["sel_equipes"], ctx["sel_tipos"] = si.equipes(), si.TIPOS
            ctx["sel_faltam"], ctx["sel_horas"] = si.nao_encontrados(), si.HORAS
            ctx["sel_sortear"] = si.SORTEAR
            ctx["sel_estado"] = si.estado(cod)
        ctx["usuarios_resp"] = __import__("fotos").usuarios_ativos()
    elif secao == "rankings":
        ctx["ranks"] = rankings(cod)
    elif secao == "clipping":
        ini = _data(request.args.get("inicio")) or hoje - timedelta(days=30)
        fim = _data(request.args.get("fim")) or hoje
        ctx["buscando_aprov"] = __import__("clipping_auto").buscando(cod)
        ctx.update(clip=clipping(cod, ini, fim), ini=ini, fim=fim, rels=lista_relatorias(cod),
                   anos_relatorio=list(range(hoje.year, 2022, -1)))
    return render_template("meu.html", **ctx)


def _post(cod, secao, s, autor, pleno=True):
    f, destino = request.form, url_for("meu.painel", cod=cod, secao=secao)
    acao = f.get("acao", "")
    if not pleno:   # responsável sem vínculo: só mexe nas próprias tarefas (status, datas, anexos)
        if secao != "solicitacoes" or acao not in ("status", "anexar", "editar", "concluir", "excluir_anexo") \
                or not _tem_tarefa(cod, f.get("id", type=int)):
            abort(403)
        f = f.copy()
        f.pop("responsavel", None)
    try:
        if acao == "pref_solenes":
            ocultar = f.get("valor") == "ocultar"
            salvar_pref("agenda_ocultar_solenes", ocultar)
            flash("Padrão salvo: sessões solenes " + ("ocultas" if ocultar else "visíveis") + " na agenda.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="agenda", todos=request.args.get("todos") or None,
                              dia=request.args.get("dia") or None)
        elif acao == "pref_notif" and f.get("valor") in ("tipo", "data"):
            salvar_pref("notif_modo", f["valor"])
            flash("Padrão salvo para as notificações.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="notificacoes", periodo=request.args.get("periodo") or None)
        elif secao == "clipping" and acao == "buscar_aprovacoes":
            import clipping_auto
            if clipping_auto.buscar_em_segundo_plano(cod):
                flash("Buscando na Câmara as aprovações dos últimos 7 dias. Recarregue a página em alguns minutos.", "ok")
            else:
                flash("A busca das aprovações já está em andamento.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="clipping")
        elif acao == "atualizar":
            ok = atualizar_em_segundo_plano(cod)
            flash("Atualização iniciada. Recarregue a página em alguns minutos." if ok else
                  "Já há uma atualização em andamento.", "ok" if ok else "erro")
        elif secao == "agenda" and acao == "novo":
            adicionar_compromisso(cod, f, autor)
            flash("Compromisso incluído na agenda.", "ok")
        elif secao == "agenda" and acao in ("termo_add", "termo_del"):
            import alertas
            if acao == "termo_add":
                alertas.adicionar(f.get("termo"), autor)
                flash(f"Termo “{f.get('termo', '').strip()}” incluído nos alertas da agenda.", "ok")
            else:
                alertas.remover(f.get("termo"))
                flash(f"Termo “{f.get('termo')}” removido.", "ok")
        elif secao == "agenda" and acao == "remover":
            remover_compromisso(cod, f.get("id", type=int))
        elif secao == "solicitacoes" and acao == "nova":
            criar_solicitacao(cod, f, request.files.getlist("arquivos"), autor)
            flash("Solicitação registrada.", "ok")
            if not f.get("area"):
                __import__("areas").classificar_em_segundo_plano()
        elif secao == "solicitacoes" and acao in ("status", "anexar", "editar", "concluir", "excluir", "excluir_anexo",
                                                   "responsavel"):
            sid = f.get("id", type=int)
            alterar_solicitacao(cod, sid, acao, f, request.files.getlist("arquivos"), autor)
            if acao == "anexar":
                flash("Arquivos anexados.", "ok")
            elif acao == "editar":
                flash("Solicitação atualizada.", "ok")
            elif acao == "responsavel":
                novo = _q_resp(sid)
                flash(f"Responsável: {novo}." if novo else "Solicitação sem responsável.", "ok")
            elif acao == "concluir":
                flash("Solicitação concluída" + (" e registrada no clipping do parlamentar." if f.get("no_clipping") == "1"
                                                 else ".") , "ok")
            destino = url_for("meu.painel", cod=cod, secao=secao, status=request.args.get("status", ""),
                              abrir=sid if acao != "excluir" else None)
        elif secao == "questoes" and acao == "gerar":
            sit = (f.get("situacao") or "").strip()
            if len(sit) < 30:
                raise ValueError("Descreva a situação com mais detalhes (pelo menos algumas frases).")
            if not questoes.configurado():
                raise ValueError("A IA ainda não está ligada: falta a variável GEMINI_API_KEY no Railway.")
            qid = questoes.criar_pedido(cod, autor, sit[:8000])
            questoes.processar_em_segundo_plano(qid, s["nome"])
            flash("Elaborando a questão de ordem. A resposta aparece aqui em até um minuto.", "ok")
        elif secao == "questoes" and acao == "regimento":
            if not g.usuario.get("admin"):
                abort(403)
            arq = request.files.get("arquivo")
            if not arq or not arq.filename:
                raise ValueError("Escolha o arquivo do Regimento.")
            n = questoes.salvar_regimento(arq.filename, arq.read(30 * 1024 * 1024))
            flash(f"Regimento cadastrado: {n} artigos encontrados.", "ok")
        elif secao == "radar" and acao in ("add", "del"):
            alterar_temas(cod, acao, f.get("termo"))
        elif secao == "radar" and acao == "item":
            sid = radar_item(cod, f.get("id", type=int), f, autor)
            if sid:
                quem = _q_resp(sid)
                flash("Notícia registrada em Solicitações · responsável: " + (quem or "ninguém (configure as equipes "
                      "na Seleção da IA)") + ("" if f.get("responsavel") else " (sorteado pelos critérios)"), "ok")
            destino = url_for("meu.painel", cod=cod, secao=secao, filtro=request.args.get("filtro", ""),
                              termo=request.args.get("termo", "")) + f"#n{f.get('id', type=int)}"
        elif secao == "radar" and acao == "manual":
            import radar as rd
            nid, nova = rd.incluir_manual(cod, f, autor)
            if nova:
                ia = rd.analisar_em_segundo_plano(cod, nid)
                flash("Notícia incluída no radar." + (" A IA está analisando — recarregue em instantes." if ia else ""), "ok")
            else:
                flash("Essa notícia já está no radar deste deputado.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="radar", q=f.get("q") or None,
                              web="1" if f.get("q") else None) + f"#n{nid}"
            if not f.get("q"):
                destino = url_for("meu.painel", cod=cod, secao="radar") + f"#n{nid}"
        elif secao == "radar" and acao == "selecao_gerar":
            import selecao_ia as si
            r = si.gerar_em_segundo_plano(cod, autor=autor)
            destino = url_for("meu.painel", cod=cod, secao="radar", filtro="selecao")
            if r.get("rodando"):
                flash("A IA está gerando a nova seleção — pode levar 1 a 3 minutos. A página atualiza sozinha "
                      "quando o lote ficar pronto.", "ok")
            elif r.get("erro"):
                raise ValueError(r["erro"])
            else:
                flash(f"Seleção de {r['gerado_em'].strftime('%d/%m/%Y às %H:%M')} gerada: {r['n']} tópico(s) "
                      "pendentes de abertura. Confirme os que devem virar solicitação.", "ok")
                destino += "#lote-" + r["lote"].replace(":", "")
        elif secao == "radar" and acao == "prelista_add":
            import prelista as pl
            _, novo = pl.adicionar_noticia(cod, f.get("id", type=int), autor)
            flash("Notícia enviada à pré-lista." if novo else "Essa notícia já está na pré-lista.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="radar", filtro=f.get("filtro") or None,
                              termo=f.get("termo") or None, q=f.get("q") or None) + f"#n{f.get('id', '')}"
        elif secao == "radar" and acao == "prelista_link":
            import prelista as pl
            import radar as rd
            nid, nova = rd.incluir_manual(cod, f, autor)
            if nova:
                try:                                   # análise curta da IA antes, para o item já levar as providências
                    rd.analisar(cod, limite=1, ids=[nid])
                except Exception:
                    log.exception("pré-lista: análise da notícia incluída pelo link")
            _, novo = pl.adicionar_noticia(cod, nid, autor)
            flash(("Notícia adicionada à pré-lista." if novo else "Essa notícia já está na pré-lista.")
                  + ("" if nova else " (Ela já estava no radar.)"), "ok")
            destino = url_for("meu.painel", cod=cod, secao="radar", filtro="prelista")
        elif secao == "radar" and acao == "prelista_remover":
            import prelista as pl
            pl.remover(cod, f.get("item", type=int))
            flash("Item retirado da pré-lista.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="radar", filtro="prelista")
        elif secao == "radar" and acao == "prelista_enviar":
            import prelista as pl
            eid = pl.gerar_envio(cod, autor)
            flash("Pré-lista pronta: envie a imagem e o link pelo WhatsApp para a chefia aprovar. O que você enviar "
                  "à pré-lista daqui em diante entra nesta mesma lista, até a chefia responder.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="radar", filtro="prelista", envio=eid) + f"#envio{eid}"
        elif secao == "radar" and acao == "prelista_cancelar":
            import prelista as pl
            if pl.cancelar_envio(cod, f.get("envio", type=int)):
                flash("Pré-lista cancelada: o link deixou de valer e os itens voltaram para a lista.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="radar", filtro="prelista")
        elif secao == "radar" and acao == "selecao_decidir":
            import selecao_ia as si
            ancora = ""
            if f.get("pre_um"):
                import prelista as pl
                pl.adicionar_selecao(cod, [f.get("pre_um")], autor)
                flash("Tópico enviado à pré-lista.", "ok")
                return redirect(url_for("meu.painel", cod=cod, secao="radar", filtro="selecao") + f"#t{f.get('pre_um')}")
            if f.get("modo") == "prelista":
                import prelista as pl
                n = pl.adicionar_selecao(cod, f.getlist("ids"), autor)
                flash(f"{n} tópico(s) enviado(s) à pré-lista." if n else "Marque pelo menos um tópico.", "ok" if n else "erro")
                return redirect(url_for("meu.painel", cod=cod, secao="radar", filtro="selecao"))
            if f.get("um"):
                ids, modo = [f.get("um")], "abrir"
            elif f.get("descartar_um"):
                ids, modo = [f.get("descartar_um")], "descartar"
            elif f.get("restaurar_um"):
                ids, modo = [f.get("restaurar_um")], "restaurar"
            else:
                ids, modo = f.getlist("ids"), f.get("modo")
            if not ids:
                flash("Marque pelo menos um tópico.", "erro")
            elif modo == "abrir":
                resps = {int(k[5:]): v for k, v in f.items() if k.startswith("resp_") and k[5:].isdigit()}
                escolhas = {int(k[4:]): int(v) for k, v in f.items()
                            if k.startswith("alt_") and k[4:].isdigit() and str(v).isdigit()}
                feitos = si.abrir(cod, ids, autor, resps, escolhas=escolhas)
                if feitos:
                    flash(f"{len(feitos)} solicitação(ões) aberta(s) a partir da Seleção da IA." if len(feitos) > 1 else
                          "Solicitação aberta a partir da Seleção da IA.", "ok")
                    ancora = f"#t{feitos[0][0]}"
                else:
                    flash("Esses tópicos já tinham sido abertos ou descartados por outra pessoa.", "erro")
            elif modo == "descartar":
                flash(f"{si.descartar(cod, ids, autor)} tópico(s) descartado(s).", "ok")
            elif modo == "restaurar":
                si.restaurar(cod, ids)
                flash("Tópico de volta às pendentes de abertura.", "ok")
                ancora = f"#t{ids[0]}"
            destino = url_for("meu.painel", cod=cod, secao="radar", filtro="selecao") + ancora
        elif secao == "radar" and acao == "selecao_equipes":
            import selecao_ia as si
            si.salvar_equipes(f.getlist("comissao"), f.getlist("outros"))
            flash("Equipes da Seleção da IA salvas.", "ok")
            destino = url_for("meu.painel", cod=cod, secao="radar", filtro="selecao")
        elif secao == "radar" and acao in ("fonte_add", "fonte_toggle", "fonte_del"):
            if not g.usuario.get("admin"):
                abort(403)
            import radar as rd
            rd.alterar_fonte(acao, f)
        else:
            abort(400)
    except ValueError as e:
        flash(str(e), "erro")
    return redirect(destino)


def _login_do_painel(login, nome=""):
    """Login deste painel para o usuário do Minha Pauta: o mesmo login (sem diferenciar maiúsculas); se não houver,
    o usuário ativo cujo nome bate com o nome exibido lá (ex.: "Marcelo Uvara"). None se não achar ou houver empate."""
    from sqlalchemy import func
    with engine.connect() as c:
        if login:
            achado = c.execute(select(usuarios.c.login).where(func.lower(usuarios.c.login) == login)).scalar()
            if achado:
                return achado
        if nome:
            alvo = _sem_acento(nome).lower().split()
            cands = [r[0] for r in c.execute(select(usuarios.c.login, usuarios.c.nome).where(usuarios.c.ativo.is_(True)))
                     if r[1] and _sem_acento(r[1]).lower().split() == alvo]
            if len(cands) == 1:
                return cands[0]
    return None


def tarefas_api():
    """GET /api/tarefas?login=fulano — solicitações em aberto do analista, para o aviso no app Minha Pauta.

    Protegido pela chave compartilhada TAREFAS_TOKEN (cabeçalho X-Painel-Token); sem a variável, o serviço fica
    desligado. Devolve só o necessário para o aviso (título, deputado, prazo e link)."""
    import hmac
    import os
    from flask import jsonify
    token = os.environ.get("TAREFAS_TOKEN", "")
    enviado = request.headers.get("X-Painel-Token", "")
    if not token:
        return jsonify({"erro": "sem_token", "detalhe": "TAREFAS_TOKEN não está configurado no Painel."}), 503
    if not hmac.compare_digest(enviado, token):
        return jsonify({"erro": "token_diferente", "detalhe": "A chave enviada não confere com TAREFAS_TOKEN."}), 403
    pedido = (request.args.get("login") or "").strip().lower()[:60]
    achado = _login_do_painel(pedido, (request.args.get("nome") or "").strip()[:120])
    login = achado or pedido
    base = request.url_root.rstrip("/")
    if os.environ.get("RAILWAY_ENVIRONMENT") and base.startswith("http://"):
        base = "https://" + base[7:]
    itens = tarefas_do_usuario(login, limite=30) if login else []
    return jsonify({
        "login": login, "usuario_encontrado": bool(achado), "total": len(itens), "novas": sum(1 for t in itens if t["nova"]),
        "atrasadas": sum(1 for t in itens if t["atrasada"]), "url": base + "/orientacoes",
        "itens": [{"titulo": t["titulo"], "deputado": t["deputado"], "nova": t["nova"], "atrasada": t["atrasada"],
                   "prazo": t["prazo"], "status": t["status"],
                   "url": f"{base}/meu/{t['codigo_deputado']}?secao=solicitacoes&abrir={t['id']}#s{t['id']}"}
                  for t in itens[:10]]})


@bp.route("/<int:cod>/prelista/<int:eid>.png")
def prelista_imagem(cod, eid):
    import prelista as pl
    _exigir_acesso(cod)
    e = pl.envio_por_id(cod, eid)
    if not e:
        abort(404)
    from flask import send_file
    return send_file(pl.imagem(e, cod), mimetype="image/png", download_name=f"pre-lista-{eid}.png",
                     as_attachment=request.args.get("baixar") == "1")


@bp.route("/<int:cod>/agenda/status")
def agenda_status(cod):
    """A tela da agenda consulta isto enquanto a releitura roda, para se recarregar quando terminar."""
    import coleta_meu
    _exigir_acesso(cod)
    v = request.args.get("dia")
    if v:
        try:
            return coleta_meu.status_dia(date.fromisoformat(v))
        except ValueError:
            pass
    return coleta_meu.status_agenda_proxima()


@bp.route("/<int:cod>/agenda.pdf")
def agenda_pdf(cod):
    """Agenda do período escolhido em PDF (abre no navegador, pronto para imprimir ou salvar)."""
    import agenda_pdf as ap
    _exigir_acesso(cod)
    s = paineis.deputado(cod)
    if not s:
        abort(404)
    agora = agora_brt()
    try:
        inicio, fim, rotulo = ap.periodo(request.args, agora.date())
    except ValueError as e:
        flash(str(e), "erro")
        return redirect(url_for("meu.painel", cod=cod, secao="agenda"))
    so_dele = request.args.get("todos") != "1"
    pdf = ap.gerar(s, agenda_deputado(cod, inicio, fim, so_dele, ocultar_solenes()), inicio, fim, rotulo, so_dele,
                   com_descricao=request.args.get("descricao") == "1", agora=agora)
    nome = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", s.get("nome") or "").encode("ascii", "ignore")
                  .decode().lower()).strip("-") or str(cod)
    resp = send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=False,
                     download_name=f"agenda-{nome}-{inicio:%Y%m%d}{'' if fim == inicio else f'-{fim:%Y%m%d}'}.pdf")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/<int:cod>/anexo/<int:aid>")
def baixar_anexo(cod, aid):
    with engine.connect() as c:
        a = c.execute(select(anexos, solicitacoes.c.codigo_deputado, solicitacoes.c.responsavel)
                      .join(solicitacoes, solicitacoes.c.id == anexos.c.solicitacao_id)
                      .where(anexos.c.id == aid)).mappings().first()
    if not _acesso_pleno(cod) and not (a and a["responsavel"] and a["responsavel"] == g.usuario.get("login")):
        abort(403)
    if not a or a["codigo_deputado"] != cod:
        abort(404)
    resp = send_file(io.BytesIO(a["conteudo"]), mimetype=a["mime"] or "application/octet-stream",
                     as_attachment=True, download_name=a["nome"])
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/<int:cod>/realizacoes.pdf")
def realizacoes_pdf(cod):
    """Relatório de realizações do ano (ou do período) em PDF."""
    import relatorio_pdf as rp
    _exigir_acesso(cod)
    s = paineis.deputado(cod)
    if not s:
        abort(404)
    agora = agora_brt()
    ano = request.args.get("ano", type=int) or agora.year
    inicio = _data(request.args.get("de")) or date(ano, 1, 1)
    fim = _data(request.args.get("ate")) or min(date(ano, 12, 31), agora.date())
    if fim < inicio:
        inicio, fim = fim, inicio
    if request.args.get("de") or request.args.get("ate"):
        rotulo = f"Período de {inicio:%d/%m/%Y} a {fim:%d/%m/%Y}"
    else:
        rotulo = f"Ano de {ano}" + (f" · até {fim:%d/%m/%Y}" if fim < date(ano, 12, 31) else "")
    evs = eventos_relatorio(cod, inicio, fim, request.args.get("aprovacoes", "1") == "1")
    pdf = rp.gerar(s, evs, inicio, fim, rotulo, agora=agora)
    nome = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", s.get("nome") or "").encode("ascii", "ignore")
                  .decode().lower()).strip("-") or str(cod)
    resp = send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=False,
                     download_name=f"realizacoes-{nome}-{inicio:%Y%m%d}-{fim:%Y%m%d}.pdf")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.app_template_filter("md_simples")
def md_simples(texto):
    """Markdown mínimo da resposta da IA (títulos ##, negrito, listas), sempre escapando HTML."""
    from markupsafe import Markup, escape
    html, lista = [], False
    for linha in (texto or "").splitlines():
        t = str(escape(linha.rstrip()))
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        item = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)", t)
        if item:
            if not lista:
                html.append("<ul>")
                lista = True
            html.append(f"<li>{item.group(1)}</li>")
            continue
        if lista:
            html.append("</ul>")
            lista = False
        if t.startswith("## ") or t.startswith("### "):
            html.append(f"<h3>{t.lstrip('#').strip()}</h3>")
        elif t.strip() == "---":
            html.append("<hr>")
        elif t.strip():
            html.append(f"<p>{t}</p>")
    if lista:
        html.append("</ul>")
    return Markup("\n".join(html))


@bp.app_template_filter("data_hora")
def data_hora(v):
    """Data e hora de um registro (gravado no horário do servidor) no horário de Brasília: 03/11/2026 às 18:55."""
    if not v:
        return ""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    if v.tzinfo is None:
        v = v.astimezone()                    # o horário gravado é o do servidor (UTC no Railway)
    v = v.astimezone(BRT)
    return f"{v:%d/%m/%Y} às {v:%H:%M}"


@bp.app_template_filter("isodata")
def isodata(v):
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return date.today()


@bp.app_template_filter("tamanho")
def tamanho(n):
    n = n or 0
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB".replace(".", ",")
