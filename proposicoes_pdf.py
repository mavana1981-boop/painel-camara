"""Relatório de proposições em PDF (aba Proposições · exportar).

Mesmo cabeçalho dos outros PDFs do painel (faixa com foto). As proposições vêm agrupadas pelo regime
de tramitação — urgências primeiro — mantendo, dentro de cada grupo, a ordem escolhida na tela.
"""
import io
from datetime import date, datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, KeepTogether, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

from agenda_pdf import CASA, LINHA, META, OURO, SUAVE, TEXTO, _Numerado, _p, fazer_cabecalho, foto_redonda

LIMITE = 1500   # acima disso o PDF fica pesado demais; o Excel continua sem limite

GRUPOS = [
    ("urgencia", "Em urgência", "#b42318", "#fde2e1"),
    ("prioridade", "Prioridade", "#a15c00", "#fff1d6"),
    ("especial", "Regime especial", "#1f4fa8", "#e6eefc"),
    ("ordinario", "Tramitação ordinária", CASA["cor"], CASA["cor_cl"]),
    ("", "Regime não informado", "#5f6b76", "#eef1f3"),
]


def _br(iso):
    try:
        return datetime.fromisoformat(str(iso)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def _relator(p):
    if not p.get("relator_nome"):
        return ""
    suf = "-".join(x for x in (p.get("relator_partido"), p.get("relator_uf")) if x)
    return p["relator_nome"] + (f" ({suf})" if suf else "")


def gerar(parl, props, filtros_txt, titulo_papel, extras=(), agora=None, relatoria=False):
    """parl: dict do deputado. props: resultado de paineis.proposicoes(limite=None). Devolve bytes."""
    agora = agora or datetime.now()
    cor, cor_esc, cor_cl = (colors.HexColor(CASA[k]) for k in ("cor", "cor_esc", "cor_cl"))
    nome = parl.get("nome") or ""
    sub = " · ".join(x for x in (parl.get("partido"), parl.get("uf"), CASA["nome"]) if x)
    kicker = "R E L A T Ó R I O   D E   R E L A T O R I A S" if relatoria else \
        "R E L A T Ó R I O   D E   P R O P O S I Ç Õ E S"
    cabecalho = fazer_cabecalho(nome, sub, foto_redonda(parl.get("foto")), titulo_papel, agora,
                                kicker=kicker, rodape="Proposições")
    st = {
        "filtro": ParagraphStyle("filtro", fontName="Helvetica", fontSize=10.5, leading=14, textColor=META),
        "num": ParagraphStyle("num", fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=cor_esc),
        "numu": ParagraphStyle("numu", fontName="Helvetica-Bold", fontSize=24, leading=28,
                               textColor=colors.HexColor("#b42318")),
        "numrot": ParagraphStyle("numrot", fontName="Helvetica", fontSize=10, leading=13, textColor=META),
        "sec": ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=cor_esc),
        "secsub": ParagraphStyle("secsub", fontName="Helvetica", fontSize=11, leading=15, textColor=META,
                                 alignment=2),
        "mini_t": ParagraphStyle("mini_t", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=cor_esc),
        "mini": ParagraphStyle("mini", fontName="Helvetica", fontSize=9.5, leading=12.5, textColor=TEXTO),
        "mini_n": ParagraphStyle("mini_n", fontName="Helvetica-Bold", fontSize=9.5, leading=12.5,
                                 textColor=TEXTO, alignment=2),
        "id": ParagraphStyle("id", fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=TEXTO),
        "resumo": ParagraphStyle("resumo", fontName="Helvetica", fontSize=11.2, leading=15,
                                 textColor=colors.HexColor("#1f2933")),
        "ementa": ParagraphStyle("ementa", fontName="Helvetica", fontSize=10.8, leading=14.6,
                                 textColor=colors.HexColor("#2b353d")),
        "meta": ParagraphStyle("meta", fontName="Helvetica", fontSize=9.6, leading=13, textColor=META),
        "sit": ParagraphStyle("sit", fontName="Helvetica-Bold", fontSize=9.8, leading=13, textColor=TEXTO),
        "nota": ParagraphStyle("nota", fontName="Helvetica-Oblique", fontSize=9, leading=12, textColor=SUAVE),
        "vazio": ParagraphStyle("vazio", fontName="Helvetica-Oblique", fontSize=11.5, leading=15, textColor=SUAVE),
    }

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, title=f"Proposições – {nome}", author=CASA["painel"],
                          leftMargin=16 * mm, rightMargin=16 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    larg = A4[0] - 32 * mm
    doc.addPageTemplates([
        PageTemplate("primeira", [Frame(16 * mm, 20 * mm, larg, A4[1] - 20 * mm - 62 * mm, leftPadding=0,
                                        rightPadding=0, topPadding=0, bottomPadding=0)],
                     onPage=cabecalho, autoNextPageTemplate="demais"),
        PageTemplate("demais", [Frame(16 * mm, 20 * mm, larg, A4[1] - 20 * mm - 21 * mm, leftPadding=0,
                                      rightPadding=0, topPadding=0, bottomPadding=0)], onPage=cabecalho),
    ])
    s = []
    itens = props["itens"]
    cortado = len(itens) > LIMITE
    itens = itens[:LIMITE]

    if filtros_txt:
        s += [Paragraph(f"<b>Recorte:</b> {_p(filtros_txt)}", st["filtro"]), Spacer(1, 4 * mm)]

    # ---- números do recorte
    caixas = [(props["total"], "proposições no recorte", st["num"]),
              (props["tramitando"], "em tramitação", st["num"]),
              (props["urgentes"], "em urgência (tramitando)", st["numu"])]
    caixas.append((props["relator"], "como relator atual", st["num"]) if relatoria
                  else (props["normas"], "viraram norma jurídica", st["num"]))
    w = larg / len(caixas)
    t = Table([[Table([[Paragraph(f"{n:,}".replace(",", "."), est)], [Paragraph(rot, st["numrot"])]],
                      colWidths=[w - 8 * mm]) for n, rot, est in caixas]], colWidths=[w] * len(caixas))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), cor_cl), ("LINEBELOW", (0, 0), (-1, -1), 2, OURO),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LINEAFTER", (0, 0), (-2, -1), 4, colors.white)]))
    s += [t, Spacer(1, 5 * mm)]

    # ---- quadro-resumo: regime, onde está e situação
    def _mini(titulo, lst, rot, n=5):
        linhas = [[Paragraph(titulo, st["mini_t"]), ""]]
        linhas += [[Paragraph(_p(x[rot] if x[rot] != "—" else "(não informado)"), st["mini"]), Paragraph(str(x["n"]), st["mini_n"])] for x in lst[:n]]
        if len(lst) > n:
            linhas.append([Paragraph(f"outros ({sum(x['n'] for x in lst[n:])})", st["meta"]), ""])
        return linhas

    col = (larg - 8 * mm) / 3
    quadros = [_mini("Regime de tramitação", props["por_regime"], "regime"),
               _mini("Onde está agora", props["por_orgao"], "orgao"),
               _mini("Situação atual", props["por_situacao"], "situacao")]
    tabs = []
    for q in quadros:
        tq = Table(q, colWidths=[col - 12 * mm, 10 * mm])
        tq.setStyle(TableStyle([("SPAN", (0, 0), (-1, 0)), ("LINEBELOW", (0, 0), (-1, 0), 0.8, cor),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 2.2),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2)]))
        tabs.append(tq)
    tr = Table([tabs], colWidths=[col + 4 * mm, col + 4 * mm, col])
    tr.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm)]))
    s += [tr, Spacer(1, 8 * mm)]

    if not itens and not extras:
        s.append(Paragraph("Nenhuma proposição neste recorte.", st["vazio"]))

    # ---- proposições, por regime
    por_grupo = {}
    for p in itens:
        por_grupo.setdefault(p.get("grupo_regime") or "", []).append(p)
    for chave, rotulo, cor_g, fundo_g in GRUPOS:
        lst = por_grupo.get(chave)
        if not lst:
            continue
        cg = colors.HexColor(cor_g)
        cab = Table([[Paragraph(f'<font color="{cor_g}">{rotulo}</font>', st["sec"]),
                      Paragraph(f"{len(lst)} proposiç{'ões' if len(lst) != 1 else 'ão'}", st["secsub"])]],
                    colWidths=[larg * 0.6, larg * 0.4])
        cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LINEBELOW", (0, 0), (-1, 0), 1.2, cg),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 0)]))
        s += [CondPageBreak(40 * mm), cab, Spacer(1, 2 * mm)]
        for p in lst:
            s.append(KeepTogether([_cartao(p, st, larg, cg, colors.HexColor(fundo_g)), Spacer(1, 2.5 * mm)]))
        s.append(Spacer(1, 5 * mm))

    if cortado:
        s.append(Paragraph(f"O PDF mostra as {LIMITE} primeiras de {props['total']} proposições. "
                           "Use a exportação em Excel para a lista completa.", st["nota"]))

    # ---- relatorias vistas nas pautas das comissões
    if extras:
        cab = Table([[Paragraph("Outras relatorias vistas nas pautas das comissões", st["sec"]),
                      Paragraph(str(len(extras)), st["secsub"])]], colWidths=[larg * 0.8, larg * 0.2])
        cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LINEBELOW", (0, 0), (-1, 0), 1.2, cor),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        s += [CondPageBreak(40 * mm), cab, Spacer(1, 2 * mm)]
        for r in extras:
            meta = " · ".join(x for x in (
                r.get("colegiado"), f"relator desde {_br(r.get('data_designacao'))}" if r.get("data_designacao") else "",
                f"saiu em {_br(r.get('data_destituicao'))}" if r.get("data_destituicao") else "") if x)
            p = {"identificacao": r.get("identificacao"), "ementa": r.get("ementa"), "url": r.get("url"),
                 "situacao": r.get("situacao"), "data_situacao": r.get("data_situacao"), "_meta": meta}
            s.append(KeepTogether([_cartao(p, st, larg, cor, cor_cl), Spacer(1, 2.5 * mm)]))

    import questoes
    sem = sum(1 for p in itens if not p.get("resumo") and (p.get("ementa") or "").strip()) \
        if questoes.configurado() else 0
    nota_resumo = (" As ementas resumidas foram geradas por IA a partir da ementa oficial, que vem logo abaixo de "
                   "cada uma; confira antes de citar.")
    if sem:
        nota_resumo += (f" {sem} proposição(ões) saíram só com a ementa oficial porque o resumo ainda estava "
                        "sendo gerado; exporte de novo em alguns minutos para tê-las completas.")
    s += [Spacer(1, 4 * mm), Paragraph(
        "Fonte: dados abertos da Câmara dos Deputados (proposições, autores e pautas). “Relator atual” é o "
        "informado no último andamento da proposição. Emendas, substitutivos, pareceres e redações ficam de fora."
        + (nota_resumo if any(p.get("resumo") for p in itens) or sem else ""), st["nota"])]
    doc.build(s, canvasmaker=_Numerado)
    return buf.getvalue()


def _cartao(p, st, larg, cor_g, fundo_g):
    """Uma proposição: faixa colorida à esquerda (regime), identificação, ementa e andamento."""
    ident = _p(p.get("identificacao"))
    if p.get("url"):
        ident = f'<link href="{_p(p["url"])}" color="#1f2933">{ident}</link>'
    selos = []
    if p.get("papel"):
        selos.append(p["papel"])
    if p.get("virou_norma"):
        selos.append("virou norma")
    cab = ident + (f'  <font size="9" color="#5f6b76">{_p(" · ".join(selos))}</font>' if selos else "")
    data_ap = _br(p.get("data_apresentacao"))
    corpo = [Paragraph(cab, st["id"])]
    if p.get("resumo"):          # ementa resumida (IA) em destaque; a oficial logo abaixo, menor
        corpo += [Spacer(1, 1 * mm), Paragraph(_p(p["resumo"]), st["resumo"])]
        if p.get("ementa"):
            corpo += [Spacer(1, 0.8 * mm), Paragraph(f"<i>Ementa:</i> {_p(p['ementa'])}", st["meta"])]
    elif p.get("ementa"):
        corpo += [Spacer(1, 1 * mm), Paragraph(_p(p["ementa"]), st["ementa"])]
    if p.get("autoria") and (not p.get("principal") or "relator" in (p.get("papel") or "")):
        aut = p["autoria"][:220] + ("…" if len(p["autoria"]) > 220 else "")
        corpo += [Spacer(1, 0.8 * mm), Paragraph(f"Autoria: {_p(aut)}", st["meta"])]

    # andamento: situação em destaque, depois órgão / data / dias parado / relator
    andamento = []
    if p.get("situacao"):
        andamento.append(Paragraph(_p(p["situacao"]), st["sit"]))
    info = []
    if p.get("orgao"):
        info.append(f"<b>{_p(p['orgao'])}</b>")
    if p.get("data_situacao"):
        info.append(f"desde {_br(p['data_situacao'])}")
    if p.get("dias_parado") is not None and p["dias_parado"] > 30:
        info.append(f"{p['dias_parado']} dias parado")
    if p.get("_meta"):
        info.append(_p(p["_meta"]))
    if info:
        andamento.append(Paragraph(" · ".join(info), st["meta"]))
    rel = _relator(p)
    if rel and "relator" not in (p.get("papel") or ""):
        andamento.append(Paragraph(f"Relator: {_p(rel)}", st["meta"]))
    if p.get("regime") and not str(p["regime"]).lower().startswith("ordin"):
        andamento.append(Paragraph(_p(p["regime"]), st["meta"]))
    if data_ap:
        andamento.append(Paragraph(f"Apresentada em {data_ap}", st["meta"]))

    t = Table([[corpo, andamento]], colWidths=[larg * 0.58, larg * 0.42])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBEFORE", (0, 0), (0, 0), 3, cor_g),
        ("BACKGROUND", (1, 0), (1, 0), fundo_g),
        ("LEFTPADDING", (0, 0), (0, 0), 8), ("RIGHTPADDING", (0, 0), (0, 0), 8),
        ("LEFTPADDING", (1, 0), (1, 0), 8), ("RIGHTPADDING", (1, 0), (1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINHA),
    ]))
    return t
