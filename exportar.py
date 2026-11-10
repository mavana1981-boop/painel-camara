"""Exportação da pesquisa de proposições (aba Proposições) em Excel ou CSV."""
import csv
import io
import re
import unicodedata
from datetime import date

import paineis

COLUNAS = [
    ("Proposição", "identificacao", 16),
    ("Tipo", "tipo_documento", 22),
    ("Apresentação", "data_apresentacao", 13),
    ("Ementa", "ementa", 70),
    ("Papel do deputado", "papel", 16),
    ("Autoria", "autoria", 40),
    ("Regime de tramitação", "regime", 26),
    ("Situação atual", "situacao", 40),
    ("Data da situação", "data_situacao", 13),
    ("Dias parado", "dias_parado", 10),
    ("Órgão atual", "orgao", 12),
    ("Relator atual", "relator", 30),
    ("Forma de apreciação", "tipo_conteudo", 30),
    ("Link", "url", 50),
]


def _br(d):
    """2026-03-05 → 05/03/2026 (texto; mantém vazio se faltar)."""
    d = str(d or "")[:10]
    return f"{d[8:10]}/{d[5:7]}/{d[:4]}" if re.match(r"\d{4}-\d{2}-\d{2}$", d) else d


def _relator(p):
    if not p.get("relator_nome"):
        return ""
    sufixo = "-".join(x for x in (p.get("relator_partido"), p.get("relator_uf")) if x)
    return f"{p['relator_nome']} ({sufixo})" if sufixo else p["relator_nome"]


def linhas(itens, extras=()):
    for p in itens:
        v = dict(p)
        v["relator"] = _relator(p)
        v["data_apresentacao"] = _br(p.get("data_apresentacao"))
        v["data_situacao"] = _br(p.get("data_situacao"))
        yield [v.get(campo) if v.get(campo) is not None else "" for _, campo, _ in COLUNAS]
    for r in extras:   # relatorias vistas só nas pautas das comissões
        v = {"identificacao": r.get("identificacao"), "ementa": r.get("ementa"),
             "papel": "relator (pauta de comissão)",
             "situacao": r.get("situacao") or "", "data_situacao": _br(r.get("data_situacao")),
             "orgao": r.get("colegiado"), "url": r.get("url")}
        yield [v.get(campo) if v.get(campo) is not None else "" for _, campo, _ in COLUNAS]


def csv_bytes(itens, extras=()):
    """CSV no jeito que o Excel brasileiro abre direto: UTF-8 com BOM e separador ';'."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    w.writerow([c for c, _, _ in COLUNAS])
    w.writerows(linhas(itens, extras))
    return ("﻿" + buf.getvalue()).encode("utf-8")


def xlsx_bytes(itens, extras=(), descricao=""):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Proposições"
    ws.append([descricao])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append([f"Gerado em {date.today():%d/%m/%Y} · Painel da Câmara · fonte: dados abertos da Câmara"])
    ws["A2"].font = Font(italic=True, color="666666")
    ws.append([])
    ws.append([c for c, _, _ in COLUNAS])
    cab = 4
    for i, (_, _, larg) in enumerate(COLUNAS, 1):
        cel = ws.cell(row=cab, column=i)
        cel.font = Font(bold=True, color="FFFFFF")
        cel.fill = PatternFill("solid", fgColor="1F3A5F")
        cel.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = larg
    i_link = [c for _, c, _ in COLUNAS].index("url") + 1
    i_reg = [c for _, c, _ in COLUNAS].index("regime") + 1
    urg = PatternFill("solid", fgColor="FDE2E1")
    for lin in linhas(itens, extras):
        ws.append(lin)
        r = ws.max_row
        for col in range(1, len(COLUNAS) + 1):
            ws.cell(row=r, column=col).alignment = Alignment(vertical="top", wrap_text=True)
        link = ws.cell(row=r, column=i_link)
        if link.value:
            link.hyperlink = link.value
            link.font = Font(color="1155CC", underline="single")
        if str(ws.cell(row=r, column=i_reg).value or "").lower().startswith("urg"):
            ws.cell(row=r, column=i_reg).fill = urg
    ws.freeze_panes = ws.cell(row=cab + 1, column=2)
    ws.auto_filter.ref = f"A{cab}:{get_column_letter(len(COLUNAS))}{ws.max_row}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def descricao_filtros(s, f, com_nome=True):
    nome = s.get("nome") or ""
    if s.get("partido"):
        nome += f" ({s['partido']}-{s.get('uf') or ''})"
    partes = [f"Proposições · {nome}" if com_nome else "", paineis.PAPEIS.get(f.get("papel", ""), "")]
    andamento = {"tramitando": "Tramitando", "encerrada": "Tramitação encerrada", "norma": "Virou norma"}
    if f.get("regime") in paineis.REGIMES:
        partes.append(f"Regime: {paineis.REGIMES[f['regime']][0]}")
    for chave, rot in (("sigla", "Tipo"), ("sit", "Situação"), ("orgao", "Órgão"), ("busca", "Busca")):
        if f.get(chave):
            partes.append(f"{rot}: {f[chave]}")
    if f.get("situacao") in andamento:
        partes.append(andamento[f["situacao"]])
    if f.get("inicio") or f.get("fim"):
        partes.append(f"Apresentadas {_br(f.get('inicio') and f['inicio'].isoformat()) or '…'} a "
                      f"{_br(f.get('fim') and f['fim'].isoformat()) or '…'}")
    return " · ".join(x for x in partes if x)


def nome_arquivo(nome_deputado, papel):
    base = unicodedata.normalize("NFKD", nome_deputado).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower() or "deputado"
    sufixo = "relatorias" if papel == "relator" else "proposicoes"
    return f"{sufixo}-{base}-{date.today():%Y-%m-%d}"
