"""O que exatamente foi votado em cada votação do Plenário (para o relatório da proposição).

A API de dados abertos muitas vezes só diz "Rejeitado o Requerimento." ou "Mantido o texto." — sem dizer
qual requerimento, nem o número e a autoria do destaque. O serviço antigo da Câmara (SitCamaraWS,
ObterVotacaoProposicao) traz isso no campo ObjVotacao de cada votação, por exemplo:

    "REQUERIMENTO DE ADIAMENTO DA DISCUSSÃO"
    "DTQ 7 - PL - EMENDA DE PLENÁRIO Nº 38"
    "DTQ 1 - FDR PSOL-REDE - §5°, DO ART. 5° DA LEI COMPLEMENTAR N° 200/2023..."

As votações do serviço antigo são casadas com as nossas pelo dia e pelo texto do resultado (que é o
mesmo nos dois) ou, na falta, pelo placar. O objeto fica guardado em `votacoes_objeto`; uma votação já
feita não muda, então só se busca de novo quando aparece votação nova sem objeto (no máximo 1x/hora).
Sem o serviço, o complemento sai do próprio texto do resultado ("requerimento nº 4504/2026, Do Sr. ...,
que solicita urgência...") ou do objeto que veio no arquivo de dados abertos.
"""
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests
from sqlalchemy import select, text

from db import engine, meta, sem_acento, votacoes_objeto

log = logging.getLogger("objeto_votacao")
URL = os.environ.get("CAMARA_WS_VOTACOES",
                     "https://www.camara.leg.br/SitCamaraWS/Proposicoes.asmx/ObterVotacaoProposicao")
PRAZO = 12          # segundos: o PDF não pode ficar preso esperando a Câmara
REBUSCA = timedelta(hours=1)


# ---------------- serviço da Câmara ----------------

def _baixar(sigla, numero, ano):
    r = requests.get(URL, params={"tipo": sigla, "numero": numero, "ano": ano}, timeout=PRAZO,
                     headers={"User-Agent": "painel-camara-oposicao/1.0"})
    r.raise_for_status()
    return r.content


def ler_xml(conteudo):
    """[{dia, hora, resumo, objeto}] de cada <Votacao> do XML do ObterVotacaoProposicao."""
    raiz = ET.fromstring(conteudo)
    saida = []
    for el in raiz.iter():
        if el.tag.rsplit("}", 1)[-1] != "Votacao":
            continue
        a = {k.strip(): (v or "").strip() for k, v in el.attrib.items()}
        try:
            dia = datetime.strptime(a.get("Data", ""), "%d/%m/%Y").date()
        except ValueError:
            continue
        saida.append({"dia": dia, "hora": a.get("Hora", ""), "resumo": a.get("Resumo", ""),
                      "objeto": a.get("ObjVotacao", "")})
    return saida


def _plano(s):
    return " ".join(sem_acento(s or "").lower().replace(".", " ").split())


def _placar(s):
    m_s, m_n = re.search(r"sim:\s*(\d+)", s or "", re.I), re.search(r"n[aã]o:\s*(\d+)", s or "", re.I)
    return (int(m_s.group(1)) if m_s else None, int(m_n.group(1)) if m_n else None)


def _minutos(h):
    m = re.match(r"(\d{1,2}):(\d{2})", h or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _quando(d):
    if isinstance(d, datetime):
        return d
    try:
        return datetime.fromisoformat(str(d)[:19]) if d else None
    except ValueError:
        return None


def casar(nossas, do_ws):
    """{votacao_id: objeto}. nossas: dicts com id, data, descricao_votacao, qtd_sim, qtd_nao."""
    livres = [w for w in do_ws if w["objeto"]]
    saida = {}

    def melhor(v, cands):
        if not cands:
            return None
        dt = _quando(v["data"])
        hv = dt.hour * 60 + dt.minute
        return min(cands, key=lambda w: abs((_minutos(w["hora"]) or hv) - hv))

    def placar_v(v):
        return (v.get("qtd_sim"), v.get("qtd_nao")) if v.get("qtd_sim") is not None else (None, None)

    # do mais seguro ao menos seguro; cada votação do serviço só casa com uma das nossas
    passos = (
        lambda v, w: bool(_plano(w["resumo"])) and _plano(v.get("descricao_votacao")) == _plano(w["resumo"]),
        lambda v, w: _plano(_sem_placar(v.get("descricao_votacao"))) == _plano(_sem_placar(w["resumo"]))
                     and None not in placar_v(v) and placar_v(v) == _placar(w["resumo"]),
        lambda v, w: None not in placar_v(v) and placar_v(v) == _placar(w["resumo"]),
        "unico",
    )
    for passo in passos:
        for v in nossas:
            dt = _quando(v.get("data"))
            if v["id"] in saida or not dt or not (v.get("descricao_votacao") or v.get("qtd_sim") is not None):
                continue
            mesmo_dia = [w for w in livres if w["dia"] == dt.date()]
            if passo == "unico":    # mesmo texto sem placar, e só uma possibilidade
                alvo = _plano(_sem_placar(v.get("descricao_votacao")))
                cands = [w for w in mesmo_dia if alvo and _plano(_sem_placar(w["resumo"])) == alvo]
                cands = cands if len(cands) == 1 else []
            else:
                cands = [w for w in mesmo_dia if passo(v, w)]
            w = melhor(v, cands)
            if w:
                saida[v["id"]] = w["objeto"][:1000]
                livres.remove(w)
    return saida


def _materia(ident):
    m = re.match(r"\s*([A-Za-z]+)\s+(\d+)\s*/\s*(\d{4})", ident or "")
    return (m.group(1).upper(), int(m.group(2)), int(m.group(3))) if m else None


def completar(ident, lista, buscar=True):
    """Põe em cada votação de `lista` (itens de metrics.detalhe_votacao) a chave 'objeto' (ObjVotacao),
    buscando no serviço da Câmara as que ainda não temos. Nunca levanta erro."""
    vs = [x["votacao"] for x in lista]
    if not vs:
        return
    ids = [v["id"] for v in vs]
    try:
        with engine.connect() as c:
            guardados = dict(c.execute(select(votacoes_objeto.c.votacao_id, votacoes_objeto.c.objeto)
                                       .where(votacoes_objeto.c.votacao_id.in_(ids))).all())
        faltam = [v for v in vs if v["id"] not in guardados and not v.get("simbolica")]   # o serviço só tem nominais
        mat = _materia(ident)
        if faltam and mat and buscar:
            chave = f"objvot:{mat[0]} {mat[1]}/{mat[2]}"[:60]
            with engine.connect() as c:
                ult = c.execute(select(meta.c.valor).where(meta.c.chave == chave)).scalar()
            recente = False
            if ult:
                try:
                    recente = datetime.now() - datetime.fromisoformat(ult) < REBUSCA
                except ValueError:
                    pass
            if not recente:
                novos = {}
                try:
                    novos = casar(faltam, ler_xml(_baixar(*mat)))
                except Exception as e:      # serviço fora do ar, XML inesperado...
                    log.warning("ObjVotacao de %s indisponível: %s", chave, e)
                with engine.begin() as c:
                    c.execute(meta.delete().where(meta.c.chave == chave))
                    c.execute(meta.insert().values(chave=chave, valor=datetime.now().isoformat(timespec="seconds")))
                    for vid, obj in novos.items():
                        c.execute(votacoes_objeto.delete().where(votacoes_objeto.c.votacao_id == vid))
                        c.execute(votacoes_objeto.insert().values(votacao_id=vid, objeto=obj,
                                                                  buscado_em=datetime.now()))
                guardados.update(novos)
    except Exception:
        log.exception("falha ao completar o objeto das votações")
        guardados = {}
    for v in vs:
        v["objeto"] = guardados.get(v["id"])
        v.update(descrever(v))


# ---------------- texto para o relatório ----------------

_SIGLAS = {"PL", "PLP", "PEC", "MPV", "PDL", "PLV", "PRC", "PDC", "RICD", "CF", "REQ", "DTQ", "DVS", "EMP",
           "EMC", "EMA", "SBT", "LDO", "LOA", "PPA", "SUS", "INSS", "ICMS", "IPI", "IR", "IBS", "CBS", "STF",
           "TCU", "CPI", "CPMI", "ADCT"}


def capitaliza(s):
    """'EMENDA DE PLENÁRIO Nº 38 AO ART. 2º DO PLP 299/2026' -> 'Emenda de Plenário nº 38 ao art. 2º do PLP
    299/2026' (só mexe em texto todo em maiúsculas; siglas, números e romanos ficam)."""
    s = " ".join((s or "").split())
    letras = [c for c in s if c.isalpha()]
    if not letras or sum(c.isupper() for c in letras) < 0.8 * len(letras):
        return s
    out = []
    for tok in s.split(" "):
        nucleo = re.sub(r"[^\wÀ-ú]", "", tok)
        if nucleo.upper() in _SIGLAS or (len(nucleo) > 1 and re.fullmatch(r"[IVXLC]+", nucleo)):
            out.append(tok)
        else:
            out.append(tok.lower())
    t = " ".join(out)
    t = re.sub(r"\bn([º°o])\b", r"n\1", t)
    t = t.replace("plenário", "Plenário").replace("senado federal", "Senado Federal") \
         .replace("câmara dos deputados", "Câmara dos Deputados").replace("constituição federal", "Constituição Federal") \
         .replace("lei complementar", "Lei Complementar")
    return t[:1].upper() + t[1:]


def _autoria(a):
    a = " ".join((a or "").split())
    if not a:
        return None
    a = re.sub(r"^FDR\.?\s+", "Federação ", a, flags=re.I)
    a = re.sub(r"^BL\.?\s+", "Bloco ", a, flags=re.I)
    if re.match(r"^(DEP|DEPUTAD[OA])\.?\s", a, re.I):
        a = "Dep. " + capitaliza(re.sub(r"^(DEP|DEPUTAD[OA])\.?\s+", "", a, flags=re.I)).title()
    return a


def _destaque(obj):
    """'DTQ 7 - PL - EMENDA DE PLENÁRIO Nº 38' -> ('7', 'PL', 'Emenda de Plenário nº 38')."""
    partes = [p.strip() for p in re.split(r"\s+-\s+", obj.strip(), maxsplit=2)]
    m = re.match(r"(?:DTQ|DVS|DESTAQUE)\D*?(\d+)?\s*$", partes[0], re.I)
    if not m:
        return None
    numero = m.group(1)
    autor = _autoria(partes[1]) if len(partes) >= 3 else None
    alvo = capitaliza(partes[-1]) if len(partes) >= 2 else None
    return numero, autor, alvo


_REQ_DESCR = re.compile(
    r"requerimento\s+n[º°o.]*\s*([\d.]+\s*/\s*\d{4})\s*,?\s*(?:d[oa]s?\s+(?:sr[a]?s?\.?\s+)?(.+?))?\s*,?\s*"
    r"que\s+(?:solicita|requer)\s+(.+)$", re.I)


def _sem_placar(descr):
    """'Aprovado o X. Sim: 318; Não: 135; ...' -> 'Aprovado o X'."""
    return re.split(r"\s+Sim:\s*\d", descr or "", maxsplit=1)[0].strip().rstrip(".").strip()


def descrever(v):
    """{'rotulo': ..., 'dtq_numero': ..., 'dtq_autoria': ..., 'dtq_objeto': ..., 'complemento': ...}
    rotulo = o que aparece no lugar do genérico "Requerimento"/"Destaque"."""
    tipo = v.get("tipo_etapa") or "Votação"
    obj = (v.get("objeto") or "").strip()
    descr = _sem_placar(v.get("descricao_votacao"))
    etapa = (v.get("etapa") or "").strip()
    d = {"rotulo": tipo, "dtq_numero": None, "dtq_autoria": None, "dtq_objeto": None, "complemento": None}

    if obj:
        dt = _destaque(obj) if re.match(r"\s*(DTQ|DVS|DESTAQUE)\b", obj, re.I) else None
        if dt:
            n, autor, alvo = dt
            d.update(dtq_numero=n, dtq_autoria=autor, dtq_objeto=alvo)
            d["rotulo"] = ("DVS" if obj.upper().startswith("DVS") else "Destaque") + (f" nº {n}" if n else "")
            d["complemento"] = alvo
            return d
        texto = capitaliza(obj)
        if tipo == "Requerimento" and not _plano(texto).startswith("requerimento"):
            texto = "Requerimento de " + texto[:1].lower() + texto[1:]
        d["rotulo"] = texto
        m = _REQ_DESCR.search(descr)
        if m and m.group(2):
            d["complemento"] = f"REQ {m.group(1).replace(' ', '')} — autoria: {_autor_req(m.group(2))}"
        return d

    # sem o serviço: o que der para tirar do texto do resultado ou do objeto do arquivo
    m = _REQ_DESCR.search(descr)
    if m:
        quem = f" ({_autor_req(m.group(2))})" if m.group(2) else ""
        d["rotulo"] = f"Requerimento nº {m.group(1).replace(' ', '')}{quem}"
        d["complemento"] = "Pede " + m.group(3).strip().rstrip(".")
        return d
    m = re.search(r"\b(requerimento\s+de\s+.+)$", descr, re.I)
    if m and tipo == "Requerimento":
        d["rotulo"] = capitaliza(m.group(1)) if m.group(1).isupper() else m.group(1)[:1].upper() + m.group(1)[1:]
        return d
    if tipo == "Requerimento" and re.match(r"REQ\b", etapa):
        tit, _, eme = etapa.partition(" — ")
        d["rotulo"] = f"Requerimento {tit.replace('REQ', '').strip()}".strip()
        d["complemento"] = eme or None
        return d
    if tipo == "Destaque" and re.match(r"(DTQ|DVS)\b", etapa):
        tit, _, eme = etapa.partition(" — ")
        dt = _destaque(tit)
        if dt and dt[0]:
            d.update(dtq_numero=dt[0], rotulo=f"Destaque nº {dt[0]}")
        d["complemento"] = eme or None
        d["dtq_objeto"] = eme or None
    return d


def _autor_req(s):
    s = re.sub(r"^(sr[a]?s?\.?\s+)", "", (s or "").strip(), flags=re.I).rstrip(",. ")
    return "Dep. " + s if s and not s.lower().startswith(("dep", "comiss", "líder", "lider", "mesa")) else s
