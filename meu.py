"""Meu Parlamentar: área por deputado, aberta só a quem o administrador vinculou (admin vê todos).

Seções: visão geral, agenda, notificações, solicitações (com anexos), questões de ordem por IA,
radar de notícias, rankings (posição + sugestões) e clipping diário de aprovações.
"""
import io
import logging
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, send_file,
                   url_for)
from sqlalchemy import and_, delete, insert, or_, select, text, update

import metrics
import paineis
import questoes
from db import (agenda, anexos, deputados, engine, noticias, norm_nome, relatorias, solicitacoes,
                temas_radar, usuario_deputados)

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
    u = g.get("usuario") or {}
    if u.get("admin"):
        return
    if cod not in vinculos_do_usuario(u.get("id")):
        abort(403)


def tem_acesso_menu():
    u = g.get("usuario")
    if not u:
        return False
    return bool(u.get("admin")) or bool(vinculos_do_usuario(u["id"]))


# ---------------- utilitários ----------------

def _sem_acento(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


_APROVACAO = re.compile(r"(aprovad|transformad[ao] em norma|remetid[ao] (a|ao) (senado|camara|sancao)|"
                        r"enviad[ao] (a|ao) (senado|sancao)|aguardando (sancao|promulgacao)|"
                        r"aguardando (envio|remessa) ao senado|promulgad|sancionad)")


def eh_aprovacao(situacao):
    s = _sem_acento(situacao)
    return bool(_APROVACAO.search(s)) and "rejeitad" not in s and "nao aprovad" not in s


def _data(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


# ---------------- agenda ----------------

def agenda_deputado(cod, inicio, fim, so_dele=True):
    s = paineis.deputado(cod) or {}
    import coleta_meu
    cols = coleta_meu.colegiados_do_deputado(cod) | {"PLEN"}
    with engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(agenda).where(
            agenda.c.data >= inicio.isoformat(), agenda.c.data <= fim.isoformat(),
            or_(and_(agenda.c.origem == "camara", agenda.c.codigo_deputado.is_(None)),
                agenda.c.codigo_deputado == cod)).order_by(agenda.c.data, agenda.c.hora, agenda.c.id)).mappings()]
    nome = norm_nome(s.get("nome") or "")
    out = []
    for r in rows:
        desc = norm_nome(r.get("descricao") or "")
        # relator de item da pauta (pelo código) ou nome citado no texto do evento
        r["citado"] = f",{cod}," in (r.get("relatores") or "") or (bool(nome) and f" {nome} " in f" {desc} ")
        r["membro"] = r["origem"] == "equipe" or (r.get("colegiado") or "") in cols
        if so_dele and not (r["membro"] or r["citado"]):
            continue
        out.append(r)
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

def lista_solicitacoes(cod, status=""):
    q = select(solicitacoes).where(solicitacoes.c.codigo_deputado == cod)
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
        for r in c.execute(text("SELECT status, COUNT(*) n FROM solicitacoes WHERE codigo_deputado = :c "
                                "GROUP BY status"), {"c": cod}):
            contagem[r[0]] = r[1]
    hoje = date.today().isoformat()
    for i in itens:
        i["anexos"] = anx.get(i["id"], [])
        i["atrasada"] = bool(i["prazo"] and i["prazo"] < hoje and i["status"] in ("aberta", "andamento"))
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
    with engine.begin() as c:
        sid = c.execute(insert(solicitacoes).values(
            codigo_deputado=cod, titulo=titulo[:200], descricao=(f.get("descricao") or "").strip() or None,
            solicitante=(f.get("solicitante") or "").strip()[:120] or None, status="aberta",
            prazo=(_data(f.get("prazo")) or None) and _data(f.get("prazo")).isoformat(),
            criado_por=autor, criado_em=agora, atualizado_em=agora)).inserted_primary_key[0]
        _guardar_anexos(c, sid, arquivos, autor)
    return sid


def _solicitacao_do_deputado(c, cod, sid):
    return c.execute(select(solicitacoes.c.id).where(solicitacoes.c.id == sid,
                                                     solicitacoes.c.codigo_deputado == cod)).first()


def alterar_solicitacao(cod, sid, acao, f, arquivos, autor):
    with engine.begin() as c:
        if not _solicitacao_do_deputado(c, cod, sid):
            abort(404)
        if acao == "status" and f.get("status") in dict(STATUS):
            c.execute(update(solicitacoes).where(solicitacoes.c.id == sid)
                      .values(status=f["status"], atualizado_em=datetime.now()))
        elif acao == "anexar":
            _guardar_anexos(c, sid, arquivos, autor)
            c.execute(update(solicitacoes).where(solicitacoes.c.id == sid).values(atualizado_em=datetime.now()))
        elif acao == "excluir":
            c.execute(delete(anexos).where(anexos.c.solicitacao_id == sid))
            c.execute(delete(solicitacoes).where(solicitacoes.c.id == sid))
        elif acao == "excluir_anexo":
            c.execute(delete(anexos).where(anexos.c.id == f.get("anexo", type=int),
                                           anexos.c.solicitacao_id == sid))


# ---------------- radar ----------------

def radar(cod, termo=""):
    q = select(noticias).where(noticias.c.codigo_deputado == cod)
    if termo:
        q = q.where(noticias.c.termo == termo)
    with engine.connect() as c:
        itens = [dict(r) for r in c.execute(q.order_by(noticias.c.publicado.desc().nullslast(),
                                                       noticias.c.id.desc()).limit(200)).mappings()]
        temas = [r[0] for r in c.execute(select(temas_radar.c.termo).where(temas_radar.c.codigo_deputado == cod)
                                         .order_by(temas_radar.c.termo))]
        termos = [r[0] for r in c.execute(text("SELECT termo, COUNT(*) n FROM noticias WHERE codigo_deputado = :c "
                                               "GROUP BY termo ORDER BY n DESC"), {"c": cod})]
    return {"itens": itens, "temas": temas, "termos": termos}


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
    (item da pauta com parecer aprovado, na data da reunião)."""
    p = {"c": cod, "ini": inicio.isoformat(), "fim": fim.isoformat()}
    with engine.connect() as c:
        props = [dict(r) for r in c.execute(text("""
            SELECT pr.id, pr.identificacao, pr.ementa, pr.situacao, pr.data_situacao, pr.url, pr.virou_norma,
                   a.principal
            FROM processos pr JOIN autorias a ON a.processo_id = pr.id
            WHERE a.codigo_deputado = :c AND pr.data_situacao BETWEEN :ini AND :fim"""), p).mappings()]
        rels = [dict(r) for r in c.execute(text("""
            SELECT identificacao, ementa, situacao, data_situacao, url, colegiado
            FROM relatorias WHERE codigo_deputado = :c AND data_situacao BETWEEN :ini AND :fim"""), p).mappings()]
    eventos = []
    for x in props:
        if eh_aprovacao(x["situacao"]):
            eventos.append({**x, "tipo": "autoria", "rotulo": "Proposição aprovada" if x["principal"]
                            else "Proposição (coautoria) aprovada"})
    for x in rels:
        if eh_aprovacao(x["situacao"]):
            eventos.append({**x, "tipo": "relatoria", "rotulo": "Relatoria aprovada"})
    dias = {}
    for e in eventos:
        dias.setdefault(e["data_situacao"], []).append(e)
    return {"dias": [{"data": d, "itens": sorted(dias[d], key=lambda e: e["tipo"])}
                     for d in sorted(dias, reverse=True)],
            "total": len(eventos), "autoria": sum(e["tipo"] == "autoria" for e in eventos),
            "relatoria": sum(e["tipo"] == "relatoria" for e in eventos)}


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


# ---------------- visão geral ----------------

def visao(cod):
    hoje = date.today()
    ag = agenda_deputado(cod, hoje, hoje + timedelta(days=7))
    return {
        "agenda": ag[:4],
        "notif": paineis.notificacoes(cod, limite=6),
        "clip": clipping(cod, hoje - timedelta(days=30), hoje),
        "solic": lista_solicitacoes(cod, "")["contagem"],
        "radar": radar(cod)["itens"][:6],
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

@bp.app_context_processor
def _ctx():
    return {"menu_meu": tem_acesso_menu()}


@bp.route("/")
def inicio():
    cods = deputados_do_usuario()
    if not cods:
        abort(403)
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
    _exigir_acesso(cod)
    s = paineis.deputado(cod)
    if not s:
        abort(404)
    secao = request.args.get("secao", "visao")
    if secao not in dict(SECOES):
        secao = "visao"
    autor = g.usuario.get("login")
    if request.method == "POST":
        return _post(cod, secao, s, autor)
    hoje = date.today()
    ctx = {"aba": "meu", "s": s, "cod": cod, "secao": secao, "secoes": SECOES, "status": STATUS,
           "meus": [x for x in paineis.lista_deputados() if x["codigo"] in set(deputados_do_usuario())],
           "hoje": hoje}
    if secao == "visao":
        ctx["v"] = visao(cod)
    elif secao == "agenda":
        so_dele = request.args.get("todos") != "1"
        ctx["agenda"] = agenda_deputado(cod, hoje - timedelta(days=7), hoje + timedelta(days=45), so_dele)
        ctx["so_dele"] = so_dele
    elif secao == "notificacoes":
        ctx["notif"] = paineis.notificacoes(cod, limite=150)
    elif secao == "solicitacoes":
        ctx["filtro_status"] = request.args.get("status", "")
        ctx["sol"] = lista_solicitacoes(cod, ctx["filtro_status"])
    elif secao == "questoes":
        ctx["hist"] = questoes.historico(cod)
        ctx["ia_ok"] = questoes.configurado()
        reg = questoes.regimento()
        ctx["regimento"] = {"nome": reg["nome"], "atualizado": reg["atualizado"],
                            "artigos": len(questoes.artigos(reg["texto"]))} if reg else None
        ctx["gerando"] = any(q["resposta"] is None for q in ctx["hist"])
    elif secao == "radar":
        ctx["termo"] = request.args.get("termo", "")
        ctx["radar"] = radar(cod, ctx["termo"])
    elif secao == "rankings":
        ctx["ranks"] = rankings(cod)
    elif secao == "clipping":
        ini = _data(request.args.get("inicio")) or hoje - timedelta(days=30)
        fim = _data(request.args.get("fim")) or hoje
        ctx.update(clip=clipping(cod, ini, fim), ini=ini, fim=fim, rels=lista_relatorias(cod))
    return render_template("meu.html", **ctx)


def _post(cod, secao, s, autor):
    f, destino = request.form, url_for("meu.painel", cod=cod, secao=secao)
    acao = f.get("acao", "")
    try:
        if acao == "atualizar":
            ok = atualizar_em_segundo_plano(cod)
            flash("Atualização iniciada. Recarregue a página em alguns minutos." if ok else
                  "Já há uma atualização em andamento.", "ok" if ok else "erro")
        elif secao == "agenda" and acao == "novo":
            adicionar_compromisso(cod, f, autor)
            flash("Compromisso incluído na agenda.", "ok")
        elif secao == "agenda" and acao == "remover":
            remover_compromisso(cod, f.get("id", type=int))
        elif secao == "solicitacoes" and acao == "nova":
            criar_solicitacao(cod, f, request.files.getlist("arquivos"), autor)
            flash("Solicitação registrada.", "ok")
        elif secao == "solicitacoes" and acao in ("status", "anexar", "excluir", "excluir_anexo"):
            alterar_solicitacao(cod, f.get("id", type=int), acao, f, request.files.getlist("arquivos"), autor)
            if acao == "anexar":
                flash("Arquivos anexados.", "ok")
            destino = url_for("meu.painel", cod=cod, secao=secao, status=request.args.get("status", ""))
        elif secao == "questoes" and acao == "gerar":
            sit = (f.get("situacao") or "").strip()
            if len(sit) < 30:
                raise ValueError("Descreva a situação com mais detalhes (pelo menos algumas frases).")
            if not questoes.configurado():
                raise ValueError("A IA ainda não está ligada: falta a variável ANTHROPIC_API_KEY no Railway.")
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
        else:
            abort(400)
    except ValueError as e:
        flash(str(e), "erro")
    return redirect(destino)


@bp.route("/<int:cod>/anexo/<int:aid>")
def baixar_anexo(cod, aid):
    _exigir_acesso(cod)
    with engine.connect() as c:
        a = c.execute(select(anexos, solicitacoes.c.codigo_deputado)
                      .join(solicitacoes, solicitacoes.c.id == anexos.c.solicitacao_id)
                      .where(anexos.c.id == aid)).mappings().first()
    if not a or a["codigo_deputado"] != cod:
        abort(404)
    resp = send_file(io.BytesIO(a["conteudo"]), mimetype=a["mime"] or "application/octet-stream",
                     as_attachment=True, download_name=a["nome"])
    resp.headers["X-Content-Type-Options"] = "nosniff"
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
