"""Pré-lista: providências separadas pela equipe para a chefia ou o parlamentar aprovar.

Qualquer notícia do radar (em qualquer filtro) ou tópico da Seleção da IA pode ir para a pré-lista. Quando
quiser, a equipe gera um ENVIO: uma imagem da lista e um link secreto (sem login) para mandar pelo WhatsApp.
Pelo link, a chefia marca o que deve virar solicitação e, para cada item, escolhe o analista ou deixa o painel
sortear (pela equipe do tipo: comissão × outros assuntos, sem repetir até todos receberem). Ao confirmar, as
solicitações são criadas e os analistas recebem o aviso de tarefa; o que não foi marcado fica como recusado.
Um link vale para uma resposta e expira em PRELISTA_DIAS (7) dias; cancelar o envio devolve os itens à lista.
"""
import io
import json
import logging
import os
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, render_template, request, send_file, session, url_for
from sqlalchemy import delete, func, insert, select, update

from db import engine, noticias, prelista, prelista_envios, selecao_ia, solicitacoes, usuarios

log = logging.getLogger("prelista")
DIAS_LINK = int(os.environ.get("PRELISTA_DIAS", "7"))
ATIVOS = ("na_lista", "enviada")
SORTEAR = "__sortear__"
bp = Blueprint("prelista_publica", __name__)


def _agora_brt():
    import meu
    return meu.agora_brt()


# ---------------- montar a lista ----------------

def _ja_ativo(c, cod, origem, ref):
    return c.execute(select(prelista.c.id, prelista.c.status).where(
        prelista.c.codigo_deputado == cod, prelista.c.origem == origem, prelista.c.ref_id == ref,
        prelista.c.status.in_(ATIVOS))).first()


def adicionar_noticia(cod, nid, autor):
    """Põe uma notícia do radar na pré-lista. Devolve (id do item, novo?)."""
    import selecao_ia as si
    with engine.begin() as c:
        n = c.execute(select(noticias).where(noticias.c.id == nid, noticias.c.codigo_deputado == cod)).mappings().first()
        if not n:
            abort(404)
        if n["solicitacao_id"] and c.execute(select(solicitacoes.c.id).where(solicitacoes.c.id == n["solicitacao_id"])).first():
            raise ValueError("Essa notícia já tem solicitação aberta.")
        ja = _ja_ativo(c, cod, "noticia", nid)
        if ja:
            return ja[0], False
        texto = " ".join(str(n.get(k) or "") for k in ("tipo_acao", "acao_sugerida", "titulo"))
        iid = c.execute(insert(prelista).values(
            codigo_deputado=cod, origem="noticia", ref_id=nid, titulo=(n["titulo"] or "Notícia")[:300],
            resumo=n.get("resumo"), acao=n.get("acao_sugerida"), instrumento=(n.get("tipo_acao") or "")[:80] or None,
            area=None, tipo=si.tipo_do_texto(texto), tema=n.get("termo"),
            links=json.dumps([{"titulo": n["titulo"], "link": n["link"], "fonte": n.get("fonte")}]),
            status="na_lista", incluido_por=autor, incluido_em=datetime.now())).inserted_primary_key[0]
    return iid, True


def adicionar_selecao(cod, ids, autor):
    """Põe tópicos pendentes da Seleção da IA na pré-lista. Devolve quantos entraram."""
    ids = [int(i) for i in ids if str(i).isdigit()]
    n = 0
    with engine.begin() as c:
        tops = [dict(r) for r in c.execute(select(selecao_ia).where(
            selecao_ia.c.codigo_deputado == cod, selecao_ia.c.id.in_(ids or [0]),
            selecao_ia.c.status == "pendente")).mappings()]
        for t in tops:
            nids = json.loads(t["noticias"] or "[]")
            links = [{"titulo": r[0], "link": r[1], "fonte": r[2]} for r in c.execute(
                select(noticias.c.titulo, noticias.c.link, noticias.c.fonte).where(noticias.c.id.in_(nids or [0])))]
            c.execute(insert(prelista).values(
                codigo_deputado=cod, origem="selecao", ref_id=t["id"], titulo=t["topico"][:300], resumo=t["resumo"],
                acao=t["acao_sugerida"], instrumento=t["instrumento"], area=t["area"], tipo=t["tipo"] or "outros",
                tema=t["tema"], links=json.dumps(links), status="na_lista", incluido_por=autor,
                incluido_em=datetime.now()))
            c.execute(update(selecao_ia).where(selecao_ia.c.id == t["id"]).values(status="prelista"))
            n += 1
    return n


def remover(cod, iid):
    """Tira um item que ainda não foi enviado (tópico da Seleção da IA volta a pendente de abertura)."""
    with engine.begin() as c:
        it = c.execute(select(prelista).where(prelista.c.id == iid, prelista.c.codigo_deputado == cod,
                                              prelista.c.status == "na_lista")).mappings().first()
        if not it:
            return False
        if it["origem"] == "selecao":
            c.execute(update(selecao_ia).where(selecao_ia.c.id == it["ref_id"], selecao_ia.c.status == "prelista")
                      .values(status="pendente"))
        c.execute(delete(prelista).where(prelista.c.id == iid))
    return True


def itens(cod, status=("na_lista",)):
    with engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(prelista).where(
            prelista.c.codigo_deputado == cod, prelista.c.status.in_(status)).order_by(prelista.c.id)).mappings()]
    for r in rows:
        r["links_l"] = json.loads(r["links"] or "[]")
    return rows


def contagem(cod):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(prelista).where(
            prelista.c.codigo_deputado == cod, prelista.c.status == "na_lista")).scalar() or 0


def noticias_na_lista(cod):
    """{id da notícia: status do item} para marcar as notícias que estão na pré-lista ou aguardando a chefia."""
    with engine.connect() as c:
        return {r[0]: r[1] for r in c.execute(select(prelista.c.ref_id, prelista.c.status).where(
            prelista.c.codigo_deputado == cod, prelista.c.origem == "noticia", prelista.c.status.in_(ATIVOS)))}


# ---------------- envios ----------------

def gerar_envio(cod, autor):
    """Congela os itens da lista num envio com link secreto. Devolve o id do envio."""
    agora = datetime.now()
    with engine.begin() as c:
        ids = [r[0] for r in c.execute(select(prelista.c.id).where(prelista.c.codigo_deputado == cod,
                                                                   prelista.c.status == "na_lista"))]
        if not ids:
            raise ValueError("A pré-lista está vazia: envie itens do radar ou da Seleção da IA para ela.")
        eid = c.execute(insert(prelista_envios).values(
            codigo_deputado=cod, token=secrets.token_urlsafe(24), status="aguardando", criado_por=autor,
            criado_em=agora, expira_em=agora + timedelta(days=DIAS_LINK))).inserted_primary_key[0]
        c.execute(update(prelista).where(prelista.c.id.in_(ids)).values(status="enviada", envio_id=eid))
    log.info("pré-lista do deputado %s: envio %s com %d item(ns)", cod, eid, len(ids))
    return eid


def cancelar_envio(cod, eid):
    """Cancela um envio ainda sem resposta: o link deixa de valer e os itens voltam à lista."""
    with engine.begin() as c:
        ok = c.execute(update(prelista_envios).where(prelista_envios.c.id == eid,
                                                     prelista_envios.c.codigo_deputado == cod,
                                                     prelista_envios.c.status == "aguardando")
                       .values(status="cancelada")).rowcount
        if ok:
            c.execute(update(prelista).where(prelista.c.envio_id == eid, prelista.c.status == "enviada")
                      .values(status="na_lista", envio_id=None))
    return bool(ok)


def _envio_com_itens(c, e):
    e = dict(e)
    e["itens"] = [dict(r) for r in c.execute(select(prelista).where(prelista.c.envio_id == e["id"])
                                             .order_by(prelista.c.id)).mappings()]
    for r in e["itens"]:
        r["links_l"] = json.loads(r["links"] or "[]")
    e["aprovadas"] = sum(1 for r in e["itens"] if r["status"] == "aprovada")
    e["recusadas"] = sum(1 for r in e["itens"] if r["status"] == "recusada")
    e["expirado"] = e["status"] == "aguardando" and e["expira_em"] and e["expira_em"] < datetime.now()
    return e


def envios(cod, limite=10):
    with engine.connect() as c:
        rows = c.execute(select(prelista_envios).where(prelista_envios.c.codigo_deputado == cod,
                                                       prelista_envios.c.status != "cancelada")
                         .order_by(prelista_envios.c.id.desc()).limit(limite)).mappings().all()
        return [_envio_com_itens(c, e) for e in rows]


def envio_por_id(cod, eid):
    with engine.connect() as c:
        e = c.execute(select(prelista_envios).where(prelista_envios.c.id == eid,
                                                    prelista_envios.c.codigo_deputado == cod)).mappings().first()
        return _envio_com_itens(c, e) if e else None


def envio_por_token(token):
    if not token or len(token) > 64:
        return None
    with engine.connect() as c:
        e = c.execute(select(prelista_envios).where(prelista_envios.c.token == token)).mappings().first()
        return _envio_com_itens(c, e) if e else None


def analistas():
    """Quem a chefia pode escolher: as equipes da Seleção da IA e os servidores da Oposição."""
    import selecao_ia as si
    eq = si.equipes()
    logins = set(eq.get("comissao", [])) | set(eq.get("outros", []))
    with engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(usuarios.c.login, usuarios.c.nome, usuarios.c.equipe)
                                           .where(usuarios.c.ativo.is_(True))).mappings()]
    out = [u for u in rows if u["login"] in logins or u["equipe"] == "oposicao"]
    return sorted(out, key=lambda u: (u["nome"] or u["login"]).lower())


def responder(token, escolhas, nome, rnd=None):
    """Resposta da chefia: `escolhas` = {id do item: login ou SORTEAR} só para os itens aprovados.
    Cria as solicitações (sorteando quando pedido) e marca os demais como recusados. Devolve o envio atualizado."""
    import random
    import selecao_ia as si
    rnd = rnd or random
    e = envio_por_token(token)
    if not e or e["status"] != "aguardando" or e["expirado"]:
        raise ValueError("Este link não está mais disponível.")
    nome = (nome or "").strip()[:120] or "Chefia"
    validos = {u["login"] for u in analistas()}
    aprovados = [it for it in e["itens"] if it["id"] in escolhas and it["status"] == "enviada"]
    sortear_ = [it for it in aprovados if escolhas[it["id"]] not in validos]
    sorteados = dict(zip([it["id"] for it in sortear_], si.sortear([it["tipo"] or "outros" for it in sortear_],
                                                                    si.equipes(), rnd)))
    agora, hoje = datetime.now(), _agora_brt().date()
    prazo = (hoje + timedelta(days=si.PRAZO_DIAS)).isoformat()
    quem = f"Pré-lista · {nome}"[:60]
    with engine.begin() as c:
        ok = c.execute(update(prelista_envios).where(prelista_envios.c.id == e["id"],
                                                     prelista_envios.c.status == "aguardando")
                       .values(status="respondida", respondido_por=nome, respondido_em=agora)).rowcount
        if not ok:
            raise ValueError("Esta pré-lista já foi respondida.")
        for it in e["itens"]:
            if it["status"] != "enviada":
                continue
            if it not in aprovados:
                c.execute(update(prelista).where(prelista.c.id == it["id"]).values(status="recusada", decidido_em=agora))
                if it["origem"] == "selecao":
                    c.execute(update(selecao_ia).where(selecao_ia.c.id == it["ref_id"]).values(
                        status="descartada", decidido_por=quem, decidido_em=agora))
                continue
            resp = escolhas[it["id"]] if escolhas[it["id"]] in validos else sorteados.get(it["id"])
            links = "\n".join(f"- {l.get('titulo') or ''} — {l.get('link') or ''}" for l in it["links_l"])
            desc = "\n\n".join(x for x in (
                f"Aprovada na pré-lista por {nome} em {quando(agora)}"
                + (" · responsável sorteado" if it["id"] in sorteados else " · responsável escolhido pela chefia"),
                f"Tema acompanhado: {it['tema']}" if it.get("tema") else None, it.get("resumo"),
                f"Providência sugerida{f' ({it['instrumento']})' if it.get('instrumento') else ''}: {it['acao']}"
                if it.get("acao") else None, f"Notícias:\n{links}" if links else None) if x)
            prefixo = "Seleção da IA: " if it["origem"] == "selecao" else "Radar: "
            sid = c.execute(insert(solicitacoes).values(
                codigo_deputado=e["codigo_deputado"], titulo=(prefixo + it["titulo"])[:200], descricao=desc,
                solicitante="Pré-lista aprovada pela chefia", status="aberta", data_solicitacao=hoje.isoformat(),
                prazo=prazo, responsavel=resp, area=it.get("area"), atribuido_por=quem if resp else None,
                atribuido_em=agora if resp else None, criado_por=quem, criado_em=agora,
                atualizado_em=agora)).inserted_primary_key[0]
            c.execute(update(prelista).where(prelista.c.id == it["id"]).values(
                status="aprovada", solicitacao_id=sid, responsavel=resp, decidido_em=agora))
            nome_resp = None
            if resp:
                nome_resp = c.execute(select(usuarios.c.nome).where(usuarios.c.login == resp)).scalar() or resp
            if it["origem"] == "selecao":
                c.execute(update(selecao_ia).where(selecao_ia.c.id == it["ref_id"]).values(
                    status="aberta", solicitacao_id=sid, responsavel=resp, decidido_por=quem, decidido_em=agora))
                nids = [r[0] for r in c.execute(select(selecao_ia.c.noticias).where(selecao_ia.c.id == it["ref_id"]))]
                nids = json.loads(nids[0] or "[]") if nids else []
                if nids:
                    c.execute(update(noticias).where(noticias.c.id.in_(nids), noticias.c.solicitacao_id.is_(None))
                              .values(solicitacao_id=sid))
            else:
                c.execute(update(noticias).where(noticias.c.id == it["ref_id"]).values(
                    solicitacao_id=sid, assessor=nome_resp))
    log.info("pré-lista %s respondida por %s: %d aprovada(s)", e["id"], nome, len(aprovados))
    return envio_por_token(token)


# ---------------- textos e imagem ----------------

def quando(v):
    """'05/11/2026 às 14:46' no horário de Brasília (os registros guardam o horário do servidor)."""
    import meu
    return meu.data_hora(v)


def nome_deputado(cod):
    import paineis
    d = paineis.deputado(cod) or {}
    return d.get("nome") or f"Deputado {cod}"


def link_publico(e):
    url = url_for("prelista_publica.pagina", token=e["token"], _external=True)
    if os.environ.get("RAILWAY_ENVIRONMENT") and url.startswith("http://"):
        url = "https://" + url[7:]
    return url


def texto_whatsapp(e, cod):
    linhas = [f"*Pré-lista de providências — {nome_deputado(cod)}* ({quando(e['criado_em'])})", ""]
    for i, it in enumerate(e["itens"], 1):
        linhas.append(f"{i}. {it['titulo']}")
    linhas += ["", "Marque no link quais devem virar solicitação e escolha o analista (ou deixe sortear):",
               link_publico(e)]
    return "\n".join(linhas)


def _fontes():
    import reportlab
    from PIL import ImageFont
    d = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    f = lambda nome, tam: ImageFont.truetype(os.path.join(d, nome), tam)
    return {"tit": f("VeraBd.ttf", 40), "sub": f("Vera.ttf", 25), "item": f("VeraBd.ttf", 29),
            "txt": f("Vera.ttf", 24), "chip": f("VeraBd.ttf", 19), "num": f("VeraBd.ttf", 26), "rod": f("Vera.ttf", 22)}


def _quebrar(draw, texto, fonte, largura, max_linhas):
    palavras, linhas, atual = (texto or "").split(), [], ""
    for p in palavras:
        teste = (atual + " " + p).strip()
        if draw.textlength(teste, font=fonte) <= largura:
            atual = teste
            continue
        if atual:
            linhas.append(atual)
        atual = p
        if len(linhas) == max_linhas:
            break
    if atual and len(linhas) < max_linhas:
        linhas.append(atual)
    if len(linhas) == max_linhas and " ".join(linhas) != " ".join(palavras):
        ult = linhas[-1]
        while ult and draw.textlength(ult + "…", font=fonte) > largura:
            ult = ult[:-1]
        linhas[-1] = ult.rstrip() + "…"
    return linhas


def imagem(e, cod):
    """PNG da pré-lista (para enviar pelo WhatsApp e para a prévia do link)."""
    from PIL import Image, ImageDraw
    F = _fontes()
    W, M = 1080, 56
    AZUL, AZUL_ESC, TINTA, SUAVE = (29, 95, 191), (16, 61, 130), (28, 33, 40), (95, 104, 116)
    tmp = ImageDraw.Draw(Image.new("RGB", (W, 10)))
    larg = W - 2 * M - 64
    blocos = []
    for i, it in enumerate(e["itens"], 1):
        tit = _quebrar(tmp, it["titulo"], F["item"], larg, 3)
        acao = _quebrar(tmp, it.get("acao") or it.get("resumo") or "", F["txt"], larg, 3)
        chips = [x for x in ("Comissão" if it.get("tipo") == "comissao" else "Outros assuntos", it.get("area"),
                             it.get("instrumento")) if x]
        h = 22 + len(tit) * 38 + 44 + len(acao) * 32 + 28
        blocos.append((i, it, tit, acao, chips, h))
    altura = 210 + sum(b[5] for b in blocos) + 120
    img = Image.new("RGB", (W, altura), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 180], fill=AZUL)
    d.text((M, 42), "Pré-lista para aprovação", font=F["tit"], fill=(255, 255, 255))
    sub = f"{nome_deputado(cod)} · {quando(e['criado_em'])} · {len(e['itens'])} " \
          f"providência{'s' if len(e['itens']) != 1 else ''}"
    d.text((M, 104), _quebrar(d, sub, F["sub"], W - 2 * M, 1)[0], font=F["sub"], fill=(220, 232, 250))
    y = 210
    for i, it, tit, acao, chips, h in blocos:
        if i > 1:
            d.line([M, y - 4, W - M, y - 4], fill=(226, 232, 240), width=2)
        d.ellipse([M, y + 18, M + 44, y + 62], fill=AZUL)
        n = str(i)
        d.text((M + 22 - d.textlength(n, font=F["num"]) / 2, y + 25), n, font=F["num"], fill=(255, 255, 255))
        x0, yy = M + 64, y + 18
        for linha in tit:
            d.text((x0, yy), linha, font=F["item"], fill=TINTA)
            yy += 38
        x = x0
        for ch in chips:
            w = d.textlength(ch, font=F["chip"]) + 22
            if x + w > W - M:
                break
            d.rounded_rectangle([x, yy + 4, x + w, yy + 34], radius=14, fill=(232, 240, 252))
            d.text((x + 11, yy + 9), ch, font=F["chip"], fill=AZUL_ESC)
            x += w + 8
        yy += 44
        for linha in acao:
            d.text((x0, yy), linha, font=F["txt"], fill=SUAVE)
            yy += 32
        y += h
    d.rectangle([0, altura - 96, W, altura], fill=(243, 246, 251))
    d.text((M, altura - 64), "Abra o link enviado junto para escolher quais viram solicitação.", font=F["rod"],
           fill=SUAVE)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf


# ---------------- página pública (sem login) ----------------

@bp.route("/aprovar/<token>", methods=["GET", "POST"])
def pagina(token):
    from auth import _csrf_ok, csrf_token
    e = envio_por_token(token)
    if not e:
        abort(404)
    erro = None
    if request.method == "POST":
        if not _csrf_ok():
            erro = "A página ficou aberta por muito tempo. Recarregue e marque de novo."
        else:
            escolhas = {}
            for k in request.form.getlist("itens"):
                if k.isdigit():
                    escolhas[int(k)] = request.form.get(f"resp_{k}") or SORTEAR
            try:
                if not escolhas and request.form.get("nenhuma") != "1":
                    raise ValueError("Marque pelo menos uma providência (ou confirme que não quer nenhuma).")
                e = responder(token, escolhas, request.form.get("nome"))
            except ValueError as ex:
                erro = str(ex)
    nomes = {u["login"]: u["nome"] or u["login"] for u in analistas()}
    if e.get("itens"):
        with engine.connect() as c:
            for r in c.execute(select(usuarios.c.login, usuarios.c.nome)):
                nomes.setdefault(r[0], r[1] or r[0])
    return render_template("prelista_aprovar.html", e=e, erro=erro, deputado=nome_deputado(e["codigo_deputado"]), quando=quando,
                           analistas=analistas(), nomes=nomes, sortear=SORTEAR, csrf=csrf_token(),
                           img_url=url_for("prelista_publica.imagem_publica", token=token, _external=True))


@bp.route("/aprovar/<token>/imagem.png")
def imagem_publica(token):
    e = envio_por_token(token)
    if not e:
        abort(404)
    return send_file(imagem(e, e["codigo_deputado"]), mimetype="image/png", max_age=300)
