"""Alertas da agenda do parlamentar.

1. Deputado na pauta: diz QUAL proposição — ele é relator do item (relator informado na pauta) ou autor/coautor
   da proposição (tabela autorias), ou o nome dele aparece no texto do evento.
2. Termos sensíveis para a Oposição (gênero, misoginia, aborto...): procurados na ementa de cada item da pauta
   (e da proposição relacionada, quando o item é um requerimento) e no título/descrição do evento.
   A lista é da equipe toda e pode ser editada na própria agenda; começa com TERMOS_PADRAO.

A comparação ignora acentos e maiúsculas e casa pelo começo da palavra: "misogin" pega misoginia e misógino;
"genero" pega gênero e gêneros.
"""
import json
import re
from datetime import datetime

from sqlalchemy import delete, insert, select

from db import autorias, engine, norm_nome, termos_alerta

TERMOS_PADRAO = [
    "gênero", "identidade de gênero", "ideologia de gênero", "linguagem neutra", "misoginia", "feminicídio",
    "aborto", "interrupção da gravidez", "interrupção voluntária", "LGBT", "homofobia", "transfobia",
    "homoafetivo", "pessoas trans", "educação sexual", "drogas", "descriminalização", "maconha", "cannabis",
    "desarmamento", "armas de fogo", "fake news", "desinformação", "discurso de ódio", "regulação das redes",
    "plataformas digitais", "censura", "anistia", "8 de janeiro", "racismo estrutural", "cotas raciais",
    "reforma agrária", "invasão de terras", "MST", "liberdade religiosa", "escola sem partido", "homeschooling",
    "eutanásia", "imposto sobre grandes fortunas", "taxação",
]


# ---------------- lista de termos ----------------

def _semear():
    with engine.begin() as c:
        if c.execute(select(termos_alerta.c.id).limit(1)).first() is None:
            agora = datetime.now()
            for t in TERMOS_PADRAO:
                c.execute(insert(termos_alerta).values(termo=t, criado_por="padrão", criado_em=agora))


def termos():
    _semear()
    with engine.connect() as c:
        lista = [r[0] for r in c.execute(select(termos_alerta.c.termo))]
    return sorted(lista, key=norm_nome)


def adicionar(termo, autor):
    termo = re.sub(r"\s+", " ", (termo or "").strip())[:80]
    if len(norm_nome(termo)) < 3:
        raise ValueError("Informe um termo com pelo menos 3 letras.")
    _semear()
    if norm_nome(termo) in {norm_nome(t) for t in termos()}:
        raise ValueError(f"“{termo}” já está na lista.")
    with engine.begin() as c:
        c.execute(insert(termos_alerta).values(termo=termo, criado_por=autor, criado_em=datetime.now()))


def remover(termo):
    with engine.begin() as c:
        c.execute(delete(termos_alerta).where(termos_alerta.c.termo == termo))


def compilar(lista=None):
    """[(termo, regex)] — casa pelo começo da palavra, sem acento."""
    out = []
    for t in lista if lista is not None else termos():
        n = norm_nome(t)
        if n:
            out.append((t, re.compile(r"(?<![a-z0-9])" + re.escape(n).replace(r"\ ", r"\s+"))))
    return out


def achar(texto, regras):
    """Termos (na grafia da lista) encontrados no texto."""
    n = norm_nome(texto or "")
    return [t for t, rx in regras if rx.search(n)] if n else []


# ---------------- análise de um evento ----------------

def autorias_do(cod):
    with engine.connect() as c:
        return {r[0] for r in c.execute(select(autorias.c.processo_id).where(autorias.c.codigo_deputado == cod))}


def itens(e):
    try:
        v = json.loads(e.get("itens_pauta") or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def analisar(e, cod, nome_norm, autorias_cod, regras):
    """Acrescenta ao evento `na_pauta` ([{ident, papel, ementa}]) e `alertas` ([{termos, ident, ementa}])."""
    na_pauta, alertas = [], []
    its = itens(e)
    for it in its:
        papeis = []
        if it.get("relator_id") == cod:
            papeis.append("relator")
        if it.get("id") in autorias_cod:
            papeis.append("autor")
        if it.get("rel_id") and it["rel_id"] in autorias_cod and "autor" not in papeis:
            papeis.append(f"autor da {it.get('rel_ident') or 'proposição relacionada'}")
        if papeis:
            na_pauta.append({"ident": it.get("ident"), "papel": " e ".join(papeis), "ementa": it.get("ementa"),
                             "situacao": it.get("situacao")})
        proprio = achar(" ".join(x for x in (it.get("ident"), it.get("ementa")) if x), regras)
        relac = [t for t in achar(it.get("rel_ementa"), regras) if t not in proprio]
        if proprio:
            alertas.append({"termos": proprio, "ident": it.get("ident"), "ementa": it.get("ementa")})
        if relac:   # ex.: requerimento de urgência para um projeto polêmico
            alertas.append({"termos": relac, "ementa": it.get("rel_ementa"),
                            "ident": f"{it.get('ident')} (sobre o {it.get('rel_ident') or 'projeto relacionado'})"})
    if not its:   # evento sem pauta estruturada (ou gravado antes desta versão): olha o texto
        if f",{cod}," in (e.get("relatores") or ""):
            na_pauta.append({"ident": None, "papel": "relator", "ementa": None})
    if not na_pauta and nome_norm and f" {nome_norm} " in f" {norm_nome(e.get('descricao') or '')} ":
        na_pauta.append({"ident": None, "papel": "citado no evento", "ementa": None})
    if e.get("origem") != "equipe":
        ja = {t for a in alertas for t in a["termos"]}
        resto = [t for t in achar(f"{e.get('titulo') or ''} {'' if its else (e.get('descricao') or '')}", regras)
                 if t not in ja]
        if resto:
            alertas.append({"termos": resto, "ident": None, "ementa": None})
    e["na_pauta"], e["alertas"] = na_pauta, alertas
    e["citado"] = bool(na_pauta)
    return e
