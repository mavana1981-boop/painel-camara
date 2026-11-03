"""Agenda do parlamentar em PDF para impressão (Meu Parlamentar).

Período escolhido pelo usuário (hoje, amanhã, semana, data ou intervalo), cabeçalho com foto e nome do
parlamentar, compromissos agrupados por dia. Feito com reportlab; a foto é baixada na hora e recortada em
círculo (sem foto, entram as iniciais).
"""
import html
import io
import logging
from datetime import date, datetime, timedelta

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph, Spacer,
                                Table, TableStyle)

log = logging.getLogger(__name__)

# ---- casa legislativa (o mesmo arquivo serve ao Painel do Senado, mudando só este bloco) ----
CASA = {
    "nome": "Câmara dos Deputados",
    "painel": "Painel da Câmara",
    "cor": "#0b5d3b", "cor_esc": "#073f28", "cor_cl": "#e8f2ec",
    "na_pauta": "Deputado na pauta",
    "escopo_dele": "Órgãos do deputado, Plenário e compromissos da equipe",
    "escopo_todos": "Toda a agenda da Câmara e compromissos da equipe",
}
OURO = colors.HexColor("#c9a24a")
TEXTO = colors.HexColor("#1f2933")
SUAVE = colors.HexColor("#5f6b76")
LINHA = colors.HexColor("#dfe5e1")
MAX_DIAS = 92

DIAS_SEM = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro"]

PERIODOS = [("hoje", "Hoje"), ("amanha", "Amanhã"), ("semana", "Esta semana (seg. a dom.)"),
            ("proxima_semana", "Próxima semana"), ("7dias", "Próximos 7 dias"), ("mes", "Este mês"),
            ("data", "Uma data"), ("intervalo", "Período (de… até…)")]


# ---------------- período ----------------

def _d(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _extenso(d, ano=True):
    s = f"{d.day} de {MESES[d.month - 1]}"
    return f"{s} de {d.year}" if ano else s


def _intervalo_txt(a, b):
    if a == b:
        return f"{DIAS_SEM[a.weekday()].capitalize()}, {_extenso(a)}"
    if a.year == b.year and a.month == b.month:
        return f"{a.day} a {_extenso(b)}"
    if a.year == b.year:
        return f"{_extenso(a, False)} a {_extenso(b)}"
    return f"{_extenso(a)} a {_extenso(b)}"


def periodo(args, hoje):
    """(início, fim, rótulo) a partir dos parâmetros do formulário. ValueError se a escolha for inválida."""
    p = args.get("periodo") or "hoje"
    if p == "hoje":
        a = b = hoje
        rot = "Hoje"
    elif p == "amanha":
        a = b = hoje + timedelta(days=1)
        rot = "Amanhã"
    elif p in ("semana", "proxima_semana"):
        a = hoje - timedelta(days=hoje.weekday()) + timedelta(days=7 if p == "proxima_semana" else 0)
        b = a + timedelta(days=6)
        rot = "Semana" if p == "semana" else "Próxima semana"
    elif p == "7dias":
        a, b = hoje, hoje + timedelta(days=6)
        rot = "Próximos 7 dias"
    elif p == "mes":
        a = hoje.replace(day=1)
        b = (a + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        rot = MESES[a.month - 1].capitalize() + f" de {a.year}"
        return a, b, rot
    elif p == "data":
        a = b = _d(args.get("de"))
        if not a:
            raise ValueError("Escolha a data a imprimir.")
        rot = "Agenda do dia"
    elif p == "intervalo":
        a, b = _d(args.get("de")), _d(args.get("ate"))
        if not a or not b:
            raise ValueError("Informe o início e o fim do período.")
        if b < a:
            a, b = b, a
        if (b - a).days + 1 > MAX_DIAS:
            raise ValueError(f"Escolha um período de até {MAX_DIAS} dias.")
        rot = "Período"
    else:
        raise ValueError("Período inválido.")
    return a, b, f"{rot} · {_intervalo_txt(a, b)}"


# ---------------- foto ----------------

def _iniciais(nome):
    partes = [p for p in (nome or "").replace(".", " ").split() if p[:1].isalpha() and len(p) > 2]
    return ((partes[0][0] + (partes[-1][0] if len(partes) > 1 else "")) if partes else "?").upper()


def foto_redonda(url, lado=360):
    """PNG com a foto recortada em círculo (fundo transparente) ou None se não der para baixar."""
    if not url:
        return None
    try:
        from PIL import Image, ImageDraw, ImageOps
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (painel)"})
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        w, h = im.size                       # fotos oficiais são retrato: centraliza no rosto (terço superior)
        s = min(w, h)
        topo = 0 if h <= w else int(min(h - s, h * 0.06))
        im = im.crop(((w - s) // 2, topo, (w - s) // 2 + s, topo + s)).resize((lado, lado), Image.LANCZOS)
        mask = Image.new("L", (lado * 4, lado * 4), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, lado * 4 - 1, lado * 4 - 1), fill=255)
        im.putalpha(mask.resize((lado, lado), Image.LANCZOS))
        out = io.BytesIO()
        im.save(out, "PNG")
        out.seek(0)
        return out
    except Exception:
        log.warning("agenda PDF: não consegui baixar a foto %s", url, exc_info=True)
        return None


# ---------------- PDF ----------------

def _p(t):
    return html.escape(str(t or ""), quote=False)


class _Numerado(rl_canvas.Canvas):
    """Canvas que conhece o total de páginas (para o 'Página x de y')."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._paginas = []

    def showPage(self):
        self._paginas.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._paginas)
        for est in self._paginas:
            self.__dict__.update(est)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(SUAVE)
            self.drawRightString(A4[0] - 16 * mm, 10 * mm, f"Página {self._pageNumber} de {total}")
            super().showPage()
        super().save()


def gerar(parl, dias, inicio, fim, rotulo, so_dele=True, com_descricao=True, agora=None):
    """parl: dict com nome, partido, uf, foto. dias: [{"data": "AAAA-MM-DD", "itens": [...]}]. Devolve bytes."""
    agora = agora or datetime.now()
    cor, cor_esc, cor_cl = (colors.HexColor(CASA[k]) for k in ("cor", "cor_esc", "cor_cl"))
    nome = parl.get("nome") or ""
    sub = " · ".join(x for x in (parl.get("partido"), parl.get("uf"), CASA["nome"]) if x)
    foto = foto_redonda(parl.get("foto"))
    n_itens = sum(len(d["itens"]) for d in dias)

    st = {
        "dia": ParagraphStyle("dia", fontName="Helvetica-Bold", fontSize=12.5, leading=15, textColor=cor_esc),
        "diasub": ParagraphStyle("diasub", fontName="Helvetica", fontSize=9, leading=12, textColor=SUAVE),
        "hora": ParagraphStyle("hora", fontName="Helvetica-Bold", fontSize=11, leading=13, textColor=cor),
        "tit": ParagraphStyle("tit", fontName="Helvetica-Bold", fontSize=10.5, leading=13.5, textColor=TEXTO),
        "meta": ParagraphStyle("meta", fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=SUAVE),
        "desc": ParagraphStyle("desc", fontName="Helvetica", fontSize=8.3, leading=11, textColor=colors.HexColor("#46525c")),
        "vazio": ParagraphStyle("vazio", fontName="Helvetica-Oblique", fontSize=9, leading=12, textColor=SUAVE),
        "resumo": ParagraphStyle("resumo", fontName="Helvetica", fontSize=9, leading=12, textColor=SUAVE),
    }

    def cabecalho(c, doc):
        larg, alt = A4
        c.saveState()
        if doc.page == 1:
            faixa = 52 * mm
            c.setFillColor(cor_esc)
            c.rect(0, alt - faixa, larg, faixa, stroke=0, fill=1)
            c.setFillColor(cor)                                   # detalhe diagonal discreto
            p = c.beginPath()
            p.moveTo(larg * 0.62, alt)
            p.lineTo(larg, alt)
            p.lineTo(larg, alt - faixa)
            p.lineTo(larg * 0.48, alt - faixa)
            p.close()
            c.drawPath(p, stroke=0, fill=1)
            c.setFillColor(OURO)
            c.rect(0, alt - faixa - 1.4 * mm, larg, 1.4 * mm, stroke=0, fill=1)
            # foto
            r, cx, cy = 15 * mm, 16 * mm + 15 * mm, alt - faixa / 2
            c.setFillColor(colors.white)
            c.circle(cx, cy, r + 1.6 * mm, stroke=0, fill=1)
            c.setStrokeColor(OURO)
            c.setLineWidth(1.4)
            c.circle(cx, cy, r + 1.6 * mm, stroke=1, fill=0)
            if foto:
                foto.seek(0)
                c.drawImage(ImageReader(foto), cx - r, cy - r, 2 * r, 2 * r, mask="auto")
            else:
                c.setFillColor(cor_cl)
                c.circle(cx, cy, r, stroke=0, fill=1)
                c.setFillColor(cor_esc)
                c.setFont("Helvetica-Bold", 22)
                c.drawCentredString(cx, cy - 7.5, _iniciais(nome))
            # textos
            x = cx + r + 9 * mm
            c.setFillColor(OURO)
            c.setFont("Helvetica-Bold", 8.5)
            c.drawString(x, alt - 15 * mm, "A G E N D A   P A R L A M E N T A R")
            c.setFillColor(colors.white)
            tam = 24 if len(nome) <= 26 else 19
            c.setFont("Helvetica-Bold", tam)
            c.drawString(x, alt - 26 * mm, nome)
            c.setFont("Helvetica", 10.5)
            c.setFillColor(colors.HexColor("#d9e6df"))
            c.drawString(x, alt - 33 * mm, sub)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x, alt - 42 * mm, rotulo)
        else:
            c.setFillColor(cor_esc)
            c.rect(0, alt - 13 * mm, larg, 13 * mm, stroke=0, fill=1)
            c.setFillColor(OURO)
            c.rect(0, alt - 13.8 * mm, larg, 0.8 * mm, stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(16 * mm, alt - 8.3 * mm, nome)
            c.setFont("Helvetica", 9)
            c.drawRightString(larg - 16 * mm, alt - 8.3 * mm, rotulo)
        # rodapé
        c.setStrokeColor(LINHA)
        c.setLineWidth(0.5)
        c.line(16 * mm, 14 * mm, larg - 16 * mm, 14 * mm)
        c.setFont("Helvetica", 7.5)
        c.setFillColor(SUAVE)
        c.drawString(16 * mm, 10 * mm, f"Meu Parlamentar · {CASA['painel']} · gerado em "
                                       f"{agora:%d/%m/%Y} às {agora:%H:%M}")
        c.restoreState()

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, title=f"Agenda – {nome} – {rotulo}", author=CASA["painel"],
                          leftMargin=16 * mm, rightMargin=16 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    larg_util = A4[0] - 32 * mm
    doc.addPageTemplates([
        PageTemplate("primeira", [Frame(16 * mm, 20 * mm, larg_util, A4[1] - 20 * mm - 62 * mm, id="f1",
                                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)],
                     onPage=cabecalho, autoNextPageTemplate="demais"),
        PageTemplate("demais", [Frame(16 * mm, 20 * mm, larg_util, A4[1] - 20 * mm - 21 * mm, id="f2",
                                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)],
                     onPage=cabecalho),
    ])

    s = []
    escopo = CASA["escopo_dele"] if so_dele else CASA["escopo_todos"]
    n_dias = len([d for d in dias if d["itens"]])
    s.append(Paragraph(f"<b>{n_itens}</b> compromisso{'s' if n_itens != 1 else ''}"
                       f"{f' em <b>{n_dias}</b> dia' + ('s' if n_dias != 1 else '') if (fim - inicio).days else ''}"
                       f" · {_p(escopo)}", st["resumo"]))
    s.append(Spacer(1, 5 * mm))

    por_dia = {d["data"]: d["itens"] for d in dias}
    total_dias = (fim - inicio).days + 1
    mostrar_vazios = total_dias <= 14            # na semana, o dia sem nada também aparece
    hoje_txt = agora.date().isoformat()
    for i in range(total_dias):
        dia = inicio + timedelta(days=i)
        itens = por_dia.get(dia.isoformat(), [])
        if not itens and not mostrar_vazios:
            continue
        marca = "  ·  hoje" if dia.isoformat() == hoje_txt else ""
        cab = Table([[Paragraph(f"{DIAS_SEM[dia.weekday()].capitalize()}", st["dia"]),
                      Paragraph(f"{_extenso(dia)}{marca}", st["diasub"])]],
                    colWidths=[48 * mm, larg_util - 48 * mm])
        cab.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LINEBELOW", (0, 0), (-1, 0), 1.2, cor),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 0),
        ]))
        if not itens:
            s.append(KeepTogether([cab, Spacer(1, 2.5 * mm), Paragraph("Sem compromissos.", st["vazio"]),
                                   Spacer(1, 6 * mm)]))
            continue
        linhas, estilo = [], []
        for e in itens:
            tags = []
            if e.get("origem") == "equipe":
                tags.append('<font color="#8a6d22"><b>EQUIPE</b></font>')
            elif e.get("colegiado") == "PLEN":
                tags.append(f'<font color="{CASA["cor"]}"><b>PLENÁRIO</b></font>')
            elif e.get("colegiado"):
                tags.append(f'<b>{_p(e["colegiado"])}</b>')
            if e.get("citado"):
                tags.append(f'<font color="#9b2c2c"><b>{CASA["na_pauta"].upper()}</b></font>')
            for k in ("local", "situacao"):
                if e.get(k):
                    tags.append(_p(e[k]))
            cel = [Paragraph(_p(e.get("titulo")), st["tit"])]
            if tags:
                cel.append(Paragraph("  ·  ".join(tags), st["meta"]))
            if com_descricao and e.get("descricao"):
                d = " ".join(str(e["descricao"]).split())
                d = d if len(d) <= 900 else d[:900].rsplit(" ", 1)[0] + "…"
                cel.append(Spacer(1, 1.2 * mm))
                cel.append(Paragraph(_p(d), st["desc"]))
            linhas.append([Paragraph(_p((e.get("hora") or "")[:5]) or "—", st["hora"]), cel])
        t = Table(linhas, colWidths=[20 * mm, larg_util - 20 * mm])
        estilo = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, -1), 0), ("LEFTPADDING", (1, 0), (1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINHA),
        ]
        for j, e in enumerate(itens):
            if e.get("origem") == "equipe":
                estilo.append(("LINEBEFORE", (1, j), (1, j), 2, OURO))
                estilo.append(("LEFTPADDING", (1, j), (1, j), 6))
        t.setStyle(TableStyle(estilo))
        s.append(KeepTogether([cab, Spacer(1, 1 * mm), t]) if len(itens) <= 4 else cab)
        if len(itens) > 4:
            s.append(Spacer(1, 1 * mm))
            s.append(t)
        s.append(Spacer(1, 6 * mm))

    if not n_itens and not mostrar_vazios:
        s.append(Paragraph("Nenhum compromisso no período escolhido.", st["vazio"]))

    doc.build(s, canvasmaker=_Numerado)
    return buf.getvalue()
