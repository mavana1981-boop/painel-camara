"""Relatório de realizações do parlamentar em PDF (Meu Parlamentar · Clipping).

Reúne, mês a mês, as solicitações atendidas pela equipe (com o resumo do que foi feito e os anexos) e,
se pedido, as aprovações legislativas do clipping (proposições de autoria e relatorias aprovadas).
Usa o mesmo cabeçalho do PDF da agenda.
"""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph, Spacer,
                                Table, TableStyle)

from agenda_pdf import (CASA, LINHA, MESES, META, OURO, SUAVE, TEXTO, _Numerado, _p, fazer_cabecalho,
                        foto_redonda)

ROTULOS = {"realizacao": "Solicitação atendida", "autoria": "Proposição aprovada", "relatoria": "Relatoria aprovada"}
CORES = {"realizacao": "#8a6d22", "autoria": CASA["cor"], "relatoria": "#2563c9"}


def _data_br(iso):
    try:
        return datetime.fromisoformat(str(iso)[:10]).strftime("%d/%m")
    except ValueError:
        return str(iso or "")


def gerar(parl, eventos, inicio, fim, rotulo, agora=None):
    """eventos: dicts com tipo (realizacao/autoria/relatoria), data, titulo, resumo, ementa, anexos, rotulo."""
    agora = agora or datetime.now()
    cor, cor_esc, cor_cl = (colors.HexColor(CASA[k]) for k in ("cor", "cor_esc", "cor_cl"))
    nome = parl.get("nome") or ""
    sub = " · ".join(x for x in (parl.get("partido"), parl.get("uf"), CASA["nome"]) if x)
    cabecalho = fazer_cabecalho(nome, sub, foto_redonda(parl.get("foto")), rotulo, agora,
                                kicker="R E L A T Ó R I O   D E   R E A L I Z A Ç Õ E S")
    st = {
        "sec": ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=15.5, leading=19, textColor=cor_esc),
        "secsub": ParagraphStyle("secsub", fontName="Helvetica", fontSize=11.5, leading=15, textColor=META),
        "data": ParagraphStyle("data", fontName="Helvetica-Bold", fontSize=12.5, leading=15.5, textColor=cor),
        "rot": ParagraphStyle("rot", fontName="Helvetica-Bold", fontSize=9.5, leading=12),
        "tit": ParagraphStyle("tit", fontName="Helvetica-Bold", fontSize=13, leading=16.5, textColor=TEXTO),
        "txt": ParagraphStyle("txt", fontName="Helvetica", fontSize=11.5, leading=15.5, textColor=colors.HexColor("#2b353d")),
        "meta": ParagraphStyle("meta", fontName="Helvetica", fontSize=10.5, leading=14, textColor=META),
        "num": ParagraphStyle("num", fontName="Helvetica-Bold", fontSize=26, leading=30, textColor=cor_esc),
        "numrot": ParagraphStyle("numrot", fontName="Helvetica", fontSize=10.5, leading=13.5, textColor=META),
        "vazio": ParagraphStyle("vazio", fontName="Helvetica-Oblique", fontSize=11.5, leading=15, textColor=SUAVE),
    }

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, title=f"Realizações – {nome} – {rotulo}", author=CASA["painel"],
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
    cont = {k: sum(1 for e in eventos if e["tipo"] == k) for k in ROTULOS}
    tipos = [k for k in ROTULOS if cont[k] or k == "realizacao"]
    caixas = [[Paragraph(str(cont[k]), st["num"]), Paragraph(ROTULOS[k].replace("Solicitação atendida",
              "Solicitações atendidas").replace("Proposição aprovada", "Proposições aprovadas")
              .replace("Relatoria aprovada", "Relatorias aprovadas"), st["numrot"])] for k in tipos]
    t = Table([[Table([[c[0]], [c[1]]], colWidths=[larg / len(caixas) - 6 * mm]) for c in caixas]],
              colWidths=[larg / len(caixas)] * len(caixas))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), cor_cl), ("LINEBELOW", (0, 0), (-1, -1), 2, OURO),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                           ("LINEAFTER", (0, 0), (-2, -1), 4, colors.white)]))
    s += [t, Spacer(1, 7 * mm)]

    if not eventos:
        s.append(Paragraph("Nenhuma realização registrada no período.", st["vazio"]))

    meses = {}
    for e in sorted(eventos, key=lambda e: (e["data"], e["tipo"] != "realizacao")):
        meses.setdefault(e["data"][:7], []).append(e)
    for chave in sorted(meses):
        itens = meses[chave]
        ano, mes = int(chave[:4]), int(chave[5:7])
        n_r = sum(1 for e in itens if e["tipo"] == "realizacao")
        resumo = f"{len(itens)} registro{'s' if len(itens) != 1 else ''}" + (
            f" · {n_r} solicitaç{'ões' if n_r != 1 else 'ão'} atendida{'s' if n_r != 1 else ''}" if n_r else "")
        cab = Table([[Paragraph(f"{MESES[mes - 1].capitalize()} de {ano}", st["sec"]), Paragraph(resumo, st["secsub"])]],
                    colWidths=[70 * mm, larg - 70 * mm])
        cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LINEBELOW", (0, 0), (-1, 0), 1.2, cor),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 0)]))
        linhas, estilo = [], [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, -1), 0),
                              ("LEFTPADDING", (1, 0), (1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                              ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINHA)]
        for j, e in enumerate(itens):
            corx = CORES[e["tipo"]]
            cel = [Paragraph(f'<font color="{corx}">{_p(e.get("rotulo") or ROTULOS[e["tipo"]]).upper()}</font>', st["rot"]),
                   Paragraph(_p(e.get("titulo")), st["tit"])]
            if e.get("resumo"):
                cel += [Spacer(1, 1.5 * mm), Paragraph(_p(e["resumo"]).replace("\n", "<br/>"), st["txt"])]
            if e.get("ementa"):
                cel += [Spacer(1, 1 * mm), Paragraph(_p(e["ementa"]), st["meta"])]
            extra = []
            if e.get("solicitante"):
                extra.append(f"Pedido por {_p(e['solicitante'])}")
            if e.get("responsavel"):
                extra.append(f"Responsável: {_p(e['responsavel'])}")
            if e.get("anexos"):
                extra.append("Anexos: " + ", ".join(_p(a) for a in e["anexos"]))
            if extra:
                cel += [Spacer(1, 1 * mm), Paragraph("  ·  ".join(extra), st["meta"])]
            linhas.append([Paragraph(_data_br(e["data"]), st["data"]), cel])
            estilo.append(("LINEBEFORE", (1, j), (1, j), 2.5, colors.HexColor(corx)))
        tb = Table(linhas, colWidths=[18 * mm, larg - 18 * mm])
        tb.setStyle(TableStyle(estilo))
        s += [KeepTogether([cab, Spacer(1, 1 * mm), tb]) if len(itens) <= 3 else cab]
        if len(itens) > 3:
            s += [Spacer(1, 1 * mm), tb]
        s.append(Spacer(1, 7 * mm))

    doc.build(s, canvasmaker=_Numerado)
    return buf.getvalue()
