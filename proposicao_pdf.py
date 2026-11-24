"""Relatório em PDF de UMA proposição (aba Votações e aba Proposições · botão "Relatório PDF").

Reúne o que o painel sabe da matéria: ficha (ementa, autoria, regime, situação, relator), todas as
votações nominais do Plenário (etapa, resultado, placar, orientações das lideranças, votos por partido
e quem votou contra a orientação do próprio partido) e o histórico de mudanças de situação.
Mesmo cabeçalho dos outros PDFs do painel.
"""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Flowable, Frame, HRFlowable, KeepTogether,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)
from sqlalchemy import text

import metrics
from agenda_pdf import CASA, LINHA, META, TEXTO, _Numerado, _p, fazer_cabecalho
from db import INSTITUCIONAIS, engine


# ---------------- dados ----------------

def _br(v, hora=False):
    if not v:
        return ""
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:19])
    except ValueError:
        return str(v)
    return d.strftime("%d/%m/%Y %H:%M" if hora and (d.hour or d.minute) else "%d/%m/%Y")


def _no_periodo(data, inicio, fim):
    d = _quando(data)
    if d is None:
        return not (inicio or fim)
    return (not inicio or d.date() >= inicio) and (not fim or d.date() <= fim)


def _quando(v):
    if not v:
        return None
    try:
        return v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:19])
    except ValueError:
        return None


def _simbolicas(c, pid, v0):
    """Votações simbólicas da matéria, no mesmo formato de metrics.detalhe_votacao."""
    if pid:
        rs = c.execute(text("SELECT * FROM votacoes_simbolicas WHERE proposicao_id = :p"), {"p": pid})
    elif v0 and v0.get("sigla_materia") and v0.get("numero_materia"):
        rs = c.execute(text("SELECT * FROM votacoes_simbolicas WHERE proposicao_id IS NULL AND sigla_materia = :s "
                            "AND numero_materia = :n"), {"s": v0["sigla_materia"], "n": v0["numero_materia"]})
    else:
        return []
    saida = []
    for r in rs.mappings():
        v = dict(r)
        v.update(secreta=False, simbolica=True, qtd_sim=None, qtd_nao=None, presidente=None)
        v["tipo_etapa"] = metrics.tipo_etapa(v)
        saida.append({"votacao": v, "simbolica": True, "orientacoes": [], "placar": [], "traidores": [],
                      "gov": None, "opo": None,
                      "contagem": {"SIM": None, "NÃO": None, "ABSTENÇÃO": None, "OBSTRUÇÃO": 0, "OUTROS": 0}})
    return saida


def dados(pid=None, vid=None, inicio=None, fim=None, buscar=True):
    """Tudo o que o PDF precisa. pid: id da proposição; vid: uma votação dela (quando a votação não
    traz o id da proposição, a matéria é achada por sigla/número/ano). None se não houver nada.
    Entram também as votações simbólicas. inicio/fim (date): só as votações desse período."""
    v0, v0_simb = None, False
    with engine.connect() as c:
        if vid:
            v0 = c.execute(text("SELECT * FROM votacoes WHERE id = :id"), {"id": vid}).mappings().first()
            v0_simb = not v0
            if not v0:
                v0 = c.execute(text("SELECT * FROM votacoes_simbolicas WHERE id = :id"), {"id": vid}).mappings().first()
            v0 = dict(v0) if v0 else None
            if v0 and not pid:
                pid = v0.get("proposicao_id")
        proc = None
        if pid:
            proc = c.execute(text(
                "SELECT pr.*, rd.nome relator_nome, rd.partido relator_partido, rd.uf relator_uf "
                "FROM processos pr LEFT JOIN deputados rd ON rd.codigo = pr.relator_id WHERE pr.id = :id"),
                {"id": pid}).mappings().first()
            proc = dict(proc) if proc else None
        autores = []
        if pid:
            autores = [dict(r) for r in c.execute(text(
                "SELECT d.nome, d.partido, d.uf, a.principal FROM autorias a "
                "JOIN deputados d ON d.codigo = a.codigo_deputado WHERE a.processo_id = :id "
                "ORDER BY a.principal DESC, d.nome"), {"id": pid}).mappings()]
        # votações nominais do Plenário da matéria (todas, de qualquer ano)
        if pid:
            ids = [r[0] for r in c.execute(text(
                "SELECT id FROM votacoes WHERE proposicao_id = :p ORDER BY data"), {"p": pid})]
        elif v0 and v0.get("sigla_materia") and v0.get("numero_materia"):
            ids = [r[0] for r in c.execute(text(
                "SELECT id FROM votacoes WHERE proposicao_id IS NULL AND sigla_materia = :s "
                "AND numero_materia = :n ORDER BY data"),
                {"s": v0["sigla_materia"], "n": v0["numero_materia"]})]
        else:
            ids = [vid] if v0 and not v0_simb else []
        simb = _simbolicas(c, pid, v0)
        historico = []
        if pid:
            vistos = set()
            for r in c.execute(text(
                    "SELECT situacao_anterior, situacao_nova, data_situacao, detectado_em FROM notificacoes "
                    "WHERE processo_id = :id AND tipo = 'situacao' ORDER BY data_situacao, detectado_em"),
                    {"id": pid}).mappings():
                k = (r["situacao_anterior"], r["situacao_nova"], r["data_situacao"])
                if k not in vistos:      # a mesma mudança é notificada a cada autor
                    vistos.add(k)
                    historico.append(dict(r))
    if not proc and not ids and not simb:
        return None
    vots = [d for d in (metrics.detalhe_votacao(i) for i in ids) if d]
    ids_nom = {x["votacao"]["id"] for x in vots}
    vots += [x for x in simb if x["votacao"]["id"] not in ids_nom]
    if inicio or fim:
        vots = [x for x in vots if _no_periodo(x["votacao"].get("data"), inicio, fim)]
    vots.sort(key=lambda x: (_quando(x["votacao"].get("data")) is None, _quando(x["votacao"].get("data")) or datetime.min))
    if proc:
        ident, ementa = proc.get("identificacao"), proc.get("ementa")
        import resumos
        proc["resumo"] = resumos.resumos_de([proc]).get(proc["id"])
    else:
        v = vots[0]["votacao"] if vots else v0
        v = v or {}
        ident = f"{v['sigla_materia']} {v['numero_materia']}/{v['ano_materia']}" if v.get("sigla_materia") \
            else "Votação no Plenário"
        ementa = v.get("descricao_materia")
    import objeto_votacao      # nº e autoria dos destaques, qual requerimento foi votado
    objeto_votacao.completar(ident, vots, buscar=buscar)
    return {"pid": pid, "proc": proc, "ident": ident or "Proposição", "ementa": ementa,
            "autores": autores, "votacoes": vots, "historico": historico}


# ---------------- PDF ----------------

def _selo(sigla, lado=360):
    """No lugar da foto do deputado: círculo com a sigla da proposição (PL, PEC, MPV...)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        esc = 4
        im = Image.new("RGBA", (lado * esc, lado * esc), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        dr.ellipse((0, 0, lado * esc - 1, lado * esc - 1), fill=CASA["cor_cl"])
        txt = (sigla or "?")[:4]
        tam = int(lado * esc * (0.42 if len(txt) <= 2 else 0.33 if len(txt) == 3 else 0.26))
        fonte = None
        for f in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "LiberationSans-Bold.ttf", "Arial Bold.ttf"):
            try:
                fonte = ImageFont.truetype(f, tam)
                break
            except OSError:
                continue
        fonte = fonte or ImageFont.load_default(size=tam)
        caixa = dr.textbbox((0, 0), txt, font=fonte)
        x = (lado * esc - (caixa[2] - caixa[0])) / 2 - caixa[0]
        y = (lado * esc - (caixa[3] - caixa[1])) / 2 - caixa[1]
        dr.text((x, y), txt, font=fonte, fill=CASA["cor_esc"])
        im = im.resize((lado, lado), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "PNG")
        out.seek(0)
        return out
    except Exception:
        return None


def _estilos(cor_esc):
    P = lambda nome, **k: ParagraphStyle(nome, fontName=k.pop("f", "Helvetica"), textColor=k.pop("c", TEXTO), **k)  # noqa: E731
    return {
        "sec": P("sec", f="Helvetica-Bold", fontSize=15, leading=19, c=cor_esc, spaceBefore=4, spaceAfter=4),
        "corpo": P("corpo", fontSize=11, leading=15),
        "resumo": P("resumo", fontSize=11.5, leading=15.5),
        "meta": P("meta", fontSize=9.5, leading=12.5, c=META),
        "rot": P("rot", f="Helvetica-Bold", fontSize=9.5, leading=12.5, c=META),
        "cel": P("cel", fontSize=9.5, leading=12),
        "celb": P("celb", f="Helvetica-Bold", fontSize=9.5, leading=12),
        "vt": P("vt", f="Helvetica-Bold", fontSize=12.5, leading=16, c=cor_esc),
        "num": P("num", f="Helvetica-Bold", fontSize=22, leading=26, c=cor_esc),
        "numrot": P("numrot", fontSize=9.5, leading=12, c=META),
    }


def _voto_cor(voto):
    return {"SIM": "#1e7b45", "NÃO": "#b42318"}.get(voto or "", "#4a5560")


def _voto_txt(voto):
    if not voto:
        return "—"
    return f'<font color="{_voto_cor(voto)}"><b>{_p(voto.capitalize())}</b></font>'


def _tabela(linhas, larguras, cab_fundo, zebra=True):
    t = Table(linhas, colWidths=larguras, repeatRows=1)
    est = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
           ("BACKGROUND", (0, 0), (-1, 0), cab_fundo),
           ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINHA),
           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
           ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    if zebra:
        for i in range(2, len(linhas), 2):
            est.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f7f9f8")))
    t.setStyle(TableStyle(est))
    return t


def _ficha(d, st, larg):
    pr = d["proc"] or {}
    linhas = []

    def add(rot, val):
        if val:
            linhas.append([Paragraph(rot, st["rot"]), Paragraph(val, st["cel"])])
    princ = [a for a in d["autores"] if a["principal"]]
    co = [a for a in d["autores"] if not a["principal"]]
    fmt = lambda a: f"{_p(a['nome'])} ({_p('-'.join(x for x in (a.get('partido'), a.get('uf')) if x))})"  # noqa: E731
    autoria = ", ".join(fmt(a) for a in princ)
    if co:
        autoria += (" · " if autoria else "") + f"coautoria: {', '.join(fmt(a) for a in co[:25])}" + \
            (f" e mais {len(co) - 25}" if len(co) > 25 else "")
    add("Autoria", autoria or _p(pr.get("autoria")))
    add("Apresentação", _br(pr.get("data_apresentacao")))
    add("Forma de apreciação", _p(pr.get("tipo_conteudo")))
    add("Regime", _p(pr.get("regime")))
    sit = _p(pr.get("situacao"))
    if sit and pr.get("data_situacao"):
        sit += f" <font color='#5f6b76'>(desde {_br(pr['data_situacao'])})</font>"
    add("Situação", sit)
    add("Onde está", _p(pr.get("orgao")))
    if pr.get("relator_nome"):
        suf = "-".join(x for x in (pr.get("relator_partido"), pr.get("relator_uf")) if x)
        add("Relator", _p(pr["relator_nome"] + (f" ({suf})" if suf else "")))
    if pr.get("virou_norma"):
        add("Norma", "Transformada em norma jurídica")
    url = pr.get("url") or (f"https://www.camara.leg.br/propostas-legislativas/{d['pid']}" if d["pid"] else "")
    if url:
        add("Ficha na Câmara", f'<link href="{_p(url)}" color="#1f4fa8">{_p(url)}</link>')
    if not linhas:
        return None
    t = Table(linhas, colWidths=[38 * mm, larg - 38 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINHA),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                           ("LEFTPADDING", (0, 0), (-1, -1), 2)]))
    return t


def _numeros(d, st, larg, cor_cl):
    vs = [x["votacao"] for x in d["votacoes"]]
    aprov = sum(1 for v in vs if _aprov(v) is True)
    rej = sum(1 for v in vs if _aprov(v) is False)
    com_ori = [x for x in d["votacoes"] if x["opo"] in ("SIM", "NÃO") and _aprov(x["votacao"]) is not None]
    with_opo = sum(1 for x in com_ori if (x["opo"] == "SIM") == _aprov(x["votacao"]))
    n_simb = sum(1 for x in d["votacoes"] if x.get("simbolica"))
    cels = [(len(vs) - n_simb, "votações nominais")] + ([(n_simb, "simbólicas")] if n_simb else []) + \
        [(aprov, "aprovadas"), (rej, "rejeitadas")]
    if com_ori:
        cels.append((f"{with_opo}/{len(com_ori)}", "resultado como a Oposição orientou"))
    t = Table([[[Paragraph(str(n), st["num"]), Paragraph(r, st["numrot"])] for n, r in cels]],
              colWidths=[larg / len(cels)] * len(cels))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), cor_cl), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LINEAFTER", (0, 0), (-2, 0), 0.6, colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    return t


def _aprov(v):
    """True/False/None (o SQLite devolve 1/0)."""
    a = v.get("aprovacao")
    return None if a is None else bool(a)


def _resultado(v):
    if _aprov(v) is True:
        return '<font color="#1e7b45"><b>Aprovada</b></font>'
    if _aprov(v) is False:
        return '<font color="#b42318"><b>Rejeitada</b></font>'
    return "—"


def _rotulo(v):
    return v.get("rotulo") or v.get("tipo_etapa") or "Votação"


def _etapa_cel(v):
    """Etapa no quadro-resumo: 'Requerimento de adiamento da discussão'; 'Destaque nº 7 · PL' + objeto."""
    txt = f"<b>{_p(_rotulo(v))}</b>"
    if v.get("dtq_autoria"):
        txt += f" · autoria: {_p(v['dtq_autoria'])}"
    compl = v.get("complemento")
    if compl:
        compl = compl if len(compl) <= 110 else compl[:108].rstrip() + "…"
        txt += f'<br/><font size="7">{_p(compl)}</font>'
    return txt


def _quadro_votacoes(d, st, larg, cab_fundo):
    cab = [Paragraph(f"<b>{x}</b>", st["cel"]) for x in
           ("Data", "Etapa", "Resultado", "Sim", "Não", "Abst.", "Governo", "Oposição")]
    linhas, spans = [cab], []
    for x in d["votacoes"]:
        v, c = x["votacao"], x["contagem"]
        if x.get("simbolica"):
            spans.append(len(linhas))
            linhas.append([Paragraph(_br(v["data"], hora=True), st["cel"]), Paragraph(_etapa_cel(v), st["cel"]),
                           Paragraph(_resultado(v), st["cel"]), Paragraph("<i>votação simbólica</i>", st["cel"]),
                           "", "", "", ""])
            continue
        linhas.append([Paragraph(_br(v["data"], hora=True), st["cel"]), Paragraph(_etapa_cel(v), st["cel"]),
                       Paragraph(_resultado(v), st["cel"]), Paragraph(str(c["SIM"]), st["cel"]),
                       Paragraph(str(c["NÃO"]), st["cel"]), Paragraph(str(c["ABSTENÇÃO"]), st["cel"]),
                       Paragraph(_voto_txt(x["gov"]), st["cel"]), Paragraph(_voto_txt(x["opo"]), st["cel"])])
    w = [27 * mm, 0, 22 * mm, 12 * mm, 12 * mm, 12 * mm, 20 * mm, 20 * mm]
    w[1] = larg - sum(w)
    t = _tabela(linhas, w, cab_fundo)
    if spans:
        t.setStyle(TableStyle([("SPAN", (3, i), (7, i)) for i in spans]))
    return t


def _bloco_votacao(n, x, st, larg, cab_fundo, cor_cl):
    v, c = x["votacao"], x["contagem"]
    out = [Paragraph(f"{n}. {_p(_rotulo(v))} · {_br(v['data'], hora=True)}", st["vt"])]
    if x.get("simbolica"):
        if v.get("complemento"):
            out.append(Paragraph(f"<b>Em votação:</b> {_p(v['complemento'])}", st["corpo"]))
        elif v.get("etapa"):
            out.append(Paragraph(f"<b>Em votação:</b> {_p(v['etapa'])}", st["corpo"]))
        if v.get("descricao_votacao"):
            out.append(Paragraph(f"<b>Resultado:</b> {_p(v['descricao_votacao'])}", st["corpo"]))
        out.append(Paragraph("Votação simbólica — sem placar nem votos individuais.", st["meta"]))
        return out
    if v.get("dtq_numero") or v.get("dtq_autoria"):
        out.append(Paragraph(f"<b>Destaque:</b> nº {_p(v.get('dtq_numero') or '—')} · "
                             f"<b>Autoria:</b> {_p(v.get('dtq_autoria') or 'não informada')}", st["corpo"]))
        if v.get("dtq_objeto"):
            out.append(Paragraph(f"<b>Objeto do destaque:</b> {_p(v['dtq_objeto'])}", st["corpo"]))
    elif v.get("objeto"):      # o título já diz o que foi votado
        if v.get("complemento"):
            out.append(Paragraph(f"<b>Requerimento:</b> {_p(v['complemento'])}", st["corpo"]))
    elif v.get("complemento"):
        out.append(Paragraph(f"<b>Em votação:</b> {_p(v['complemento'])}", st["corpo"]))
    elif v.get("etapa"):
        out.append(Paragraph(f"<b>Em votação:</b> {_p(v['etapa'])}", st["corpo"]))
    if v.get("descricao_votacao"):
        out.append(Paragraph(f"<b>Resultado:</b> {_p(v['descricao_votacao'])}", st["corpo"]))
    extras = []
    if c["OBSTRUÇÃO"]:
        extras.append(f"{c['OBSTRUÇÃO']} obstrução(ões)")
    if c["OUTROS"]:
        extras.append(f"{c['OUTROS']} outros registros")
    if v.get("presidente"):
        extras.append(f"presidente da sessão: {_p(v['presidente'])}")
    out.append(Paragraph(
        f'<font color="#1e7b45"><b>{c["SIM"]} Sim</b></font> × <font color="#b42318"><b>{c["NÃO"]} Não</b></font>'
        f' · {c["ABSTENÇÃO"]} abstenção(ões)' + (" · " + " · ".join(extras) if extras else ""), st["corpo"]))
    if v.get("secreta"):
        out.append(Paragraph("Votação secreta — sem votos individuais.", st["meta"]))
    out.append(Spacer(1, 2 * mm))

    # orientações: institucionais primeiro, em 4 colunas
    oris = x["orientacoes"]
    if oris:
        cels = [Paragraph(f"{_p(o['lideranca'])}: {_voto_txt(o['voto'])}",
                          st["celb"] if o["lideranca"] in INSTITUCIONAIS else st["cel"]) for o in oris]
        cols = 4
        grade = [cels[i:i + cols] + [""] * (cols - len(cels[i:i + cols])) for i in range(0, len(cels), cols)]
        t = Table(grade, colWidths=[larg / cols] * cols)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), cor_cl),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                               ("LEFTPADDING", (0, 0), (-1, -1), 5)]))
        out += [Paragraph("<b>Orientações</b>", st["meta"]), t, Spacer(1, 2 * mm)]

    # votos por partido
    if x["placar"] and not v.get("secreta"):
        linhas = [[Paragraph(f"<b>{h}</b>", st["cel"]) for h in ("Partido", "Orientação", "Sim", "Não", "Outros", "Total")]]
        for p in x["placar"]:
            linhas.append([Paragraph(_p(p["partido"]), st["celb"]), Paragraph(_voto_txt(p.get("orientacao")), st["cel"]),
                           Paragraph(str(p["SIM"]), st["cel"]), Paragraph(str(p["NÃO"]), st["cel"]),
                           Paragraph(str(p["outros"]), st["cel"]), Paragraph(str(p["total"]), st["cel"])])
        w = [0, 26 * mm, 16 * mm, 16 * mm, 16 * mm, 16 * mm]
        w[0] = larg - sum(w)
        out += [Paragraph("<b>Votos por partido</b> (partido na data do voto)", st["meta"]),
                _tabela(linhas, w, cab_fundo)]
    trai = x["traidores"]
    if trai:
        nomes = "; ".join(f"{_p(r['nome'])} ({_p(r['partido'] or '')}-{_p(r['uf'] or '')}) {_p(r['voto'].lower())}"
                          for r in sorted(trai, key=lambda r: (r["partido"] or "", r["nome"])))
        out += [Spacer(1, 1.5 * mm),
                Paragraph(f"<b>Contra a orientação do próprio partido ({len(trai)}):</b> {nomes}", st["meta"])]
    return out


def _rotulo_cab(d):
    n_simb = sum(1 for x in d["votacoes"] if x.get("simbolica"))
    n = len(d["votacoes"]) - n_simb
    rotulo = (f"{n} votação(ões) nominal(is)" if n else "") + \
        (f"{' e ' if n else ''}{n_simb} simbólica(s)" if n_simb else "")
    return f"{rotulo} no Plenário" if rotulo else "Sem votação no Plenário"


def _corpo(d, st, larg, cab_fundo, cor_cl, compacto=False):
    """Conteúdo de uma matéria (sem o cabeçalho de página)."""
    pr = d["proc"] or {}
    hist = []
    if pr.get("resumo"):
        hist += [Paragraph("Resumo", st["sec"]), Paragraph(_p(pr["resumo"]), st["resumo"]), Spacer(1, 2 * mm)]
    if d.get("ementa") and not (compacto and pr.get("resumo")):      # no lote, o resumo basta
        hist += [Paragraph("Ementa", st["sec"]), Paragraph(_p(d["ementa"]), st["corpo"]), Spacer(1, 3 * mm)]
    ficha = _ficha(d, st, larg)
    if ficha:
        hist += [Paragraph("Ficha", st["sec"]), ficha, Spacer(1, 4 * mm)]
    if d["votacoes"]:
        hist += [CondPageBreak(40 * mm if compacto else 60 * mm), Paragraph("Votações no Plenário", st["sec"]),
                 _numeros(d, st, larg, cor_cl), Spacer(1, 3 * mm),
                 _quadro_votacoes(d, st, larg, cab_fundo), Spacer(1, 5 * mm)]
        for i, x in enumerate(d["votacoes"], 1):
            bloco = _bloco_votacao(i, x, st, larg, cab_fundo, cor_cl)
            hist += [CondPageBreak(50 * mm if not compacto else 35 * mm), KeepTogether(bloco[:4]), *bloco[4:],
                     Spacer(1, (5 if not compacto else 3) * mm)]
    else:
        hist += [Paragraph("Votações no Plenário", st["sec"]),
                 Paragraph("O painel não tem votação desta proposição no Plenário (no período).", st["meta"]),
                 Spacer(1, 4 * mm)]
    if d["historico"]:
        linhas = [[Paragraph(f"<b>{h}</b>", st["cel"]) for h in ("Data", "Situação anterior", "Nova situação")]]
        for h in d["historico"]:
            linhas.append([Paragraph(_br(h["data_situacao"] or h["detectado_em"]), st["cel"]),
                           Paragraph(_p(h["situacao_anterior"]), st["cel"]),
                           Paragraph(_p(h["situacao_nova"]), st["celb"])])
        w = [24 * mm, (larg - 24 * mm) / 2, (larg - 24 * mm) / 2]
        hist += [CondPageBreak(40 * mm), Paragraph("Mudanças de situação acompanhadas pelo painel", st["sec"]),
                 _tabela(linhas, w, cab_fundo)]
    return hist


def gerar(d, agora=None):
    agora = agora or datetime.now()
    cor, cor_esc, cor_cl = (colors.HexColor(CASA[k]) for k in ("cor", "cor_esc", "cor_cl"))
    st = _estilos(cor_esc)
    pr = d["proc"] or {}
    sit = pr.get("situacao") or ""
    sit = sit if len(sit) <= 48 else sit[:46].rstrip() + "…"
    sub = " · ".join(x for x in (sit, pr.get("orgao")) if x) or CASA["nome"]
    rotulo = _rotulo_cab(d)
    sigla = (pr.get("sigla") or (d["ident"].split() or [""])[0]) if d["ident"] else ""
    cabecalho = fazer_cabecalho(d["ident"], sub, _selo(sigla), rotulo, agora,
                                kicker="R E L A T Ó R I O   D A   P R O P O S I Ç Ã O", rodape="Proposição")
    buf = io.BytesIO()
    larg = A4[0] - 32 * mm
    doc = BaseDocTemplate(buf, pagesize=A4, title=f"Relatório — {d['ident']}", author=CASA["painel"],
                          leftMargin=16 * mm, rightMargin=16 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    f1 = Frame(16 * mm, 20 * mm, larg, A4[1] - 20 * mm - 62 * mm, id="p1", leftPadding=0, rightPadding=0)
    fn = Frame(16 * mm, 20 * mm, larg, A4[1] - 20 * mm - 21 * mm, id="pn", leftPadding=0, rightPadding=0)
    doc.addPageTemplates([PageTemplate("primeira", [f1], onPage=cabecalho, autoNextPageTemplate="demais"),
                          PageTemplate("demais", [fn], onPage=cabecalho)])
    cab_fundo = colors.HexColor("#eef3f0")

    hist = _corpo(d, st, larg, cab_fundo, cor_cl)
    doc.build(hist, canvasmaker=_Numerado)
    return buf.getvalue()


class _Marcador(Flowable):
    """Entrada no sumário lateral do leitor de PDF."""
    def __init__(self, titulo, chave):
        super().__init__()
        self.titulo, self.chave = titulo, chave
        self.width = self.height = 0

    def draw(self):
        self.canv.bookmarkPage(self.chave)
        self.canv.addOutlineEntry(self.titulo, self.chave, level=0)


def gerar_varios(lista, titulo="Votações no Plenário", sub="", agora=None, aviso=None):
    """Várias matérias num só PDF, uma abaixo da outra: um cabeçalho só e, para cada matéria, um título
    com a situação e as mesmas seções do relatório individual."""
    agora = agora or datetime.now()
    cor, cor_esc, cor_cl = (colors.HexColor(CASA[k]) for k in ("cor", "cor_esc", "cor_cl"))
    st = _estilos(cor_esc)
    st["mat"] = ParagraphStyle("mat", fontName="Helvetica-Bold", fontSize=19, leading=23, textColor=cor_esc)
    st["matsub"] = ParagraphStyle("matsub", fontName="Helvetica", fontSize=10, leading=13, textColor=META)
    total = sum(len(d["votacoes"]) for d in lista)
    rotulo = f"{len(lista)} matéria(s) · {total} votação(ões)"
    cabecalho = fazer_cabecalho(titulo, sub or CASA["nome"], _selo("CD"), rotulo, agora,
                                kicker="R E L A T Ó R I O   D E   V O T A Ç Õ E S", rodape="Votações")
    buf = io.BytesIO()
    larg = A4[0] - 32 * mm
    doc = BaseDocTemplate(buf, pagesize=A4, title=titulo, author=CASA["painel"],
                          leftMargin=16 * mm, rightMargin=16 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    f1 = Frame(16 * mm, 20 * mm, larg, A4[1] - 20 * mm - 62 * mm, id="p1", leftPadding=0, rightPadding=0)
    fn = Frame(16 * mm, 20 * mm, larg, A4[1] - 20 * mm - 21 * mm, id="pn", leftPadding=0, rightPadding=0)
    doc.addPageTemplates([PageTemplate("primeira", [f1], onPage=cabecalho, autoNextPageTemplate="demais"),
                          PageTemplate("demais", [fn], onPage=cabecalho)])
    cab_fundo = colors.HexColor("#eef3f0")
    el = []
    if aviso:
        el += [Paragraph(_p(aviso), st["meta"]), Spacer(1, 3 * mm)]
    for i, d in enumerate(lista):
        pr = d["proc"] or {}
        if i:
            el += [Spacer(1, 2 * mm), HRFlowable(width="100%", thickness=1.2, color=cor), Spacer(1, 4 * mm)]
        info = " · ".join(x for x in (pr.get("situacao"), pr.get("orgao"), _rotulo_cab(d)) if x)
        el += [CondPageBreak(60 * mm), _Marcador(d["ident"], f"m{i}"),
               Paragraph(_p(d["ident"]), st["mat"]), Paragraph(_p(info), st["matsub"]), Spacer(1, 2 * mm)]
        el += _corpo(d, st, larg, cab_fundo, cor_cl, compacto=True)
    doc.build(el, canvasmaker=_Numerado)
    return buf.getvalue()
