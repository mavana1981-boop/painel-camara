"""PDF das matérias que aparecem nos filtros da aba Votações (completo ou sintético), num arquivo só.

Cada matéria entra com as votações do período escolhido (nominais e simbólicas), uma abaixo da outra
(sem página própria, para economizar papel), com marcador no sumário lateral do leitor de PDF.
"""
import time

import metrics
import proposicao_pdf
import sintese_pdf
from db import engine

LIMITE = 60          # matérias por arquivo (o pedido precisa caber no tempo do servidor)
PRAZO_BUSCA = 30     # segundos gastos, no total, buscando nº/autoria dos destaques na Câmara


def _busca_sql(busca, p):
    if not busca:
        return ""
    partes = []
    for i, t in enumerate(busca.replace("/", " ").split()):
        k = f"b{i}"
        p[k] = f"%{t.lower()}%"
        partes.append(f"(LOWER(COALESCE(vt.descricao_materia,'')) LIKE :{k} "
                      f"OR LOWER(COALESCE(vt.descricao_votacao,'')) LIKE :{k} "
                      f"OR LOWER(COALESCE(vt.sigla_materia,'')) LIKE :{k} "
                      f"OR CAST(vt.numero_materia AS VARCHAR(12)) LIKE :{k} "
                      f"OR CAST(vt.ano_materia AS VARCHAR(6)) LIKE :{k})")
    return " AND " + " AND ".join(partes)


def simbolicas(f, busca=""):
    """Votações simbólicas do Plenário que batem com os filtros (mesmos critérios da lista de votações)."""
    base = metrics.Filtros(f.inicio, f.fim, "", "", f.sigla, opostas=False)
    where, p = base.where_votacoes()
    sql = metrics.text(f"""SELECT vt.id, vt.data, vt.sigla_materia, vt.numero_materia, vt.ano_materia,
                                  vt.proposicao_id
                           FROM votacoes_simbolicas vt WHERE {where}{_busca_sql(busca, p)}
                           ORDER BY vt.data DESC LIMIT 2000""")
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql, p).mappings()]


def materias(f, busca=""):
    """[(pid, vid)] das matérias com votação nos filtros, da votação mais recente para a mais antiga."""
    vots = metrics.lista_votacoes(f, busca, limite=2000) + simbolicas(f, busca)
    vots.sort(key=lambda v: str(v.get("data") or ""), reverse=True)
    vistos, saida = set(), []
    for v in vots:
        k = metrics.chave_proposicao(v)
        if k in vistos or k[0] == "v":
            continue
        vistos.add(k)
        saida.append((v.get("proposicao_id"), v["id"]))
    return saida


def gerar(f, busca="", modo="completo"):
    """(pdf, total de matérias encontradas, matérias no arquivo)."""
    lista = materias(f, busca)
    t0, ds = time.monotonic(), []
    for pid, vid in lista[:LIMITE]:
        d = proposicao_pdf.dados(pid, vid, inicio=f.inicio, fim=f.fim, buscar=time.monotonic() - t0 < PRAZO_BUSCA)
        if d and d["votacoes"]:
            ds.append(d)
    if not ds:
        return None, len(lista), 0
    aviso = (f"Este arquivo traz as {LIMITE} matérias com votação mais recente, de {len(lista)} encontradas nos "
             f"filtros. Para imprimir as demais, diminua o período ou escolha um tipo de matéria."
             if len(lista) > LIMITE else None)
    # uma matéria abaixo da outra (sem página própria para cada uma)
    if modo == "sintetico":
        return sintese_pdf.gerar_varios(ds, aviso=aviso), len(lista), len(ds)
    return proposicao_pdf.gerar_varios(ds, sub=_periodo(f), aviso=aviso), len(lista), len(ds)


def _periodo(f):
    br = lambda d: d.strftime("%d/%m/%Y")  # noqa: E731
    if f.inicio and f.fim:
        return f"De {br(f.inicio)} a {br(f.fim)}" if f.inicio != f.fim else br(f.inicio)
    if f.inicio:
        return f"A partir de {br(f.inicio)}"
    if f.fim:
        return f"Até {br(f.fim)}"
    return ""
