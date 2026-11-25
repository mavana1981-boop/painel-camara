"""Votações simbólicas provisórias, achadas na tramitação das matérias da pauta.

A Câmara costuma demorar a publicar as votações simbólicas no endpoint /votacoes dos Dados Abertos, mas a Mesa
registra o resultado ("Aprovado o Projeto...", "Rejeitada a Emenda...") na tramitação da proposição bem antes.
Na atualização ao vivo de Votações, o painel lê a tramitação (órgão PLEN) das matérias das pautas do período
vindas do Minha Pauta e grava cada decisão como uma votação simbólica provisória (id "TRAM-...").

- Votação nominal (despacho com placar "Sim: X; Não: Y") também entra, como nominal provisória (id "TRAM-N..."),
  com o placar da tramitação: a API /votacoes às vezes leva horas para publicar a nominal com os votos.
- Nominal provisória com voto a voto: o serviço antigo da Câmara (SitCamaraWS, ObterVotacaoProposicao) publica
  votos e orientações logo depois da votação. Achando lá a mesma votação (dia + placar), ela é gravada como
  nominal completa em `votacoes` (id "WS-..."), igual às da API; sem ela, fica a "TRAM-N..." só com o placar.
  As "WS-" do período também são refeitas a cada atualização e somem quando a votação oficial sai.
- Uma decisão que já tem votação oficial da mesma matéria com resultado parecido não vira provisória.
- As provisórias do período são refeitas a cada atualização: quando a votação oficial sai, a provisória some.
- A coleta diária substitui todas as simbólicas do ano e também as apaga.
- Só cobre matérias que estão nas pautas do Minha Pauta (extrapauta fora de lá não é pega).
"""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select

import xml.etree.ElementTree as ET

from db import (engine, orientacoes, orientacoes_partido, plenario_pautas, sem_acento, votacoes,
                votacoes_simbolicas, votos)

log = logging.getLogger("simbolicas_provisorias")

ATIVO = os.environ.get("SIMBOLICAS_TRAMITACAO", "1") != "0"
PREFIXO = "TRAM-"
ETAPA = "Registro provisório, pela tramitação — aguardando a Câmara publicar a votação nos Dados Abertos"
PREFIXO_NOMINAL = PREFIXO + "N"
PREFIXO_WS = "WS-"   # nominal provisória com voto a voto (serviço antigo), gravada em `votacoes`
WS_ATIVO = os.environ.get("VOTOS_SITCAMARAWS", "1") != "0"
ETAPA_NOMINAL = ("Votação nominal — placar pela tramitação: Sim {sim} × Não {nao}. Registro provisório: votos por "
                 "deputado e orientações saem quando a Câmara publicar a votação nos Dados Abertos")
_ETAPA_PLACAR = re.compile(r"Sim (\d+) × Não (\d+)")
MAX_ITENS = int(os.environ.get("SIMBOLICAS_TRAMITACAO_MAX", "80"))

_OBJETO = (r"(?:(?:o|a|os|as)\s+)?(?:projeto|proposta|parecer|substitutivo|redacao final|emendas?|pec|mensagem|"
           r"medida provisoria|texto|materia|subemendas?|em (?:primeiro|segundo|turno)|projeto de decreto)")
_APROV = re.compile(r"^\s*aprovad[oa]s?\s+" + _OBJETO)
_REJEIT = re.compile(r"^\s*rejeitad[oa]s?\s+" + _OBJETO)
# despacho de votação nominal traz o placar ("Sim: 353; Não: 128; Total: 486")
_PLACAR = re.compile(r"\bsim:?\s*(\d+)\W{0,6}\s*nao:?\s*(\d+)")
_PROJ = re.compile(r"^\s*([A-Z]{2,6})\s+(\d+)\s*/\s*(\d{4})")


def _plano(s):
    return " ".join(sem_acento(s or "").lower().split())


def _frases(desp):
    return [f.strip() for f in re.split(r"(?<=[.;])\s+(?=[A-ZÀ-Ý])", desp or "") if f.strip()]


def decisoes_na_tramitacao(trams, inicio: date, fim: date):
    """Tramitações (API v2) → decisões do Plenário entre inicio e fim: [{data, texto, aprovacao}]."""
    out = []
    for t in sorted((t for t in trams or [] if isinstance(t, dict)), key=lambda t: t.get("dataHora") or ""):
        if (t.get("siglaOrgao") or "").strip().upper() != "PLEN":
            continue
        try:
            dh = datetime.fromisoformat((t.get("dataHora") or "")[:19])
        except ValueError:
            continue
        if not inicio <= dh.date() <= fim:
            continue
        desp = re.sub(r"\s+", " ", t.get("despacho") or "")
        frases = _frases(desp)
        idx = [i for i, f in enumerate(frases) if _APROV.match(_plano(f)) or _REJEIT.match(_plano(f))]
        for k, i in enumerate(idx):
            p = _plano(frases[i])
            # placar da decisão: na própria frase ou nas seguintes, até a próxima decisão
            resto = " ".join(_plano(f) for f in frases[i:idx[k + 1] if k + 1 < len(idx) else len(frases)])
            m = _PLACAR.search(resto)
            texto = frases[i]
            if m:   # tira o placar do texto da decisão, se vier na mesma frase
                texto = re.split(r"\s*\bSim\s*:", texto)[0]
            out.append({"data": dh, "texto": texto.strip().rstrip(".;,")[:600], "aprovacao": bool(_APROV.match(p)),
                        "placar": (int(m.group(1)), int(m.group(2))) if m else None})
    return out


def _itens_pauta(conn, inicio, fim):
    """Matérias (id, "PL 1/2026", ementa) das pautas do Minha Pauta do período (e da véspera: sessão que vira o dia)."""
    q = select(plenario_pautas.c.itens).where(plenario_pautas.c.data >= (inicio - timedelta(days=1)).isoformat(),
                                              plenario_pautas.c.data <= fim.isoformat())
    vistos, out = set(), []
    for (itens,) in conn.execute(q):
        for i in json.loads(itens or "[]"):
            pid = str(i.get("id_principal") or "")
            if pid.isdigit() and pid not in vistos:
                vistos.add(pid)
                out.append((int(pid), i.get("projeto_original") or i.get("projeto") or "", i.get("ementa") or ""))
    return out[:MAX_ITENS]


def _oficiais(conn, inicio, fim):
    """{proposicao_id: [(resultado normalizado, data)]} das votações oficiais (nominais e simbólicas) do período."""
    a = datetime.combine(inicio - timedelta(days=1), datetime.min.time())   # a sessão pode virar o dia
    b = datetime.combine(fim + timedelta(days=2), datetime.min.time())
    out = {}
    for t in (votacoes, votacoes_simbolicas):
        q = select(t.c.proposicao_id, t.c.descricao_votacao, t.c.data).where(t.c.data >= a, t.c.data < b,
                                                                     t.c.proposicao_id.is_not(None))
        q = q.where(~t.c.id.startswith(PREFIXO if t is votacoes_simbolicas else PREFIXO_WS))
        for pid, desc, dt in conn.execute(q):
            out.setdefault(pid, []).append((_plano(desc), dt))
    return out


def _ja_oficial(d, oficiais):
    """Já existe votação oficial da matéria com o mesmo resultado. (A hora não serve para comparar: na
    tramitação do Plenário todas as entradas levam a hora de abertura da sessão.)"""
    p = _plano(d["texto"])[:25]
    return any(o and (o.startswith(p) or p.startswith(o[:25])) for o, _ in oficiais)


def _attrs(el):
    return {k.strip().lower(): (v or "").strip() for k, v in el.attrib.items()}


def _tag(el):
    return el.tag.rsplit("}", 1)[-1].lower()


def votacao_no_ws(conteudo, dia: date, placar):
    """Do XML do ObterVotacaoProposicao, a votação do dia com o placar (sim, não) do Resumo:
    {hora, resumo, objeto, votos: [{id, nome, partido, uf, voto}], orientacoes: [(bancada, voto)]} ou None."""
    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError:
        return None
    for el in raiz.iter():
        if _tag(el) != "votacao":
            continue
        a = _attrs(el)
        try:
            if datetime.strptime(a.get("data", ""), "%d/%m/%Y").date() != dia:
                continue
        except ValueError:
            continue
        m = _PLACAR.search(_plano(a.get("resumo")))
        if not m or (int(m.group(1)), int(m.group(2))) != tuple(placar):
            continue
        vs, oris = [], []
        for x in el.iter():
            t, b = _tag(x), _attrs(x)
            if t == "deputado" and b.get("idecadastro", "").isdigit():
                voto = b.get("voto", "")
                if voto and voto != "-":
                    vs.append({"id": b["idecadastro"], "nome": b.get("nome", ""), "partido": b.get("partido", ""),
                               "uf": b.get("uf", ""), "voto": voto})
            elif "sigla" in b and "orientacao" in b and b["orientacao"]:
                oris.append((b["sigla"], b["orientacao"]))
        if vs:
            return {"hora": a.get("hora", ""), "resumo": a.get("resumo", ""), "objeto": a.get("objvotacao", ""),
                    "votos": vs, "orientacoes": oris}
    return None


def _baixar_ws(sigla, numero, ano):
    import objeto_votacao
    return objeto_votacao._baixar(sigla, numero, ano)


def _linhas_ws(vid, pid, projeto, ementa, d, w):
    """Votação do serviço antigo → linhas no formato dos arquivos (para collector.gravar_votacoes_ano)."""
    hm = re.match(r"(\d{1,2}):(\d{2})", w["hora"] or "")
    dt = d["data"].replace(hour=int(hm.group(1)), minute=int(hm.group(2))) if hm else d["data"]
    sim = sum(1 for x in w["votos"] if _plano(x["voto"]) == "sim")
    nao = sum(1 for x in w["votos"] if _plano(x["voto"]) == "nao")
    vot = {"id": vid, "siglaOrgao": "PLEN", "dataHoraRegistro": dt.isoformat(), "data": dt.date().isoformat(),
           "aprovacao": "1" if d["aprovacao"] else "0", "votosSim": str(sim), "votosNao": str(nao),
           "votosOutros": str(len(w["votos"]) - sim - nao), "descricao": w["resumo"] or d["texto"],
           "ultimaAberturaVotacao_descricao": w["objeto"], "ultimaApresentacaoProposicao_descricao": "",
           "ultimaApresentacaoProposicao_idProposicao": str(pid)}
    linhas_votos = [{"idVotacao": vid, "voto": x["voto"], "deputado_id": x["id"], "deputado_nome": x["nome"],
                     "deputado_siglaPartido": x["partido"], "deputado_siglaUf": x["uf"]} for x in w["votos"]]
    linhas_ori = [{"idVotacao": vid, "siglaBancada": b, "orientacao": o} for b, o in w["orientacoes"]]
    props = [{"idVotacao": vid, "proposicao_id": str(pid), "proposicao_titulo": projeto,
              "proposicao_ementa": ementa}]
    return [vot], linhas_votos, linhas_ori, props


def _apagar_ws(conn, a, b):
    ids = select(votacoes.c.id).where(votacoes.c.id.startswith(PREFIXO_WS), votacoes.c.data >= a,
                                      votacoes.c.data < b)
    antes = {r[0] for r in conn.execute(ids)}
    if antes:
        for t in (votos, orientacoes, orientacoes_partido):
            conn.execute(delete(t).where(t.c.votacao_id.in_(list(antes))))
        conn.execute(delete(votacoes).where(votacoes.c.id.in_(list(antes))))
    return antes


def atualizar(inicio: date, fim: date, ler=None, baixar_ws=None) -> int:
    """Refaz as simbólicas provisórias do período. Devolve quantas entraram, saíram ou mudaram."""
    if not ATIVO:
        return 0
    if ler is None:
        from clipping_auto import _ler_tramitacoes as ler
    if baixar_ws is None and WS_ATIVO:
        baixar_ws = _baixar_ws
    nominais_ws = {}   # vid -> linhas para gravar_votacoes_ano
    with engine.connect() as c:
        itens = _itens_pauta(c, inicio, fim)
        oficiais = _oficiais(c, inicio, fim)
    novas = {}
    for pid, projeto, ementa in itens:
        try:
            trams = ler(pid, inicio.isoformat(), fim.isoformat())
        except Exception as e:
            log.warning("tramitação de %s (%s): %s", projeto, pid, e)
            continue
        m = _PROJ.match(projeto or "")
        for n, d in enumerate(decisoes_na_tramitacao(trams, inicio, fim)):
            if _ja_oficial(d, oficiais.get(pid, ())):
                continue
            if d["placar"] and m and baixar_ws:   # voto a voto no serviço antigo da Câmara
                try:
                    w = votacao_no_ws(baixar_ws(m.group(1), int(m.group(2)), int(m.group(3))),
                                      d["data"].date(), d["placar"])
                except Exception as e:
                    log.warning("SitCamaraWS %s: %s", projeto, e)
                    w = None
                if w:
                    vid = f"{PREFIXO_WS}{pid}-{d['data']:%y%m%d%H%M}{n}"[:30]
                    nominais_ws[vid] = _linhas_ws(vid, pid, projeto, ementa, d, w)
                    continue
            pre = PREFIXO_NOMINAL if d["placar"] else PREFIXO
            vid = f"{pre}{pid}-{d['data']:%y%m%d%H%M}{n}"[:30]
            etapa = ETAPA_NOMINAL.format(sim=d["placar"][0], nao=d["placar"][1]) if d["placar"] else ETAPA
            novas[vid] = {"id": vid, "ano": d["data"].year, "data": d["data"], "proposicao_id": pid,
                          "sigla_materia": m.group(1) if m else None,
                          "numero_materia": int(m.group(2)) if m else None,
                          "ano_materia": int(m.group(3)) if m else None,
                          "descricao_materia": ementa or None, "descricao_votacao": d["texto"],
                          "aprovacao": d["aprovacao"], "etapa": etapa}
    a = datetime.combine(inicio, datetime.min.time())
    b = datetime.combine(fim + timedelta(days=1), datetime.min.time())
    with engine.begin() as c:
        filtro = (votacoes_simbolicas.c.id.startswith(PREFIXO), votacoes_simbolicas.c.data >= a,
                  votacoes_simbolicas.c.data < b)
        antes = {r[0]: (r[1], r[2]) for r in c.execute(
            select(votacoes_simbolicas.c.id, votacoes_simbolicas.c.descricao_votacao,
                   votacoes_simbolicas.c.aprovacao).where(*filtro))}
        c.execute(delete(votacoes_simbolicas).where(*filtro))
        if novas:
            ids = list(novas)   # uma provisória fora da janela com o mesmo id (não deveria acontecer)
            c.execute(delete(votacoes_simbolicas).where(votacoes_simbolicas.c.id.in_(ids)))
            c.execute(votacoes_simbolicas.insert(), list(novas.values()))
        ws_antes = _apagar_ws(c, a, b)
        if nominais_ws:
            import collector
            for vid, (lv, lvo, lor, lpr) in nominais_ws.items():
                collector.gravar_votacoes_ano(c, int(lv[0]["data"][:4]), lv, lvo, lor, lpr, parcial=True)
    depois = {k: (v["descricao_votacao"], v["aprovacao"]) for k, v in novas.items()}
    mudou = len(set(antes) ^ set(depois)) + len(ws_antes ^ set(nominais_ws)) + sum(1 for k in set(antes) & set(depois) if antes[k] != depois[k])
    if novas or antes or nominais_ws or ws_antes:
        log.info("provisórias %s a %s: %d pela tramitação, %d nominais com voto a voto (de %d matérias da pauta)",
                 inicio, fim, len(novas), len(nominais_ws), len(itens))
    return mudou


def placar_da_etapa(etapa):
    """(sim, não) gravados na etapa de uma nominal provisória, ou None."""
    m = _ETAPA_PLACAR.search(etapa or "")
    return (int(m.group(1)), int(m.group(2))) if m else None
