"""Relatório SINTÉTICO de uma proposição: uma página com o resumo das votações no Plenário.

Usa os mesmos dados do relatório completo (proposicao_pdf.dados): ficha curta (autoria, relator, objeto),
o resultado do texto-base com as orientações, um quadro com todas as votações nominais (rótulos curtos,
resultado e placar), como votaram PL, Novo e PT no texto-base e o saldo dos destaques.
"""
import io
import re
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from agenda_pdf import _p
from db import sem_acento

AZUL = colors.HexColor("#1b3350")
CINZA = colors.HexColor("#5f6b76")
ZEBRA = colors.HexColor("#eef3f8")
MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
         "outubro", "novembro", "dezembro")
EXTENSO = {1: "uma", 2: "duas", 3: "três", 4: "quatro", 5: "cinco", 6: "seis", 7: "sete", 8: "oito",
           9: "nove", 10: "dez"}
EXTENSO_M = {**EXTENSO, 1: "um", 2: "dois"}
BANCADAS = ("PL", "NOVO", "PT")
LIDERANCAS = ("Governo", "Oposição", "Minoria", "PL")


def _quando(d):
    if isinstance(d, datetime):
        return d
    try:
        return datetime.fromisoformat(str(d)[:19]) if d else None
    except ValueError:
        return None


def _plano(s):
    return " ".join(sem_acento(s or "").lower().split())


# ---------------- textos ----------------

def datas_extenso(datas):
    """[23/11, 24/11/2026] -> '23 E 24 DE NOVEMBRO DE 2026' (maiúsculas, como no cabeçalho)."""
    dias = sorted({d.date() for d in datas if d})
    if not dias:
        return ""
    if len(dias) > 4:
        a, b = dias[0], dias[-1]
        return f"de {a.day:02d}/{a.month:02d}/{a.year} a {b.day:02d}/{b.month:02d}/{b.year}".upper()
    grupos = []          # [(ano, mes, [dias])]
    for d in dias:
        if grupos and grupos[-1][:2] == (d.year, d.month):
            grupos[-1][2].append(d.day)
        else:
            grupos.append((d.year, d.month, [d.day]))

    def junta(xs):
        xs = [str(x) for x in xs]
        return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " e " + xs[-1]
    partes = []
    for i, (ano, mes, ds) in enumerate(grupos):
        fim_do_ano = i == len(grupos) - 1 or grupos[i + 1][0] != ano
        partes.append(f"{junta(ds)} de {MESES[mes - 1]}" + (f" de {ano}" if fim_do_ano else ""))
    return junta(partes).upper()


PARTIDOS = {"AVANTE": "Avante", "UNIÃO": "União", "UNIAO": "União", "NOVO": "Novo", "REPUBLICANOS": "Republicanos",
            "PODE": "Podemos", "PODEMOS": "Podemos", "CIDADANIA": "Cidadania", "SOLIDARIEDADE": "Solidariedade",
            "REDE": "Rede", "MISSÃO": "Missão"}


def partido(sig):
    return PARTIDOS.get((sig or "").upper(), sig or "")


def _sigla_autor(a):
    a = re.sub(r"^Federação\s+", "", a or "").strip()
    return partido(a)


def _curto_alvo(alvo):
    """'Emenda de Plenário nº 23' -> 'Emenda 23'; 'Art. 6º-F do projeto...' -> 'Art. 6º-F'; texto longo -> None."""
    a = (alvo or "").strip()
    m = re.match(r"(?:emenda|subemenda)(?:\s+de\s+plen[áa]rio|\s+aglutinativa|\s+de\s+comiss[ãa]o)?\s*n?[º°o.]*\s*(\d+)",
                 a, re.I)
    if m:
        return f"{'Subemenda' if a.lower().startswith('sub') else 'Emenda'} {m.group(1)}"
    m = re.match(r"(art\.?\s*\d+[º°o]?(?:-[A-Z])?)", a, re.I)
    if m:
        art = re.sub(r"^art\.?\s*", "Art. ", m.group(1), flags=re.I)
        return re.sub(r"-([a-z])$", lambda x: "-" + x.group(1).upper(), art)
    return None


def eh_destaque(v):
    return bool(v.get("dtq_numero") or v.get("dtq_autoria")) or v.get("tipo_etapa") == "Destaque"


def rotulo_curto(v):
    tipo = v.get("tipo_etapa") or ""
    rot = v.get("rotulo") or tipo or "Votação"
    if eh_destaque(v):
        partes = [f"Destaque {v['dtq_numero']}" if v.get("dtq_numero") else None,
                  _sigla_autor(v.get("dtq_autoria")) or None, _curto_alvo(v.get("dtq_objeto"))]
        partes = [p for p in partes if p]
        if not v.get("dtq_numero"):
            dt = _quando(v.get("data"))
            partes.insert(0, f"Destaque das {dt:%Hh%M}" if dt else "Destaque")
        return " · ".join(partes)
    if tipo == "Urgência":
        return "Urgência"
    if tipo == "Texto principal":
        return "Texto-base"
    m = re.match(r"requerimento\s+de\s+(.+)$", rot, re.I)
    if m:
        r = m.group(1)
        return r[:1].upper() + r[1:]
    if rot.lower().startswith("requerimento n") and tipo and tipo != "Requerimento":
        return tipo
    if rot.lower().startswith("requerimento n"):
        return re.sub(r"\s*\(.*\)$", "", rot)
    return rot


_FEM = {"urgencia", "retirada", "preferencia", "votacao", "emenda", "emendas", "redacao", "proposta",
        "subemenda", "subemendas", "materia", "admissibilidade", "quebra", "inversao", "dispensa", "prorrogacao"}


def resultado_curto(v):
    descr = _plano(v.get("descricao_votacao"))
    aprov = v.get("aprovacao")
    aprov = None if aprov is None else bool(aprov)
    if eh_destaque(v):
        if "mantid" in descr:
            return "Texto mantido"
        if re.search(r"(suprimid|retirad)[oa]s?\b", descr):
            return "Texto suprimido"
        if "emenda" in descr or "emenda" in _plano(v.get("dtq_objeto")):
            if "rejeitad" in descr or aprov is False:
                return "Emenda rejeitada"
            if "aprovad" in descr or aprov is True:
                return "Emenda aprovada"
        return "Aprovado" if aprov else "Rejeitado" if aprov is False else "—"
    if aprov is None:
        return "—"
    palavra = _plano(rotulo_curto(v)).replace("-", " ").split(" ")[0]
    fem = palavra in _FEM
    plural = palavra.endswith("s") and palavra not in ("pais",)
    base = "Aprovad" if aprov else "Rejeitad"
    return base + ("a" if fem else "o") + ("s" if plural else "")


def _voto(t):
    t = (t or "").upper()
    return {"SIM": "Sim", "NÃO": "Não", "NAO": "Não", "OBSTRUÇÃO": "obstrução", "LIBERADO": "liberou", "LIVRE": "liberou",
            "ABSTENÇÃO": "abstenção"}.get(t, t.capitalize())


def texto_base(vots):
    """A votação do texto principal (a primeira), ou None."""
    for x in vots:
        if x["votacao"].get("tipo_etapa") == "Texto principal":
            return x
    for x in vots:
        if x["votacao"].get("tipo_etapa") in ("1º turno", "Substitutivo", "Parecer"):
            return x
    return None


def frase_resultado(x):
    v, c = x["votacao"], x["contagem"]
    aprov = v.get("aprovacao")
    verbo = "aprovou" if aprov else "rejeitou" if aprov is not None and not aprov else "votou"
    if x.get("simbolica"):
        txt = f"A Câmara {verbo} o texto-base em <b>votação simbólica</b>"
        if "ressalvad" in _plano(v.get("descricao_votacao")):
            txt += ", ressalvados os destaques"
        return txt + "."
    txt = f"A Câmara {verbo} o texto-base por <b>{c['SIM']} voto{'s' if c['SIM'] != 1 else ''} a {c['NÃO']}</b>"
    ab = c.get("ABSTENÇÃO") or 0
    txt += f", com {ab} abstenç{'ão' if ab == 1 else 'ões'}" if ab else ""
    if "ressalvad" in _plano(v.get("descricao_votacao")):
        txt += ", ressalvados os destaques"
    txt += "."
    oris = {o["lideranca"]: o["voto"] for o in x.get("orientacoes") or []}
    grupos = {}
    for lid in LIDERANCAS:
        if oris.get(lid):
            grupos.setdefault(_voto(oris[lid]), []).append(lid)
    for voto, lids in grupos.items():
        quem = lids[0] if len(lids) == 1 else ", ".join(lids[:-1]) + " e " + lids[-1]
        if voto == "liberou":
            txt += f" {quem} liberou a bancada." if len(lids) == 1 else f" {quem} liberaram as bancadas."
        elif voto == "obstrução":
            txt += f" {quem} {'orientou' if len(lids) == 1 else 'orientaram'} obstrução."
        else:
            txt += f" {quem} {'orientou' if len(lids) == 1 else 'orientaram'} {voto}."
    return txt


def frase_bancadas(x):
    pl = {p["partido"].upper(): p for p in x.get("placar") or [] if p.get("partido")}
    frases = []
    for sig in BANCADAS:
        p = pl.get(sig)
        if not p:
            continue
        nome = partido(sig)
        art = "No"
        s, n = p.get("SIM") or 0, p.get("NÃO") or 0
        if not s and not n:
            continue
        if s and n:
            maior, menor = (("Não", n), ("Sim", s)) if n >= s else (("Sim", s), ("Não", n))
            frases.append(f"{art} {nome}, {maior[1]} votaram {maior[0]} e {menor[1]} votaram {menor[0]}.")
        else:
            lado, qt, outro = ("Sim", s, "Não") if s else ("Não", n, "Sim")
            if qt <= 10:      # bancada pequena e unida
                frases.append(f"O {nome} teve {qt} voto{'s' if qt != 1 else ''} {lado}.")
                continue
            frases.append(f"{art} {nome}, foram {qt} voto{'s' if qt != 1 else ''} {lado} e nenhum voto {outro}.")
    return " ".join(frases)


def _pelo(autor):
    a = autor or ""
    return ("pela " if re.match(r"(Federação|Bancada|Minoria|Oposição|Liderança|Maioria)\b", a)
            else "pelo ") + a


def frase_destaques(vots):
    dts = [x["votacao"] for x in vots if eh_destaque(x["votacao"])]
    if not dts:
        return ""
    nominais = not any(x.get("simbolica") for x in vots if eh_destaque(x["votacao"]))
    modo, modo1 = (" votados nominalmente", " votado nominalmente") if nominais else ("", "")
    por = {}
    for v in dts:
        por.setdefault(resultado_curto(v), []).append(v)
    frases, usados = [], 0
    for res, verbo in (("Emenda rejeitada", "rejeitada"), ("Emenda aprovada", "aprovada")):
        lst = por.pop(res, [])
        if not lst:
            continue
        usados += len(lst)
        autores = sorted({v.get("dtq_autoria") for v in lst if v.get("dtq_autoria")})
        if len(lst) == 1:
            frases.append(f"A emenda destacada {_pelo(autores[0]) if autores else ''}".rstrip() + f" foi {verbo}.")
        elif len(autores) == 1 and all(v.get("dtq_autoria") for v in lst):
            frases.append(f"As {EXTENSO.get(len(lst), len(lst))} emendas destacadas {_pelo(autores[0])} "
                          f"foram {verbo}s.")
        else:
            quem = f" ({', '.join(_sigla_autor(a) for a in autores)})" if autores else ""
            frases.append(f"{len(lst)} emendas destacadas{quem} foram {verbo}s.")
    sup = por.pop("Texto suprimido", [])
    if sup:
        usados += len(sup)
        frases.append(f"Em {EXTENSO_M.get(len(sup), len(sup))} destaque{'s' if len(sup) > 1 else ''}, "
                      f"o texto foi suprimido.")
    mant = por.pop("Texto mantido", [])
    if mant:
        n = len(mant)
        if usados:
            frases.append(f"No outro destaque{modo1}, o texto foi mantido." if n == 1 else
                          f"Nos outros {EXTENSO_M.get(n, n)} destaques{modo}, o texto foi mantido.")
        else:
            frases.append(f"No destaque{modo1}, o texto foi mantido." if n == 1 else
                          f"Nos {EXTENSO_M.get(n, n)} destaques{modo}, o texto foi mantido.")
    resto = sum(len(x) for x in por.values())
    if resto:
        frases.append(f"Outro{'s' if resto > 1 else ''} {resto} destaque{'s' if resto > 1 else ''} "
                      f"teve{'ram' if resto > 1 else ''} outro desfecho (ver relatório completo).")
    return " ".join(frases)


def titulo_quadro(vots):
    s = sum(1 for x in vots if x.get("simbolica"))
    n = len(vots) - s
    partes = []
    if n:
        partes.append(f"{n} votaç{'ão nominal' if n == 1 else 'ões nominais'}")
    if s:
        partes.append(f"{s} {'simbólica' if s == 1 else 'simbólicas'}" if n else
                      f"{s} votaç{'ão simbólica' if s == 1 else 'ões simbólicas'}")
    return " e ".join(partes)


def _autoria(d):
    pr = d.get("proc") or {}
    princ = [a for a in d.get("autores") or [] if a.get("principal")] or (d.get("autores") or [])[:1]
    if princ:
        a = princ[0]
        suf = "-".join(x for x in (partido(a.get("partido")), a.get("uf")) if x)
        txt = f"Dep. {a['nome']}" + (f" ({suf})" if suf else "")
        outros = len(d.get("autores") or []) - 1
        return txt + (f" e mais {outros}" if outros > 0 else "")
    aut = (pr.get("autoria") or "").strip()
    m = re.match(r"(Senado Federal|Poder Executivo|Comissão.+?|Mesa.+?)\s+-\s+(.+)$", aut)
    return f"{m.group(1)} ({m.group(2)})" if m else aut


def _relator(d):
    pr = d.get("proc") or {}
    if not pr.get("relator_nome"):
        return ""
    suf = "-".join(x for x in (partido(pr.get("relator_partido")), pr.get("relator_uf")) if x)
    return f"Dep. {pr['relator_nome']}" + (f" ({suf})" if suf else "")


# ---------------- PDF ----------------

def gerar(d, agora=None):
    agora = agora or datetime.now()
    vots = d["votacoes"]
    ss = dict(fontName="Helvetica", textColor=colors.HexColor("#1d2329"))
    st = {
        "kicker": ParagraphStyle("k", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=CINZA),
        "titulo": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=30, leading=34, textColor=AZUL,
                                 spaceBefore=6),
        "sub": ParagraphStyle("s", fontName="Helvetica", fontSize=15, leading=19, textColor=AZUL, spaceBefore=2),
        "corpo": ParagraphStyle("c", fontSize=10.5, leading=14.5, **ss),
        "sec": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=AZUL,
                              spaceBefore=4, spaceAfter=5),
        "cel": ParagraphStyle("cel", fontSize=9.5, leading=12, **ss),
        "celb": ParagraphStyle("celb", fontName="Helvetica-Bold", fontSize=9.5, leading=12,
                               textColor=colors.HexColor("#1d2329")),
        "cab": ParagraphStyle("cab", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=colors.white),
        "fonte": ParagraphStyle("f", fontName="Helvetica", fontSize=7.5, leading=10, textColor=CINZA),
    }
    st["num"] = ParagraphStyle("num", parent=st["cel"], alignment=1)
    st["numb"] = ParagraphStyle("numb", parent=st["celb"], alignment=1)
    st["cabn"] = ParagraphStyle("cabn", parent=st["cab"], alignment=1)

    buf = io.BytesIO()
    larg = A4[0] - 36 * mm
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm,
                            bottomMargin=16 * mm, title=f"Resumo das votações — {d['ident']}")
    datas = [_quando(x["votacao"].get("data")) for x in vots]
    quando = datas_extenso(datas)
    el = [Paragraph("PLENÁRIO DA CÂMARA" + (f" | {quando}" if quando else ""), st["kicker"]),
          Paragraph(_p(d["ident"]), st["titulo"]),
          Paragraph("Resumo das votações", st["sub"]), Spacer(1, 5 * mm)]
    ficha = []
    if _autoria(d):
        ficha.append(f"<b>Autoria</b> {_p(_autoria(d))}")
    if _relator(d):
        ficha.append(f"<b>Relator</b> {_p(_relator(d))}")
    if ficha:
        el += [Paragraph("<br/>".join(ficha), st["corpo"]), Spacer(1, 3.5 * mm)]
    objeto = (d.get("proc") or {}).get("resumo") or d.get("ementa")
    if objeto:
        el += [Paragraph(f"<b>Objeto.</b> {_p(objeto)}", st["corpo"]), Spacer(1, 3.5 * mm)]
    tb = texto_base(vots)
    if tb:
        el += [Paragraph(f"<b>Resultado.</b> {frase_resultado(tb)}", st["corpo"]), Spacer(1, 5 * mm)]

    if vots:
        el.append(Paragraph(titulo_quadro(vots), st["sec"]))
        linhas = [[Paragraph("Votação", st["cab"]), Paragraph("Resultado", st["cab"]),
                   Paragraph("Sim", st["cabn"]), Paragraph("Não", st["cabn"]), Paragraph("Abst.", st["cabn"])]]
        spans = []
        for i, x in enumerate(vots, 1):
            v, c = x["votacao"], x["contagem"]
            b = x is tb
            s, sn = (st["celb"], st["numb"]) if b else (st["cel"], st["num"])
            if x.get("simbolica"):
                spans.append(i)
                linhas.append([Paragraph(_p(rotulo_curto(v)), s), Paragraph(_p(resultado_curto(v)), s),
                               Paragraph("<i>simbólica</i>", sn), "", ""])
                continue
            linhas.append([Paragraph(_p(rotulo_curto(v)), s), Paragraph(_p(resultado_curto(v)), s),
                           Paragraph(str(c["SIM"]), sn), Paragraph(str(c["NÃO"]), sn),
                           Paragraph(str(c.get("ABSTENÇÃO") or 0), sn)])
        w = [0, 40 * mm, 16 * mm, 16 * mm, 16 * mm]
        w[0] = larg - sum(w)
        t = Table(linhas, colWidths=w, repeatRows=1)
        estilo = [("BACKGROUND", (0, 0), (-1, 0), AZUL), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                  ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                  ("LEFTPADDING", (0, 0), (-1, -1), 6), ("LINEBELOW", (0, -1), (-1, -1), 0.6, AZUL)]
        estilo += [("BACKGROUND", (0, i), (-1, i), ZEBRA) for i in range(1, len(linhas)) if i % 2 == 0]
        estilo += [("SPAN", (2, i), (4, i)) for i in spans]
        t.setStyle(TableStyle(estilo))
        el += [t, Spacer(1, 5 * mm)]
    else:
        el.append(Paragraph("O painel não tem votação desta proposição no Plenário (no período).", st["corpo"]))

    if tb and not tb.get("simbolica") and frase_bancadas(tb):
        el += [Paragraph(f"<b>Bancadas no texto-base.</b> {frase_bancadas(tb)}", st["corpo"]), Spacer(1, 3 * mm)]
    saldo = frase_destaques(vots)
    if saldo:
        el += [Paragraph(f"<b>Saldo dos destaques.</b> {saldo}", st["corpo"]), Spacer(1, 3 * mm)]
    el += [Spacer(1, 2 * mm),
           Paragraph(f"Fonte: relatório da proposição, Painel da Câmara dos Deputados, {agora:%d/%m/%Y}.",
                     st["fonte"])]
    doc.build(el)
    return buf.getvalue()
