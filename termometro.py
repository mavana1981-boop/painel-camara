"""Termômetro: antecipa como cada deputado deve votar numa proposição.

Como a previsão é feita (para cada deputado em exercício):
1. Votações parecidas — o app procura, entre as votações nominais do Plenário em que a Oposição orientou SIM ou
   NÃO, as de tema parecido com a ementa e as palavras-chave do termômetro (semelhança de palavras, com peso
   maior para as palavras raras). Votações anteriores da MESMA proposição (urgência, requerimentos, destaques)
   valem mais que todas. Em cada uma, conta-se se o deputado votou com a orientação da Oposição.
2. Ponto de partida — quem votou pouco nesses temas é puxado para o seu oposicionismo geral na legislatura e
   para o comportamento do seu partido nas votações parecidas (média ponderada; nada é inventado).
3. Discursos e entrevistas — por botão, a IA lê os discursos do deputado no Plenário (API da Câmara) e as
   notícias com o nome dele e o tema (Google Notícias) e diz se há sinal de voto a favor ou contra.
   O sinal entra na conta com peso proporcional à confiança.
4. Posição apurada — o que a assessoria marcar (ex.: "votará SIM, confirmado") prevalece sobre tudo.

Com a orientação da Oposição (SIM ou NÃO) o alinhamento vira probabilidade de votar SIM, que dá a faixa do
deputado (SIM provável, tende a SIM, indefinido, tende a NÃO, NÃO provável) e o placar previsto.

Também guarda anotações da equipe (do termômetro e de cada deputado) e os contatos das assessorias, com link
direto para o WhatsApp (wa.me) e mensagem pronta.
"""
import json
import logging
import math
import os
import re
import threading
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import quote

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import bindparam, delete, func, insert, select, text, update

import metrics
from db import (contatos_assessoria, deputados, engine, processos, sem_acento, termometro_ia, termometro_notas,
                termometro_posicoes, termometros)

log = logging.getLogger("termometro")
bp = Blueprint("termometro", __name__, url_prefix="/termometro")

MAX_SIMILARES = int(os.environ.get("TERMOMETRO_SIMILARES", "40"))
SIM_MINIMA = float(os.environ.get("TERMOMETRO_SIM_MINIMA", "0.15"))
PESO_MESMA = 1.5           # votação anterior da própria proposição
PESO_PRIOR = 2.0           # quantas "votações" vale o ponto de partida (geral + partido)
LOTE_IA = int(os.environ.get("TERMOMETRO_IA_LOTE", "15"))      # deputados por rodada de análise em lote
INICIO_DISCURSOS = os.environ.get("INICIO_LEGISLATURA", "2023-02-01")
API = os.environ.get("CAMARA_API", "https://dadosabertos.camara.leg.br/api/v2")

FAIXAS = [("sim", "SIM provável", 0.70), ("tende_sim", "Tende a SIM", 0.57), ("indef", "Indefinido", 0.43),
          ("tende_nao", "Tende a NÃO", 0.30), ("nao", "NÃO provável", -1)]
ROTULO_FAIXA = {k: r for k, r, _ in FAIXAS}
POSICOES = [("", "Sem apuração"), ("sim", "Votará SIM (confirmado)"), ("tende_sim", "Tende a SIM"),
            ("indeciso", "Indeciso"), ("tende_nao", "Tende a NÃO"), ("nao", "Votará NÃO (confirmado)"),
            ("ausente", "Ausente / não deve votar")]
ROTULO_POSICAO = dict(POSICOES)
P_MANUAL = {"sim": 0.97, "tende_sim": 0.72, "indeciso": 0.5, "tende_nao": 0.28, "nao": 0.03}
ROTULO_IA = {"favor": "A favor", "contra": "Contra", "indefinido": "Sem posição clara",
             "sem_sinal": "Nada encontrado"}

# ---------------- palavras ----------------

_STOP = set(sem_acento("""
a o e é de da do das dos em no na nos nas um uma uns umas para por pelo pela pelos pelas com sem sob sobre
que se ao aos à às ou como mais menos entre até seu sua seus suas este esta esse essa isso aquele aquela
lei leis decreto artigo art arts inciso incisos paragrafo paragrafos alinea caput dispositivo dispositivos
altera alterar alteracao alteracoes acrescenta acrescentar acresce revoga revogar modifica modificar dispoe
dispor da dar outras outros providencias institui instituir cria criar estabelece estabelecer trata tratar
redacao nova novo texto nacional federal brasil brasileira brasileiro uniao ano anos dia dias data forma
projeto proposta emenda emendas constituicao constitucional complementar ordinaria medida provisoria
requerimento requer urgencia retirada pauta adiamento votacao discussao turno destaque destaques
subemenda substitutivo parecer aprovado aprovada rejeitado rejeitada mantido texto termos sessao
deliberativa plenario camara deputados senado comissao relator autor materia sistema demais quando onde
fins efeito efeitos aplicacao aplica prever preve previsto prevista fica ficam passa passam vigor
""").split())


def _raiz(p):
    """Raiz simples (sem acento, sem plural e terminações comuns): 'tributárias' e 'tributário' → 'tribut'."""
    for suf in ("izacoes", "izacao", "acoes", "icoes", "mentos", "mento", "mente", "acao", "icao", "idade",
                "idades", "arios", "arias", "ario", "aria", "ivos", "ivas", "ivo", "iva", "ais", "eis",
                "oes", "aes", "res", "es", "as", "os", "al", "s"):
        if p.endswith(suf) and len(p) - len(suf) >= 4:
            p = p[: -len(suf)]
            break
    return p[:8]


def palavras(texto):
    """Conjunto de raízes significativas de um texto (sem acento, sem palavras de ligação ou de processo)."""
    s = sem_acento(texto or "").lower()
    return {_raiz(w) for w in re.findall(r"[a-z]{4,}", s) if w not in _STOP}


def termos_busca(t, maximo=4):
    """Palavras para procurar discursos e notícias: as palavras-chave do termômetro ou as mais longas da ementa."""
    if (t.get("palavras") or "").strip():
        return [p.strip() for p in t["palavras"].split(",") if p.strip()][:maximo]
    vistos, out = set(), []
    for w in sorted(re.findall(r"[A-Za-zÀ-ÿ]{5,}", t.get("ementa") or ""), key=len, reverse=True):
        k = sem_acento(w).lower()
        if k in _STOP or _raiz(k) in vistos:
            continue
        vistos.add(_raiz(k))
        out.append(w.lower())
        if len(out) >= maximo:
            break
    return out


_IDENT = re.compile(r"\b([A-Z]{2,5})\s*(?:n[º°o.]*\s*)?([\d.]+)\s*/\s*(\d{4})\b", re.I)


def parse_identificacao(s):
    """'PL 1.087/2025' → ('PL', 1087, 2025)."""
    m = _IDENT.search(s or "")
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2).replace(".", "")), int(m.group(3))


# ---------------- termômetros ----------------

def lista(incluir_arquivados=False):
    q = select(termometros).order_by(termometros.c.arquivado, termometros.c.atualizado_em.desc())
    if not incluir_arquivados:
        q = q.where((termometros.c.arquivado.is_(None)) | (termometros.c.arquivado.is_(False)))
    with engine.connect() as c:
        return [dict(r) for r in c.execute(q).mappings()]


def obter(tid):
    with engine.connect() as c:
        r = c.execute(select(termometros).where(termometros.c.id == tid)).mappings().first()
    return dict(r) if r else None


def _ori(v):
    v = sem_acento(v or "").strip().upper()
    return {"SIM": "SIM", "NAO": "NÃO"}.get(v)


def criar(dados, autor):
    ori = _ori(dados.get("orientacao_oposicao"))
    ident = (dados.get("identificacao") or "").strip()[:80]
    ementa = (dados.get("ementa") or "").strip()
    pid = dados.get("proposicao_id")
    if pid and not (ident and ementa):           # completa pela tabela de proposições
        with engine.connect() as c:
            p = c.execute(select(processos.c.identificacao, processos.c.ementa)
                          .where(processos.c.id == pid)).first()
        if p:
            ident, ementa = ident or p[0], ementa or p[1]
    if not ident and not ementa:
        raise ValueError("Informe a proposição (ex.: PL 1087/2025) ou o tema.")
    if not ori:
        raise ValueError("Informe como a Oposição vai orientar (SIM ou NÃO).")
    agora = datetime.now()
    with engine.begin() as c:
        res = c.execute(insert(termometros).values(
            proposicao_id=int(pid) if pid else None, identificacao=ident or "Tema", ementa=ementa,
            palavras=(dados.get("palavras") or "").strip()[:500], orientacao_oposicao=ori,
            evento_id=dados.get("evento_id"), observacao=(dados.get("observacao") or "").strip() or None,
            arquivado=False, criado_por=autor, criado_em=agora, atualizado_em=agora))
        return res.inserted_primary_key[0]


def editar(tid, dados):
    valores = {"atualizado_em": datetime.now()}
    for k in ("identificacao", "ementa", "palavras", "observacao"):
        if k in dados:
            valores[k] = (dados.get(k) or "").strip() or None
    if "orientacao_oposicao" in dados and _ori(dados["orientacao_oposicao"]):
        valores["orientacao_oposicao"] = _ori(dados["orientacao_oposicao"])
    with engine.begin() as c:
        c.execute(update(termometros).where(termometros.c.id == tid).values(**valores))


def buscar_proposicao(consulta):
    """Proposições que casam com 'PL 1087/2025' ou com palavras da ementa (até 15)."""
    consulta = (consulta or "").strip()
    if not consulta:
        return []
    q = select(processos.c.id, processos.c.identificacao, processos.c.ementa, processos.c.situacao)
    ident = parse_identificacao(consulta)
    if ident:
        q = q.where(processos.c.sigla == ident[0], processos.c.numero == str(ident[1]), processos.c.ano == ident[2])
    else:
        for w in consulta.split()[:5]:
            q = q.where(processos.c.ementa.ilike(f"%{w}%"))
        q = q.order_by(processos.c.data_apresentacao.desc())
    with engine.connect() as c:
        return [dict(r) for r in c.execute(q.limit(15)).mappings()]


def itens_pauta():
    """Itens da última pauta do Plenário (Minha Pauta) para criar o termômetro com um clique."""
    try:
        import plenario
        p = plenario.pauta()
    except Exception:
        log.exception("pauta do Plenário")
        return None, []
    if not p:
        return None, []
    out = []
    for i in p["itens"]:
        ori = _ori(((i.get("orientacoes_grupo") or {}).get("oposicao") or {}).get("orientacao")) \
            or _ori((i.get("nota") or {}).get("orientacao"))
        out.append({"proposicao_id": i.get("id_principal"), "identificacao": i.get("projeto"),
                    "ementa": i.get("ementa"), "orientacao": ori, "evento_id": p.get("evento_id")})
    return p, out


# ---------------- votações parecidas ----------------

def _candidatas():
    sql = text("""
        SELECT vt.id, vt.data, vt.sigla_materia, vt.numero_materia, vt.ano_materia, vt.proposicao_id,
               vt.descricao_materia, vt.descricao_votacao, vt.qtd_sim, vt.qtd_nao, vt.aprovacao,
               o.voto AS ori_opo, gv.voto AS ori_gov
        FROM votacoes vt
        JOIN orientacoes o ON o.votacao_id = vt.id AND o.lideranca = 'Oposição'
        LEFT JOIN orientacoes gv ON gv.votacao_id = vt.id AND gv.lideranca = 'Governo'
        WHERE o.voto IN ('SIM','NÃO') AND (vt.secreta IS NULL OR vt.secreta = :f)""")
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql, {"f": False}).mappings()]


def votacoes_similares(t, candidatas=None):
    """Votações parecidas com o termômetro, com o peso de cada uma (semelhança de 0 a 1; a mesma proposição vale
    PESO_MESMA). Semelhança = cosseno entre as palavras da ementa (com peso pela raridade da palavra)."""
    cand = _candidatas() if candidatas is None else candidatas
    if not cand:
        return []
    ident = parse_identificacao(t.get("identificacao"))
    alvo = defaultdict(float)
    for w in palavras(t.get("ementa")):
        alvo[w] += 1.0
    for w in palavras((t.get("palavras") or "").replace(",", " ")):
        alvo[w] += 2.0                             # palavras-chave escolhidas pela equipe pesam mais
    toks = {}
    df = defaultdict(int)
    for v in cand:
        chave = v["descricao_materia"] or ""
        if chave not in toks:
            toks[chave] = palavras(chave)
            for w in toks[chave]:
                df[w] += 1
    n = max(len(toks), 1)
    idf = lambda w: math.log(1 + n / (1 + df.get(w, 0)))  # noqa: E731
    norma_a = math.sqrt(sum((p * idf(w)) ** 2 for w, p in alvo.items())) or 1.0
    out = []
    for v in cand:
        mesma = bool((t.get("proposicao_id") and v["proposicao_id"] == t["proposicao_id"]) or
                     (ident and (v["sigla_materia"], v["numero_materia"], v["ano_materia"]) == ident))
        b = toks[v["descricao_materia"] or ""]
        if mesma:
            sim = 1.0
        elif not b or not alvo:
            continue
        else:
            inter = sum(alvo[w] * idf(w) ** 2 for w in b if w in alvo)
            sim = inter / (norma_a * (math.sqrt(sum(idf(w) ** 2 for w in b)) or 1.0))
            if sim < SIM_MINIMA:
                continue
        opostas = v["ori_gov"] in ("SIM", "NÃO") and v["ori_gov"] != v["ori_opo"]
        peso = PESO_MESMA if mesma else sim * (1.0 if opostas else 0.6)
        out.append({**v, "sim": round(sim, 3), "peso": round(peso, 3), "mesma": mesma, "opostas": opostas,
                    "comuns": sorted({w for w in b if w in alvo})[:8]})
    mesmas = [x for x in out if x["mesma"]]
    outras = sorted([x for x in out if not x["mesma"]], key=lambda x: (-x["peso"], str(x["data"])))
    return sorted(mesmas, key=lambda x: str(x["data"]), reverse=True) + outras[:MAX_SIMILARES]


# ---------------- previsão ----------------

def faixa(p_sim):
    for k, _, minimo in FAIXAS:
        if p_sim >= minimo:
            return k
    return "nao"


def quorum(t):
    ident = parse_identificacao(t.get("identificacao"))
    sigla = ident[0] if ident else ""
    if sigla == "PEC":
        return {"sim": 308, "rotulo": "PEC: 308 votos SIM (3/5), em dois turnos"}
    if sigla == "PLP":
        return {"sim": 257, "rotulo": "PLP: 257 votos SIM (maioria absoluta)"}
    return {"sim": None, "rotulo": "Maioria simples dos presentes (quórum de 257 deputados)"}


def _por_termometro(tabela, tid):
    with engine.connect() as c:
        return {r["codigo_deputado"]: dict(r) for r in c.execute(
            select(tabela).where(tabela.c.termometro_id == tid)).mappings()}


def _contagem(tabela, filtro=None):
    q = select(tabela.c.codigo_deputado, func.count()).group_by(tabela.c.codigo_deputado)
    if filtro is not None:
        q = q.where(filtro)
    with engine.connect() as c:
        return {r[0]: r[1] for r in c.execute(q)}


def calcular(t, similares=None):
    """Previsão de todos os deputados em exercício. Devolve {deputados, placar, similares, quorum}."""
    sims = votacoes_similares(t) if similares is None else similares
    peso = {s["id"]: s for s in sims}
    with engine.connect() as c:
        cad = {r["codigo"]: dict(r) for r in c.execute(
            select(deputados).where(deputados.c.em_exercicio.is_(True))).mappings()}
        votos = []
        if peso:
            q = text("SELECT votacao_id, codigo_deputado, voto FROM votos "
                     "WHERE votacao_id IN :ids AND voto IN ('SIM','NÃO')").bindparams(
                bindparam("ids", expanding=True))
            votos = c.execute(q, {"ids": list(peso)}).all()
    ind = defaultdict(lambda: {"w": 0.0, "a": 0.0, "n": 0, "mesma": []})
    par = defaultdict(lambda: {"w": 0.0, "a": 0.0})
    for vid, cod, voto in votos:
        s = peso[vid]
        com = 1.0 if voto == s["ori_opo"] else 0.0
        d = ind[cod]
        d["w"] += s["peso"]
        d["a"] += s["peso"] * com
        d["n"] += 1
        if s["mesma"]:
            d["mesma"].append({"id": vid, "data": s["data"], "voto": voto, "com": bool(com)})
        partido = (cad.get(cod) or {}).get("partido")
        if partido:
            par[partido]["w"] += s["peso"]
            par[partido]["a"] += s["peso"] * com
    geral = {r["codigo"]: r for r in metrics.alinhamento("Oposição", metrics.Filtros())}
    ia = _por_termometro(termometro_ia, t["id"]) if t.get("id") else {}
    manual = _por_termometro(termometro_posicoes, t["id"]) if t.get("id") else {}
    notas = _contagem(termometro_notas, termometro_notas.c.termometro_id == t["id"]) if t.get("id") else {}
    contatos = contatos_por_deputado()
    sim_opo = t.get("orientacao_oposicao") == "SIM"
    out = []
    for cod, s in cad.items():
        d = ind.get(cod) or {"w": 0.0, "a": 0.0, "n": 0, "mesma": []}
        g_ = geral.get(cod)
        p_ = par.get(s.get("partido"))
        bases = []
        if g_ and g_["total"] >= 5 and g_["pct"] is not None:
            bases.append(g_["pct"] / 100)
        if p_ and p_["w"] >= 1:
            bases.append(p_["a"] / p_["w"])
        prior = sum(bases) / len(bases) if bases else 0.5
        alin = (d["a"] + PESO_PRIOR * prior) / (d["w"] + PESO_PRIOR)
        p_votos = alin if sim_opo else 1 - alin
        p_sim, fonte = p_votos, "votos"
        r_ia = ia.get(cod)
        if r_ia and r_ia["posicao"] in ("favor", "contra") and r_ia.get("confianca"):
            a = 0.5 * min(r_ia["confianca"], 100) / 100
            p_sim = (1 - a) * p_votos + a * (0.9 if r_ia["posicao"] == "favor" else 0.1)
            fonte = "votos + IA"
        p_sim = min(max(p_sim, 0.03), 0.97)       # cálculo nunca dá certeza: 3% a 97%
        m = (manual.get(cod) or {}).get("posicao")
        if m in P_MANUAL:
            p_sim, fonte = P_MANUAL[m], "apurada"
        if m == "sim" or m == "nao" or m == "ausente":
            confianca = "apurada"
        elif d["w"] >= 4 or (r_ia and (r_ia.get("confianca") or 0) >= 70 and d["w"] >= 1) or m:
            confianca = "alta"
        elif d["w"] >= 1 or (r_ia and r_ia["posicao"] in ("favor", "contra")):
            confianca = "média"
        else:
            confianca = "baixa"
        ct = contatos.get(cod) or []
        out.append({
            "codigo": cod, "nome": s["nome"], "partido": s.get("partido") or "", "uf": s.get("uf") or "",
            "foto": s.get("foto"), "p_sim": round(p_sim, 3), "pct_sim": round(100 * p_sim),
            "faixa": "ausente" if m == "ausente" else faixa(p_sim), "fonte": fonte, "confianca": confianca,
            "n_similares": d["n"], "peso_similares": round(d["w"], 2),
            "com_oposicao_tema": round(100 * d["a"] / d["w"]) if d["w"] else None,
            "oposicionismo": g_["pct"] if g_ else None,
            "partido_tema": round(100 * p_["a"] / p_["w"]) if p_ and p_["w"] else None,
            "mesma": sorted(d["mesma"], key=lambda x: str(x["data"]), reverse=True),
            "ia": r_ia, "manual": m, "notas": notas.get(cod, 0), "contatos": ct,
        })
    out.sort(key=lambda x: (-x["p_sim"], x["nome"]))
    return {"deputados": out, "placar": placar(out, t), "similares": sims, "quorum": quorum(t)}


def placar(deps, t=None):
    c = defaultdict(int)
    for d in deps:
        c[d["faixa"]] += 1
    sim = c["sim"] + c["tende_sim"]
    nao = c["nao"] + c["tende_nao"]
    q = quorum(t or {})
    esperado = round(sum(d["p_sim"] for d in deps if d["faixa"] != "ausente"))
    res = {"sim": sim, "nao": nao, "indef": c["indef"], "ausente": c["ausente"], "faixas": dict(c),
           "esperado_sim": esperado, "total": len(deps)}
    if q["sim"]:
        res["falta_sim"] = max(q["sim"] - sim, 0)
        res["situacao"] = "aprovaria" if sim >= q["sim"] else ("rejeitaria" if sim + c["indef"] < q["sim"]
                                                              else "depende dos indefinidos")
    else:
        res["situacao"] = "aprovaria" if sim > nao + c["indef"] else ("rejeitaria" if nao > sim + c["indef"]
                                                                      else "depende dos indefinidos")
    return res


# ---------------- posição apurada e anotações ----------------

def marcar_posicao(tid, cod, posicao, autor):
    with engine.begin() as c:
        c.execute(delete(termometro_posicoes).where(termometro_posicoes.c.termometro_id == tid,
                                                    termometro_posicoes.c.codigo_deputado == cod))
        if posicao in ROTULO_POSICAO and posicao:
            c.execute(insert(termometro_posicoes).values(termometro_id=tid, codigo_deputado=cod, posicao=posicao,
                                                         autor=autor, atualizado_em=datetime.now()))
        c.execute(update(termometros).where(termometros.c.id == tid).values(atualizado_em=datetime.now()))


def notas(tid, cod=None, geral=False):
    q = select(termometro_notas).where(termometro_notas.c.termometro_id == tid)
    if geral:
        q = q.where(termometro_notas.c.codigo_deputado.is_(None))
    elif cod is not None:
        q = q.where(termometro_notas.c.codigo_deputado == cod)
    with engine.connect() as c:
        return [dict(r) for r in c.execute(q.order_by(termometro_notas.c.criado_em.desc())).mappings()]


def anotar(tid, cod, texto_, autor):
    texto_ = (texto_ or "").strip()
    if not texto_:
        return None
    with engine.begin() as c:
        c.execute(insert(termometro_notas).values(termometro_id=tid, codigo_deputado=cod, texto=texto_[:5000],
                                                  autor=autor, criado_em=datetime.now()))
        c.execute(update(termometros).where(termometros.c.id == tid).values(atualizado_em=datetime.now()))
    return True


def apagar_nota(nid, usuario):
    with engine.begin() as c:
        r = c.execute(select(termometro_notas).where(termometro_notas.c.id == nid)).mappings().first()
        if not r:
            return None
        if r["autor"] != usuario.get("login") and not usuario.get("admin"):
            abort(403)
        c.execute(delete(termometro_notas).where(termometro_notas.c.id == nid))
        return dict(r)


# ---------------- contatos e WhatsApp ----------------

def numero_whatsapp(tel):
    """'(61) 98184-8803' → '5561981848803'. Sem DDI, assume Brasil (55). Inválido → None."""
    d = re.sub(r"\D", "", tel or "")
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("0") and len(d) in (11, 12):      # 0 + DDD (discagem nacional)
        d = d[1:]
    if len(d) in (10, 11):
        d = "55" + d
    return d if 12 <= len(d) <= 13 else None


def link_whatsapp(tel, mensagem=""):
    n = numero_whatsapp(tel)
    if not n:
        return None
    return f"https://wa.me/{n}" + (f"?text={quote(mensagem)}" if mensagem else "")


def mensagem_padrao(t, dep, contato=None, usuario=None):
    quem = (usuario or {}).get("nome") or (usuario or {}).get("login") or ""
    ola = f"Olá, {contato['nome'].split()[0]}!" if contato and contato.get("nome") else "Olá!"
    ementa = (t.get("ementa") or "").strip()
    if len(ementa) > 160:
        ementa = ementa[:157].rsplit(" ", 1)[0] + "..."
    nome_dep = dep.get("nome") if dep else "o(a) deputado(a)"
    return (f"{ola} {'Aqui é ' + quem + ', da' if quem else 'Falo da'} Liderança da Oposição na Câmara. "
            f"Estamos acompanhando a {t.get('identificacao') or 'proposição'}"
            f"{' (' + ementa + ')' if ementa else ''}. "
            f"Gostaria de saber como o(a) Dep. {nome_dep} pretende votar. Podemos conversar?")


def contatos_por_deputado(cod=None):
    q = select(contatos_assessoria).order_by(contatos_assessoria.c.nome)
    if cod is not None:
        q = q.where(contatos_assessoria.c.codigo_deputado == cod)
    out = defaultdict(list)
    with engine.connect() as c:
        for r in c.execute(q).mappings():
            out[r["codigo_deputado"]].append(dict(r))
    return out if cod is None else out.get(cod, [])


def salvar_contato(cod, dados, autor, cid=None):
    valores = {k: ((dados.get(k) or "").strip()[:lim] or None)
               for k, lim in (("nome", 120), ("cargo", 120), ("whatsapp", 30), ("telefone", 30), ("email", 120),
                              ("observacao", 2000))}
    if not (valores["nome"] or valores["whatsapp"] or valores["telefone"]):
        raise ValueError("Informe ao menos o nome e um telefone ou WhatsApp.")
    if valores["whatsapp"] and not numero_whatsapp(valores["whatsapp"]):
        raise ValueError("WhatsApp inválido: use DDD + número, ex.: (61) 99999-9999.")
    agora = datetime.now()
    with engine.begin() as c:
        if cid:
            c.execute(update(contatos_assessoria).where(contatos_assessoria.c.id == cid,
                                                        contatos_assessoria.c.codigo_deputado == cod)
                      .values(**valores, atualizado_em=agora))
        else:
            c.execute(insert(contatos_assessoria).values(codigo_deputado=cod, **valores, criado_por=autor,
                                                         criado_em=agora, atualizado_em=agora))


def apagar_contato(cod, cid):
    with engine.begin() as c:
        c.execute(delete(contatos_assessoria).where(contatos_assessoria.c.id == cid,
                                                    contatos_assessoria.c.codigo_deputado == cod))


def telefone_gabinete(cod):
    try:
        import paineis
        tel = paineis.perfil(cod)["telefones"]
        return tel[0] if tel else None
    except Exception:
        return None


# ---------------- discursos e entrevistas (IA) ----------------

def _http_json(url):
    import requests
    r = requests.get(url, headers={"Accept": "application/json", "User-Agent": "painel-camara-oposicao/1.0"},
                     timeout=25)
    if r.status_code != 200:
        log.warning("termômetro: HTTP %s em %s", r.status_code, url)
        return None
    return r.json()


def _trecho(texto_, alvo, tamanho=1100):
    """Pedaço do texto em volta da primeira palavra do tema."""
    texto_ = re.sub(r"\s+", " ", texto_ or "").strip()
    norm = sem_acento(texto_).lower()
    pos = [m.start() for m in re.finditer(r"[a-z]{4,}", norm) if _raiz(m.group()) in alvo]
    if not pos or len(texto_) <= tamanho:
        return texto_[:tamanho]
    ini = max(0, pos[0] - tamanho // 3)
    return ("..." if ini else "") + texto_[ini:ini + tamanho] + "..."


def discursos(cod, alvo, pedir=None, maximo=5, paginas=3):
    """Discursos do deputado na legislatura que falam do tema (API /deputados/{id}/discursos)."""
    pedir = pedir or _http_json
    url = (f"{API}/deputados/{cod}/discursos?dataInicio={INICIO_DISCURSOS}&dataFim={date.today().isoformat()}"
           "&ordenarPor=dataHoraInicio&ordem=DESC&itens=100")
    achados = []
    for _ in range(paginas):
        try:
            doc = pedir(url)
        except Exception as e:
            log.warning("discursos de %s: %s", cod, e)
            doc = None
        if not doc:
            break
        for d in doc.get("dados") or []:
            corpo = " ".join(x for x in (d.get("sumario"), d.get("keywords"), d.get("transcricao")) if x)
            casou = len(palavras(corpo) & alvo)
            if casou >= (2 if len(alvo) >= 3 else 1):
                fase = (d.get("faseEvento") or {}).get("titulo")
                achados.append({"tipo": "discurso", "data": (d.get("dataHoraInicio") or "")[:10],
                                "titulo": " · ".join(x for x in (d.get("tipoDiscurso"), fase) if x) or "Discurso",
                                "link": d.get("urlTexto") or d.get("urlAudio") or d.get("urlVideo"),
                                "texto": _trecho((d.get("sumario") or "") + " " + (d.get("transcricao") or ""),
                                                 alvo), "casou": casou})
        prox = next((l.get("href") for l in doc.get("links") or [] if l.get("rel") == "next"), None)
        if not prox or prox == url:
            break
        url = prox
    achados.sort(key=lambda x: x["data"], reverse=True)          # mais recentes primeiro...
    achados.sort(key=lambda x: -x["casou"])                        # ...entre os que mais falam do tema
    return achados[:maximo]


def noticias(nome, termos, buscar=None, maximo=8, dias=365):
    """Notícias e entrevistas com o nome do deputado e o tema (Google Notícias)."""
    import radar
    if not termos:
        return []
    consulta = f'"{nome}" (' + " OR ".join(termos) + ")"
    url = ("https://news.google.com/rss/search?q=" + quote(f"{consulta} when:{dias}d")
           + "&hl=pt-BR&gl=BR&ceid=BR:pt-419")
    try:
        conteudo = (buscar or radar.baixar)(url)
    except Exception as e:
        log.warning("notícias de %s: %s", nome, e)
        conteudo = None
    itens = radar.parse_feed(conteudo) if conteudo else []
    return [{"tipo": "notícia", "data": it["publicado"].date().isoformat() if it.get("publicado") else "",
             "titulo": f"{it['titulo']}{' (' + it['fonte'] + ')' if it.get('fonte') else ''}", "link": it["link"],
             "texto": it.get("resumo") or it["titulo"]} for it in itens[:maximo]]


SISTEMA = """Você é analista legislativo da Câmara dos Deputados. Sua tarefa é dizer, com base SOMENTE nas fontes \
fornecidas, se um(a) deputado(a) tende a votar A FAVOR (SIM, pela aprovação) ou CONTRA (NÃO) uma proposição.
Regras:
- Considere apenas opiniões, votos anunciados e declarações do PRÓPRIO deputado sobre o tema da proposição.
- Notícia que só cita o nome do deputado, ou que fala de outro assunto, não indica posição.
- Se as fontes não deixam claro, responda "indefinido". Não deduza pelo partido. Não invente fatos nem citações.
- Os trechos das evidências devem ser copiados das fontes (no máximo 30 palavras cada).
Responda apenas com JSON, sem texto fora dele:
{"posicao": "favor" | "contra" | "indefinido", "confianca": 0-100, "justificativa": "até 50 palavras",
 "evidencias": [{"fonte": número da fonte, "trecho": "..."}]}"""


def montar_pedido(t, dep, fontes):
    linhas = [f"Proposição: {t.get('identificacao') or '—'}", f"Ementa: {t.get('ementa') or '—'}"]
    if t.get("palavras"):
        linhas.append(f"Palavras-chave do tema: {t['palavras']}")
    linhas.append(f"Deputado(a): {dep['nome']} ({dep.get('partido') or '—'}-{dep.get('uf') or '—'})")
    linhas.append("\nFontes:")
    for i, f in enumerate(fontes, 1):
        linhas.append(f"[{i}] {f['tipo']} — {f.get('data') or 's/d'} — {f.get('titulo') or ''}\n{f.get('texto') or ''}")
    linhas.append("\nCom base apenas nessas fontes, qual a tendência de voto do(a) deputado(a) nesta proposição?")
    return "\n".join(linhas)


def analisar_deputado(tid, cod, autor=None, pedir=None, buscar=None, ia=None):
    """Lê discursos e notícias do deputado sobre o tema e grava a leitura da IA. Devolve o registro gravado."""
    t = obter(tid)
    with engine.connect() as c:
        dep = c.execute(select(deputados).where(deputados.c.codigo == cod)).mappings().first()
    if not t or not dep:
        return None
    dep = dict(dep)
    alvo = palavras(t.get("ementa")) | palavras((t.get("palavras") or "").replace(",", " "))
    fontes = discursos(cod, alvo, pedir) + noticias(dep["nome"], termos_busca(t), buscar)
    reg = {"termometro_id": tid, "codigo_deputado": cod, "fontes_lidas": len(fontes), "criado_por": autor,
           "criado_em": datetime.now(), "modelo": None}
    if not fontes:
        reg.update(posicao="sem_sinal", confianca=0, evidencias="[]",
                   justificativa="Nenhum discurso no Plenário nem notícia com o nome do deputado sobre o tema.")
    else:
        import questoes
        import radar
        chamar = ia or (lambda msg: questoes.chamar_ia(msg, sistema=SISTEMA, modelo_=questoes.modelo_rapido(),
                                                       max_tokens=1500, timeout=90,
                                                       preferir=questoes.preferidas_radar()))
        resposta = chamar(montar_pedido(t, dep, fontes))
        d = radar.extrair_json(resposta)
        pos = d.get("posicao") if d.get("posicao") in ("favor", "contra", "indefinido") else "indefinido"
        try:
            conf = max(0, min(100, int(d.get("confianca") or 0)))
        except (TypeError, ValueError):
            conf = 0
        evid = []
        for e in d.get("evidencias") or []:
            try:
                f = fontes[int(e.get("fonte")) - 1]
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
            evid.append({"tipo": f["tipo"], "data": f.get("data"), "titulo": f.get("titulo"), "link": f.get("link"),
                         "trecho": str(e.get("trecho") or "")[:400]})
        if not evid:          # sem evidência citada, guarda as fontes lidas para conferência
            evid = [{"tipo": f["tipo"], "data": f.get("data"), "titulo": f.get("titulo"), "link": f.get("link"),
                     "trecho": ""} for f in fontes[:5]]
        reg.update(posicao=pos, confianca=conf if pos != "indefinido" else min(conf, 40),
                   justificativa=str(d.get("justificativa") or "")[:1000], evidencias=json.dumps(evid, default=str),
                   modelo=getattr(questoes.ULTIMA, "provedor", None) if ia is None else "teste")
    with engine.begin() as c:
        c.execute(delete(termometro_ia).where(termometro_ia.c.termometro_id == tid,
                                              termometro_ia.c.codigo_deputado == cod))
        c.execute(insert(termometro_ia).values(**reg))
    return reg


# fila de análises (um worker no Railway: o estado em memória basta)
_fila = {}
_fila_lock = threading.Lock()


def status_fila(tid):
    return _fila.get(tid)


def analisar_em_segundo_plano(tid, codigos, autor=None):
    """Põe deputados na fila da IA. Devolve False se já há uma análise rodando neste termômetro."""
    with _fila_lock:
        st = _fila.get(tid)
        if st and st["rodando"]:
            return False
        _fila[tid] = st = {"rodando": True, "total": len(codigos), "feitos": 0, "erros": [], "atual": None,
                           "codigos": list(codigos), "inicio": datetime.now()}

    def rodar():
        try:
            for cod in codigos:
                st["atual"] = cod
                try:
                    analisar_deputado(tid, cod, autor)
                except Exception as e:
                    log.exception("termômetro: IA do deputado %s", cod)
                    st["erros"].append(f"{cod}: {str(e)[:200]}")
                    if "sem cota" in str(e) or "não configurada" in str(e):
                        break
                st["feitos"] += 1
        finally:
            st["rodando"], st["atual"], st["fim"] = False, None, datetime.now()
    threading.Thread(target=rodar, daemon=True).start()
    return True


def escolher_para_ia(prev, limite=LOTE_IA):
    """Os mais indecisos ainda sem leitura da IA nem posição apurada."""
    cand = [d for d in prev["deputados"] if not d["ia"] and not d["manual"]]
    cand.sort(key=lambda d: (abs(d["p_sim"] - 0.5), d["nome"]))
    return [d["codigo"] for d in cand[:limite]]


# ---------------- rotas ----------------

def _usuario():
    return g.get("usuario") or {}


def _contexto_base():
    return {"aba": "termometro", "op": metrics.opcoes_filtro()}


def pagina_lista():
    pauta, itens = itens_pauta()
    busca = (request.args.get("busca") or "").strip()
    ts = lista(incluir_arquivados=request.args.get("arquivados") == "1")
    resumo = {}
    if ts:
        cands = _candidatas()
        for t in ts[:30]:
            try:
                resumo[t["id"]] = calcular(t, votacoes_similares(t, cands))["placar"]
            except Exception:
                log.exception("termômetro %s", t["id"])
    return render_template("termometro.html", **_contexto_base(), termometros=ts, resumo=resumo, pauta=pauta,
                           itens=itens, busca=busca, achados=buscar_proposicao(busca) if busca else [],
                           arquivados=request.args.get("arquivados") == "1")


@bp.post("/novo")
def novo():
    f = request.form
    try:
        tid = criar({"proposicao_id": f.get("proposicao_id", type=int), "identificacao": f.get("identificacao"),
                     "ementa": f.get("ementa"), "palavras": f.get("palavras"),
                     "orientacao_oposicao": f.get("orientacao_oposicao"), "evento_id": f.get("evento_id", type=int),
                     "observacao": f.get("observacao")}, _usuario().get("login"))
    except ValueError as e:
        flash(str(e), "erro")
        return redirect(url_for("termometro"))
    flash("Termômetro criado.", "ok")
    return redirect(url_for("termometro.ver", tid=tid))


def _filtrar(deps, a):
    fx = a.getlist("faixa")
    partido, uf = a.get("partido", ""), a.get("uf", "")
    busca = sem_acento(a.get("q", "")).lower().strip()
    out = []
    for d in deps:
        if fx and d["faixa"] not in fx:
            continue
        if partido and d["partido"] != partido:
            continue
        if uf and d["uf"] != uf:
            continue
        if busca and busca not in sem_acento(d["nome"]).lower():
            continue
        if a.get("so") == "notas" and not d["notas"]:
            continue
        if a.get("so") == "contatos" and not d["contatos"]:
            continue
        if a.get("so") == "apurados" and not d["manual"]:
            continue
        out.append(d)
    return out


@bp.get("/<int:tid>")
def ver(tid):
    t = obter(tid)
    if not t:
        abort(404)
    prev = calcular(t)
    deps = _filtrar(prev["deputados"], request.args)
    por_partido = defaultdict(lambda: defaultdict(int))
    for d in prev["deputados"]:
        por_partido[d["partido"] or "—"][d["faixa"]] += 1
    partidos = sorted(por_partido.items(), key=lambda x: -sum(x[1].values()))
    st = status_fila(tid)
    return render_template("termometro_ver.html", **_contexto_base(), t=t, prev=prev, deps=deps,
                           partidos=partidos, faixas=FAIXAS, rotulo_faixa=ROTULO_FAIXA, posicoes=POSICOES,
                           rotulo_posicao=ROTULO_POSICAO, rotulo_ia=ROTULO_IA, notas_gerais=notas(tid, geral=True),
                           fila=st, lote=LOTE_IA, msg_whats=lambda d, c=None: mensagem_padrao(t, d, c, _usuario()),
                           link_whatsapp=link_whatsapp,
                           opc_partidos=sorted({d["partido"] for d in prev["deputados"] if d["partido"]}),
                           opc_ufs=sorted({d["uf"] for d in prev["deputados"] if d["uf"]}))


@bp.post("/<int:tid>/editar")
def editar_rota(tid):
    if not obter(tid):
        abort(404)
    editar(tid, request.form)
    flash("Termômetro atualizado.", "ok")
    return redirect(url_for("termometro.ver", tid=tid))


@bp.post("/<int:tid>/arquivar")
def arquivar(tid):
    t = obter(tid)
    if not t:
        abort(404)
    with engine.begin() as c:
        c.execute(update(termometros).where(termometros.c.id == tid).values(arquivado=not t.get("arquivado")))
    flash("Termômetro arquivado." if not t.get("arquivado") else "Termômetro reaberto.", "ok")
    return redirect(url_for("termometro"))


@bp.post("/<int:tid>/apagar")
def apagar(tid):
    t = obter(tid)
    if not t:
        abort(404)
    u = _usuario()
    if t["criado_por"] != u.get("login") and not u.get("admin"):
        abort(403)
    with engine.begin() as c:
        for tab in (termometro_ia, termometro_posicoes, termometro_notas):
            c.execute(delete(tab).where(tab.c.termometro_id == tid))
        c.execute(delete(termometros).where(termometros.c.id == tid))
    flash("Termômetro apagado.", "ok")
    return redirect(url_for("termometro"))


@bp.post("/<int:tid>/nota")
def nota_geral(tid):
    if not obter(tid):
        abort(404)
    anotar(tid, None, request.form.get("texto"), _usuario().get("login"))
    return redirect(url_for("termometro.ver", tid=tid) + "#notas")


@bp.post("/nota/<int:nid>/apagar")
def nota_apagar(nid):
    r = apagar_nota(nid, _usuario())
    if not r:
        abort(404)
    if r["codigo_deputado"]:
        return redirect(url_for("termometro.deputado", tid=r["termometro_id"], cod=r["codigo_deputado"]) + "#notas")
    return redirect(url_for("termometro.ver", tid=r["termometro_id"]) + "#notas")


@bp.post("/<int:tid>/ia")
def ia_lote(tid):
    t = obter(tid)
    if not t:
        abort(404)
    import questoes
    if not questoes.configurado():
        flash("Nenhuma IA configurada no Railway (GEMINI_API_KEY ou uma reserva).", "erro")
        return redirect(url_for("termometro.ver", tid=tid))
    cods = request.form.getlist("cod", type=int) or escolher_para_ia(calcular(t))
    if not cods:
        flash("Todos os deputados já têm leitura da IA ou posição apurada.", "ok")
    elif analisar_em_segundo_plano(tid, cods, _usuario().get("login")):
        flash(f"Analisando discursos e notícias de {len(cods)} deputado(s). A página se atualiza sozinha.", "ok")
    else:
        flash("Já há uma análise em andamento neste termômetro.", "erro")
    return redirect(url_for("termometro.ver", tid=tid))


@bp.get("/<int:tid>/deputado/<int:cod>")
def deputado(tid, cod):
    t = obter(tid)
    if not t:
        abort(404)
    prev = calcular(t)
    d = next((x for x in prev["deputados"] if x["codigo"] == cod), None)
    if not d:
        abort(404)
    sims = {s["id"]: s for s in prev["similares"]}
    with engine.connect() as c:
        votos = c.execute(text("SELECT votacao_id, voto FROM votos WHERE codigo_deputado = :c AND votacao_id IN :ids")
                          .bindparams(bindparam("ids", expanding=True)),
                          {"c": cod, "ids": list(sims) or [""]}).all()
    historico = sorted([{**sims[v], "voto": voto, "com": voto == sims[v]["ori_opo"]} for v, voto in votos],
                       key=lambda x: (not x["mesma"], -x["peso"]))
    ia = d["ia"]
    evid = json.loads(ia["evidencias"] or "[]") if ia else []
    u = _usuario()
    return render_template("termometro_dep.html", **_contexto_base(), t=t, d=d, historico=historico, ia=ia,
                           evid=evid, notas=notas(tid, cod), posicoes=POSICOES, rotulo_posicao=ROTULO_POSICAO,
                           rotulo_faixa=ROTULO_FAIXA, rotulo_ia=ROTULO_IA, gabinete=telefone_gabinete(cod),
                           msg=mensagem_padrao(t, d, None, u), msg_contato=lambda c_: mensagem_padrao(t, d, c_, u),
                           link_whatsapp=link_whatsapp, fila=status_fila(tid),
                           editar_contato=request.args.get("editar", type=int))


@bp.post("/<int:tid>/deputado/<int:cod>/posicao")
def posicao(tid, cod):
    if not obter(tid):
        abort(404)
    marcar_posicao(tid, cod, request.form.get("posicao", ""), _usuario().get("login"))
    if request.form.get("texto"):
        anotar(tid, cod, request.form.get("texto"), _usuario().get("login"))
    flash("Posição registrada.", "ok")
    if request.form.get("voltar") == "lista":
        return redirect(url_for("termometro.ver", tid=tid) + f"#d{cod}")
    return redirect(url_for("termometro.deputado", tid=tid, cod=cod))


@bp.post("/<int:tid>/deputado/<int:cod>/nota")
def nota_deputado(tid, cod):
    if not obter(tid):
        abort(404)
    anotar(tid, cod, request.form.get("texto"), _usuario().get("login"))
    return redirect(url_for("termometro.deputado", tid=tid, cod=cod) + "#notas")


@bp.post("/<int:tid>/deputado/<int:cod>/ia")
def ia_deputado(tid, cod):
    if not obter(tid):
        abort(404)
    import questoes
    if not questoes.configurado():
        flash("Nenhuma IA configurada no Railway (GEMINI_API_KEY ou uma reserva).", "erro")
    elif analisar_em_segundo_plano(tid, [cod], _usuario().get("login")):
        flash("Lendo discursos e notícias do deputado. A página se atualiza sozinha.", "ok")
    else:
        flash("Já há uma análise em andamento neste termômetro. Tente de novo quando ela terminar.", "erro")
    return redirect(url_for("termometro.deputado", tid=tid, cod=cod) + "#ia")


@bp.post("/<int:tid>/deputado/<int:cod>/contato")
def contato(tid, cod):
    try:
        salvar_contato(cod, request.form, _usuario().get("login"), request.form.get("cid", type=int))
        flash("Contato salvo. Ele aparece em todos os termômetros.", "ok")
    except ValueError as e:
        flash(str(e), "erro")
    return redirect(url_for("termometro.deputado", tid=tid, cod=cod) + "#contatos")


@bp.post("/<int:tid>/deputado/<int:cod>/contato/<int:cid>/apagar")
def contato_apagar(tid, cod, cid):
    apagar_contato(cod, cid)
    flash("Contato apagado.", "ok")
    return redirect(url_for("termometro.deputado", tid=tid, cod=cod) + "#contatos")


@bp.app_template_filter("dt_curta")
def _dt_curta(v):
    if not v:
        return ""
    s = str(v)
    return f"{s[8:10]}/{s[5:7]}/{s[0:4]}" + (f" {s[11:16]}" if len(s) >= 16 else "")
