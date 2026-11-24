"""Coletor de dados da Câmara dos Deputados (Dados Abertos).

Uso:
    python collector.py                    # rotina diária: ano corrente + deputados
    python collector.py --desde 2023-02-01 # carga completa a partir da data (ano a ano)
    python collector.py --deputados        # só atualiza o cadastro de deputados

Fontes (todas públicas, sem chave):
    Arquivos anuais em lote  https://dadosabertos.camara.leg.br/arquivos/{nome}/csv/{nome}-{ano}.csv
        votacoes, votacoesVotos, votacoesOrientacoes, votacoesProposicoes  -> Votações/Orientações
        (completados pela API /votacoes quando o arquivo anual está atrasado)
        proposicoes, proposicoesAutores                                    -> Proposições
    CEAP (cota)              https://www.camara.leg.br/cotas/Ano-{ano}.csv.zip
        plano B: /api/v2/deputados/{id}/despesas
    API v2                   https://dadosabertos.camara.leg.br/api/v2
        /deputados?idLegislatura=57, /deputados (em exercício), /deputados/{id},
        /deputados/{id}/orgaos, /deputados/{id}/frentes, /deputados/{id}/despesas
"""
import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import requests
from sqlalchemy import delete, func, insert, select, update

from db import (INSTITUCIONAIS, autorias, deputados, despesas, engine, init_db, meta, norm_lideranca,
                norm_nome, norm_partido, norm_voto, notificacoes, orientacoes, orientacoes_partido, perfis,
                processos, sem_acento, votacoes, votacoes_simbolicas, votos)

API = os.environ.get("CAMARA_API", "https://dadosabertos.camara.leg.br/api/v2")
ARQUIVOS = os.environ.get("CAMARA_ARQUIVOS", "https://dadosabertos.camara.leg.br/arquivos")
CEAP_ZIP = os.environ.get("CAMARA_CEAP", "https://www.camara.leg.br/cotas/Ano-{ano}.csv.zip")
LEGISLATURA = int(os.environ.get("LEGISLATURA", "57"))
INICIO_LEGISLATURA = os.environ.get("INICIO_LEGISLATURA", "2023-02-01")
ORGAO_VOTACOES = os.environ.get("ORGAO_VOTACOES", "PLEN")   # só votações nominais do Plenário
PAUSA = 0.2
LOTE = 5000

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
log = logging.getLogger("coletor")
sess = requests.Session()
sess.headers.update({"Accept": "application/json", "User-Agent": "painel-camara-oposicao/1.0"})


# ---------------- HTTP ----------------

def _pedir(url, tentativas=7, stream=False, accept=None):
    headers = {"Accept": accept} if accept else None
    for i in range(tentativas):
        try:
            r = sess.get(url, timeout=300 if stream else 60, stream=stream, headers=headers)
        except requests.RequestException as e:
            log.warning("erro de rede em %s: %s", url, e)
            time.sleep(2 * (i + 1))
            continue
        if r.status_code == 200:
            return r
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            espera = min(60, 5 * 2 ** i)  # 5, 10, 20, 40, 60, 60 s
            log.warning("HTTP %s em %s — aguardando %ss", r.status_code, url, espera)
            time.sleep(espera)
            continue
        r.raise_for_status()
    raise RuntimeError(f"Falha ao obter {url} após {tentativas} tentativas")


def get_json(caminho: str):
    url = caminho if caminho.startswith("http") else f"{API}{caminho}"
    r = _pedir(url)
    time.sleep(PAUSA)
    if r is None or not r.content.strip():
        return None
    return r.json()


def paginado(caminho: str, limite_paginas=200):
    """Junta todas as páginas de um recurso da API v2 (segue o link rel="next")."""
    url = caminho if caminho.startswith("http") else f"{API}{caminho}"
    itens = []
    for _ in range(limite_paginas):
        doc = get_json(url)
        if not doc:
            break
        itens.extend(doc.get("dados") or [])
        prox = next((l.get("href") for l in doc.get("links") or [] if l.get("rel") == "next"), None)
        if not prox or prox == url:
            break
        url = prox
    return itens


def baixar(url: str):
    """Baixa um arquivo grande para disco temporário. Devolve o caminho (ou None se não existe)."""
    r = _pedir(url, stream=True, accept="*/*")
    if r is None:
        return None
    fd, caminho = tempfile.mkstemp(prefix="camara-", suffix=os.path.splitext(url)[1] or ".bin")
    with os.fdopen(fd, "wb") as f:
        for bloco in r.iter_content(1 << 20):
            f.write(bloco)
    return caminho


def ler_csv(caminho: str):
    """CSV dos dados abertos: UTF-8 (com BOM), separador ';'. Aceita também .zip com um CSV dentro."""
    if zipfile.is_zipfile(caminho):
        with zipfile.ZipFile(caminho) as z:
            nome = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            with z.open(nome) as bruto:
                yield from _ler_texto(bruto)
        return
    with open(caminho, "rb") as bruto:
        yield from _ler_texto(bruto)


def _ler_texto(bruto):
    amostra = bruto.peek(4096)[:4096] if hasattr(bruto, "peek") else b""
    try:
        amostra.decode("utf-8")
        enc = "utf-8-sig"
    except UnicodeDecodeError:
        enc = "latin-1"
    texto = io.TextIOWrapper(bruto, encoding=enc, errors="replace", newline="")
    for linha in csv.DictReader(texto, delimiter=";"):
        yield {(k or "").strip().lstrip("﻿"): (v or "").strip() if isinstance(v, str) else v
               for k, v in linha.items()}


def arquivo_anual(nome: str, ano: int):
    return f"{ARQUIVOS}/{nome}/csv/{nome}-{ano}.csv"


def _apagar(*caminhos):
    for c in caminhos:
        if c:
            try:
                os.remove(c)
            except OSError:
                pass


def _int(v):
    try:
        return int(float(str(v).replace(",", "."))) if v not in (None, "") else None
    except ValueError:
        return None


def _num(v):
    """'1.234,56' / '1234.56' / 1234.56 -> float."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)[:19])
    except ValueError:
        try:
            return datetime.fromisoformat(str(s)[:10])
        except ValueError:
            return None


def _lote(conn, tabela, linhas):
    for i in range(0, len(linhas), LOTE):
        conn.execute(insert(tabela), linhas[i:i + LOTE])


# ---------------- Deputados ----------------

def _foto(codigo):
    return f"https://www.camara.leg.br/internet/deputado/bandep/{codigo}.jpg"


def parse_deputado(d: dict) -> dict:
    codigo = int(d["id"])
    nome = (d.get("nome") or "").strip()
    return {
        "codigo": codigo, "nome": nome, "nome_norm": norm_nome(nome),
        "partido": norm_partido(d.get("siglaPartido")) or None,
        "uf": d.get("siglaUf") or None,
        "foto": d.get("urlFoto") or _foto(codigo),
        "email": d.get("email") or None,
    }


def coletar_deputados(conn=None, docs=None):
    """docs: (lista_legislatura, lista_atual) — usado nos testes; em produção busca na API."""
    if docs is None:
        leg = paginado(f"/deputados?idLegislatura={LEGISLATURA}&itens=100&ordem=ASC&ordenarPor=nome")
        atual = paginado("/deputados?itens=100&ordem=ASC&ordenarPor=nome")
    else:
        leg, atual = docs

    registros = {}
    for d in leg or []:
        r = parse_deputado(d)
        r["em_exercicio"] = False
        registros[r["codigo"]] = r
    for d in atual or []:
        r = parse_deputado(d)
        r["em_exercicio"] = True
        anterior = registros.get(r["codigo"], {})
        # dados atuais têm prioridade, mas não apagam o que só a outra lista traz
        registros[r["codigo"]] = {**anterior, **{k: v for k, v in r.items() if v is not None}}

    def _gravar(c):
        existentes = {r[0] for r in c.execute(select(deputados.c.codigo))}
        for r in registros.values():
            if r["codigo"] in existentes:
                c.execute(update(deputados).where(deputados.c.codigo == r["codigo"]).values(**r))
            else:
                c.execute(insert(deputados).values(**r))
        atuais = [k for k, v in registros.items() if v.get("em_exercicio")]
        if atuais:  # quem saiu de exercício precisa ser desmarcado
            c.execute(update(deputados).where(deputados.c.codigo.notin_(atuais)).values(em_exercicio=False))

    if not registros:
        raise RuntimeError("API da Câmara não devolveu deputados")
    if conn is None:
        with engine.begin() as c:
            _gravar(c)
    else:
        _gravar(conn)
    log.info("deputados gravados: %d", len(registros))
    return len(registros)


# ---------------- Bancadas: a quem vale cada orientação ----------------
# Rótulos de votacoesOrientacoes: partidos (às vezes truncados em 10 letras: "Republican",
# "Solidaried"), federações ("Fdr PT-PCdoB-PV") e blocos com nome abreviado ("Bl MdbPsdRepPode").
# Composição dos blocos da 57ª legislatura conforme publicados pela Câmara (/blocos).
_FED_PT = {"PT", "PCdoB", "PV"}
_FED_PSDB = {"PSDB", "CIDADANIA"}
BANCADAS = {
    "Fdr PT-PCdoB-PV": _FED_PT, "Fdr PSOL-REDE": {"PSOL", "REDE"}, "Fdr PSDB-CIDADANIA": _FED_PSDB,
    "Bl MdbPsdRepPode": {"MDB", "PSD", "REPUBLICANOS", "PODE"},
    "Bl MdbPsdRepPodePsc": {"MDB", "PSD", "REPUBLICANOS", "PODE", "PSC"},
    "Bl UniPpFdrPsdbCid...": {"UNIÃO", "PP"} | _FED_PSDB,
    "Bl UniPpPsd...": {"UNIÃO", "PP", "PSD", "REPUBLICANOS", "MDB", "PODE"} | _FED_PSDB,
    "Bl AvanSolidPrd...": {"AVANTE", "SOLIDARIEDADE", "PRD"},
}
# abreviações usadas nos nomes de bloco
_ABREV = {"MDB": "MDB", "PSD": "PSD", "REP": "REPUBLICANOS", "REPUBLICANOS": "REPUBLICANOS",
          "PODE": "PODE", "PODEMOS": "PODE", "PSC": "PSC", "UNI": "UNIÃO", "UNIAO": "UNIÃO", "PP": "PP",
          "PDT": "PDT", "PSB": "PSB", "AVAN": "AVANTE", "AVANTE": "AVANTE", "SOLID": "SOLIDARIEDADE",
          "SOLIDARIEDADE": "SOLIDARIEDADE", "PRD": "PRD", "PATRI": "PATRIOTA", "PATRIOTA": "PATRIOTA",
          "PSDB": "PSDB", "CID": "CIDADANIA", "CIDADANIA": "CIDADANIA", "PL": "PL", "NOVO": "NOVO",
          "PT": "PT", "PV": "PV", "PCDOB": "PCdoB", "PSOL": "PSOL", "REDE": "REDE", "PROS": "PROS",
          "PTB": "PTB", "AGIR": "AGIR", "DC": "DC", "PMB": "PMB", "MISSAO": "MISSÃO"}


def membros_bancada(rotulo: str, partidos: set):
    """Partidos que uma orientação alcança (None para Governo/Oposição/Maioria/Minoria ou desconhecido).
    Devolve (membros, completo): completo=False quando o nome do bloco vem truncado ("...")."""
    r = (rotulo or "").strip()
    if not r or r in INSTITUCIONAIS:
        return None, True
    if r in BANCADAS:
        return set(BANCADAS[r]), True
    if r.startswith("Fdr"):
        return {norm_partido(x) for x in re.split(r"[-/ ]+", r[3:]) if x.strip()}, True
    if r.startswith("Bl"):
        corpo = r[2:].strip()
        truncado = corpo.endswith("...") or corpo.endswith("…")
        corpo = corpo.rstrip(".…").replace("PCdoB", "Pcdob").replace("Fdr", "")
        achados = set()
        for tok in re.findall(r"[A-ZÀ-Ú][a-zà-ú]*", corpo):
            p = _ABREV.get(sem_acento(tok).upper())
            if p:
                achados.add(p)
        return (achados or None), not truncado
    p = norm_partido(r)
    if p in partidos:
        return {p}, True
    chave = sem_acento(r).upper()
    if len(chave) >= 3:  # rótulo truncado: "Republican" -> REPUBLICANOS
        cands = [x for x in partidos if sem_acento(x).upper().startswith(chave)]
        if len(cands) == 1:
            return {cands[0]}, True
    return {p}, True


def orientacoes_por_partido(oris: dict, partidos_por_votacao: dict):
    """oris: {votacao: {rotulo: voto}}; partidos_por_votacao: {votacao: set(partidos que votaram)}.
    Para cada partido, vale a orientação da bancada mais específica (partido < federação < bloco).
    Blocos de nome truncado são completados por dedução: em votações com um único bloco
    incompleto, partidos sem nenhuma outra orientação que os cubra só podem estar nele."""
    todos = set().union(*partidos_por_votacao.values()) if partidos_por_votacao else set()
    resolvidos, incompletos = {}, set()
    for labels in oris.values():
        for rot in labels:
            if rot not in resolvidos:
                m, completo = membros_bancada(rot, todos)
                resolvidos[rot] = m or set()
                if not completo or (m is None and rot.startswith("Bl")):
                    incompletos.add(rot)

    faltando, total = defaultdict(Counter), Counter()
    for vid, labels in oris.items():
        desc = [l for l in labels if l in incompletos]
        if len(desc) != 1:
            continue
        cobertos = set().union(*(resolvidos[l] for l in labels if l not in incompletos)) if labels else set()
        total[desc[0]] += 1
        for p in partidos_por_votacao.get(vid, set()) - cobertos - resolvidos[desc[0]]:
            faltando[desc[0]][p] += 1
    for rot, cont in faltando.items():
        extras = {p for p, n in cont.items() if n >= 0.8 * total[rot]}
        if extras:
            resolvidos[rot] = resolvidos[rot] | extras
            log.info("bloco %r: membros deduzidos %s", rot, sorted(extras))

    saida = []
    for vid, labels in oris.items():
        for partido in partidos_por_votacao.get(vid, set()):
            cands = [(len(resolvidos[l]), l) for l in labels if partido in resolvidos.get(l, set())]
            if cands:
                rot = min(cands)[1]
                saida.append({"votacao_id": vid, "partido": partido, "voto": labels[rot], "fonte": rot[:80]})
    return saida


# ---------------- Votações + votos + orientações ----------------

_TIPOS_PRINCIPAIS = ("PEC", "PLP", "PL", "MPV", "PLV", "PDL", "PRC", "PLN")


def _ident_proposicao(r: dict):
    titulo = (r.get("proposicao_titulo") or "").strip()            # "PL 1087/2025"
    sigla = (r.get("proposicao_siglaTipo") or "").strip()
    numero, ano = _int(r.get("proposicao_numero")), _int(r.get("proposicao_ano"))
    m = re.match(r"([A-Za-z]+)\s+(\d+)\s*/\s*(\d{4})", titulo)
    if m:
        sigla = sigla or m.group(1).upper()
        numero = numero or int(m.group(2))
        ano = ano or int(m.group(3))
    return sigla.upper() or None, numero, ano


def _sem_alvo(titulo: str) -> str:
    """'DTQ 3 => PEC 45/2019' -> 'DTQ 3' (o alvo já aparece como matéria principal)."""
    return re.split(r"\s*=>\s*", titulo or "", maxsplit=1)[0].strip()


def _texto_objeto(r: dict) -> str:
    titulo = _sem_alvo((r.get("proposicao_titulo") or "").strip())
    ementa = (r.get("proposicao_ementa") or r.get("descricao") or "").strip()
    return " — ".join(x for x in (titulo, ementa) if x)


def gravar_votacoes_ano(conn, ano: int, linhas_votacoes, linhas_votos, linhas_orient, linhas_props=(),
                        linhas_objetos=(), parcial=False):
    """Substitui as votações nominais do Plenário de um ano (os arquivos são revisados todo dia).
    parcial=True: substitui só as votações presentes nas linhas (atualização ao vivo de um período),
    sem apagar as demais do ano."""
    vts = {}
    for r in linhas_votacoes:
        if ORGAO_VOTACOES and (r.get("siglaOrgao") or "").upper() != ORGAO_VOTACOES:
            continue
        vid = (r.get("id") or "").strip()
        if not vid:
            continue
        aprov = (r.get("aprovacao") or "").strip()
        vts[vid] = {
            "id": vid, "ano": ano, "data": _dt(r.get("dataHoraRegistro")) or _dt(r.get("data")),
            "sigla_materia": None, "numero_materia": None, "ano_materia": None, "proposicao_id": None,
            "descricao_materia": None, "descricao_votacao": (r.get("descricao") or "").strip() or None,
            "dispositivo": None, "sessao": None, "presidente": None,
            "qtd_sim": _int(r.get("votosSim")), "qtd_nao": _int(r.get("votosNao")),
            "qtd_outros": _int(r.get("votosOutros")), "qtd_abstencao": None,
            "aprovacao": (aprov == "1") if aprov in ("0", "1") else None, "secreta": False,
            "_prop_alt": _int(r.get("ultimaApresentacaoProposicao_idProposicao")),
            "etapa": None,
            "_abertura": (r.get("ultimaAberturaVotacao_descricao") or "").strip(),
            "_apresentacao": (r.get("ultimaApresentacaoProposicao_descricao") or "").strip(),
        }

    lista_votos, partidos, presidentes = [], defaultdict(set), {}
    for r in linhas_votos:
        vid = (r.get("idVotacao") or "").strip()
        if vid not in vts:
            continue
        cod = _int(r.get("deputado_id"))
        if cod is None:
            continue
        voto = norm_voto(r.get("voto"))
        partido = norm_partido(r.get("deputado_siglaPartido"))
        lista_votos.append({"votacao_id": vid, "codigo_deputado": cod,
                            "nome": (r.get("deputado_nome") or "").strip(),
                            "partido": partido, "uf": r.get("deputado_siglaUf") or None, "voto": voto})
        if partido:
            partidos[vid].add(partido)
        if voto == "ART. 17":
            presidentes[vid] = (r.get("deputado_nome") or "").strip()
    # as nominais vão para `votacoes`; as simbólicas (sem voto individual) só para `votacoes_simbolicas`,
    # usadas nos relatórios — separadas no fim, depois de achar a matéria e o objeto de cada uma
    ids_com_voto = {x["votacao_id"] for x in lista_votos}
    # um deputado só pode ter um voto por votação (o arquivo às vezes repete a linha)
    unicos = {(x["votacao_id"], x["codigo_deputado"]): x for x in lista_votos if x["votacao_id"] in vts}
    lista_votos = list(unicos.values())
    abst = Counter(x["votacao_id"] for x in lista_votos if x["voto"] == "ABSTENÇÃO")
    for vid, v in vts.items():
        v["qtd_abstencao"] = abst.get(vid, 0)
        v["presidente"] = presidentes.get(vid)

    oris = defaultdict(dict)
    for r in linhas_orient:
        vid = (r.get("idVotacao") or "").strip()
        if vid not in vts:
            continue
        rot = norm_lideranca(r.get("siglaBancada"))
        voto = norm_voto(r.get("orientacao"))
        if rot and voto:
            oris[vid][rot[:80]] = voto

    props = defaultdict(list)
    for r in linhas_props:
        vid = (r.get("idVotacao") or "").strip()
        if vid in vts:
            props[vid].append(r)
    for vid, lista in props.items():
        def _peso(r):
            s = _ident_proposicao(r)[0] or ""
            return _TIPOS_PRINCIPAIS.index(s) if s in _TIPOS_PRINCIPAIS else 99
        principal = min(lista, key=_peso)
        sigla, numero, ano_m = _ident_proposicao(principal)
        v = vts[vid]
        v.update(sigla_materia=sigla, numero_materia=numero, ano_materia=ano_m,
                 proposicao_id=_int(principal.get("proposicao_id")),
                 descricao_materia=(principal.get("proposicao_ementa") or "").strip() or None)
        outras = set()
        for r in lista:
            s, n, a = _ident_proposicao(r)
            if r is not principal and s and (s, n, a) != (sigla, numero, ano_m):
                outras.add(f"{s} {n}/{a}" if n else s)
        outras = sorted(outras)
        if outras:
            v["dispositivo"] = ", ".join(outras)[:300]
        acess = [r for r in lista if r is not principal and _ident_proposicao(r) != (sigla, numero, ano_m)]
        if acess:   # requerimento, destaque, emenda... votado "sobre" a matéria principal
            v["_acessoria"] = _texto_objeto(acess[0])

    # o objeto da votação (arquivo votacoesObjetos): diz se foi requerimento, destaque, emenda, turno...
    objetos = defaultdict(list)
    for r in linhas_objetos:
        vid = (r.get("idVotacao") or "").strip()
        if vid in vts:
            objetos[vid].append(r)
    for vid, v in vts.items():
        principal = (v["sigla_materia"], v["numero_materia"], v["ano_materia"])
        objs = [r for r in objetos.get(vid, ())
                if _ident_proposicao(r) != principal or "=>" in (r.get("proposicao_titulo") or "")]
        candidatos = [_texto_objeto(objs[0]) if objs else "", v.get("_acessoria") or "", v["_abertura"],
                      v["_apresentacao"] if v["_prop_alt"] != v["proposicao_id"] else ""]
        v["etapa"] = next((c for c in candidatos if c), None)
        if v["etapa"]:
            v["etapa"] = v["etapa"][:600]

    simb = {k: v for k, v in vts.items() if k not in ids_com_voto}
    vts = {k: v for k, v in vts.items() if k in ids_com_voto}
    oris = {k: d for k, d in oris.items() if k in vts}
    _gravar_simbolicas(conn, ano, simb, parcial=parcial)
    if parcial:   # uma votação que antes veio sem votos (simbólica) e agora tem votos, ou o contrário
        ids_nom = list(vts)
        for i in range(0, len(ids_nom), 900):
            conn.execute(delete(votacoes_simbolicas).where(votacoes_simbolicas.c.id.in_(ids_nom[i:i + 900])))
    linhas_ori = [{"votacao_id": vid, "lideranca": rot, "voto": voto}
                  for vid, d in oris.items() for rot, voto in d.items()]
    linhas_op = orientacoes_por_partido(oris, partidos)

    if not parcial:
        antigos = select(votacoes.c.id).where(votacoes.c.ano == ano)
        conn.execute(delete(votos).where(votos.c.votacao_id.in_(antigos)))
        conn.execute(delete(orientacoes).where(orientacoes.c.votacao_id.in_(antigos)))
        conn.execute(delete(orientacoes_partido).where(orientacoes_partido.c.votacao_id.in_(antigos)))
    novos = list(vts) + (list(simb) if parcial else [])
    for i in range(0, len(novos), 900):  # votações que mudaram de ano no arquivo
        parte = novos[i:i + 900]
        conn.execute(delete(votos).where(votos.c.votacao_id.in_(parte)))
        conn.execute(delete(orientacoes).where(orientacoes.c.votacao_id.in_(parte)))
        conn.execute(delete(orientacoes_partido).where(orientacoes_partido.c.votacao_id.in_(parte)))
        conn.execute(delete(votacoes).where(votacoes.c.id.in_(parte)))
    if not parcial:
        conn.execute(delete(votacoes).where(votacoes.c.ano == ano))
    _lote(conn, votacoes, [{k: v for k, v in x.items() if not k.startswith("_")} for x in vts.values()])
    _lote(conn, votos, lista_votos)
    _lote(conn, orientacoes, linhas_ori)
    _lote(conn, orientacoes_partido, linhas_op)
    log.info("votações %s: %d nominais, %d votos, %d orientações", ano, len(vts), len(lista_votos), len(linhas_ori))
    return len(vts)


_COLS_SIMB = ("id", "ano", "data", "sigla_materia", "numero_materia", "ano_materia", "proposicao_id",
              "descricao_materia", "descricao_votacao", "aprovacao", "etapa")


def _plano_txt(s):
    return " ".join(sem_acento(s or "").lower().split())


def _materia_da_simbolica(conn, simb, buscar_api=True, limite_api=400):
    """Muitas simbólicas (aprovação de MPV, redação final, urgência...) não vêm ligadas a nenhuma proposição
    no arquivo. O id da votação começa pelo id da proposição votada ("2643429-30" -> MPV 1383/2026):
    usa esse id e acha sigla/número/ano no cadastro, no que já estava gravado ou, por último, na API."""
    for v in simb.values():
        # vale o id do começo da votação: a ligação do arquivo costuma apontar para o requerimento, a emenda
        # ou o parecer ("Aprovado o Requerimento." do PLP 299/2026 vinha ligado ao REQ e era descartado)
        pref = _int(str(v["id"]).split("-")[0])
        if pref and v.get("proposicao_id") != pref:
            v.update(proposicao_id=pref, sigla_materia=None, numero_materia=None, ano_materia=None,
                     descricao_materia=None)
    falta = {v["proposicao_id"] for v in simb.values() if v.get("proposicao_id") and not v.get("sigla_materia")}
    if not falta:
        return
    conhecidas = {}
    lst = sorted(falta)
    for i in range(0, len(lst), 900):
        parte = lst[i:i + 900]
        for r in conn.execute(select(processos.c.id, processos.c.sigla, processos.c.numero, processos.c.ano,
                                     processos.c.ementa).where(processos.c.id.in_(parte))):
            conhecidas[r[0]] = (r[1], _int(r[2]), r[3], r[4])
        for r in conn.execute(select(votacoes_simbolicas.c.proposicao_id, votacoes_simbolicas.c.sigla_materia,
                                     votacoes_simbolicas.c.numero_materia, votacoes_simbolicas.c.ano_materia,
                                     votacoes_simbolicas.c.descricao_materia)
                              .where(votacoes_simbolicas.c.proposicao_id.in_(parte),
                                     votacoes_simbolicas.c.sigla_materia.is_not(None))):
            conhecidas.setdefault(r[0], (r[1], r[2], r[3], r[4]))
    pedidos = 0
    for pid in lst:
        if pid in conhecidas or not buscar_api or pedidos >= limite_api:
            continue
        pedidos += 1
        try:
            dd = (get_json(f"/proposicoes/{pid}") or {}).get("dados") or {}
        except Exception as e:
            log.warning("proposição %s (votação simbólica): %s", pid, e)
            continue
        if dd.get("siglaTipo"):
            conhecidas[pid] = (dd["siglaTipo"], _int(dd.get("numero")), _int(dd.get("ano")), dd.get("ementa"))
    for v in simb.values():
        k = conhecidas.get(v.get("proposicao_id"))
        if k and not v.get("sigla_materia"):
            v.update(sigla_materia=k[0], numero_materia=k[1], ano_materia=k[2],
                     descricao_materia=v.get("descricao_materia") or k[3])
    if pedidos:
        log.info("votações simbólicas: %d matéria(s) identificada(s) pela API", pedidos)


def _gravar_simbolicas(conn, ano, simb, parcial=False):
    """Substitui as votações simbólicas do Plenário do ano (só as ligadas a uma matéria).
    parcial=True: substitui só as simbólicas recebidas."""
    _materia_da_simbolica(conn, simb, buscar_api=VOTACOES_API)
    linhas = [{k: v.get(k) for k in _COLS_SIMB} for v in simb.values()
              if (v.get("proposicao_id") or v.get("sigla_materia"))
              and (v.get("sigla_materia") or "").upper() != "REQ"        # repetem a votação do lado da matéria
              and not _plano_txt(v.get("descricao_votacao")).startswith("alteracao do regime de tramitacao")]
    ids = [x["id"] for x in linhas]
    if parcial:
        ids = list(simb)
    else:
        conn.execute(delete(votacoes_simbolicas).where(votacoes_simbolicas.c.ano == ano))
    for i in range(0, len(ids), 900):
        conn.execute(delete(votacoes_simbolicas).where(votacoes_simbolicas.c.id.in_(ids[i:i + 900])))
    _lote(conn, votacoes_simbolicas, linhas)
    if linhas:
        log.info("votações %s: %d simbólicas", ano, len(linhas))


def _anos_pendentes(chave):
    with engine.connect() as c:
        v = c.execute(select(meta.c.valor).where(meta.c.chave == chave)).scalar()
    return {int(x) for x in (v or "").split(",") if x.strip().isdigit()}


def _salvar_pendentes(chave, anos):
    with engine.begin() as c:
        c.execute(delete(meta).where(meta.c.chave == chave))
        if anos:
            c.execute(insert(meta).values(chave=chave, valor=",".join(str(a) for a in sorted(anos))))


# O arquivo anual da Câmara às vezes fica semanas sem ser regenerado (em nov/2026 parou em 14/10),
# enquanto a API já tem as votações novas. A coleta completa o arquivo com a API: votações do
# Plenário posteriores à última data do arquivo, com votos, orientações e objetos votados.
VOTACOES_API = os.environ.get("VOTACOES_API", "1") != "0"
_NOMES_VOT = ("votacoes", "votacoesVotos", "votacoesOrientacoes", "votacoesProposicoes", "votacoesObjetos")


def _data_linha(r):
    return _dt(r.get("dataHoraRegistro")) or _dt(r.get("data"))


def ultima_data_arquivo(linhas_votacoes):
    """Data da votação do Plenário mais recente no arquivo anual (None se não houver)."""
    datas = [_data_linha(r) for r in linhas_votacoes
             if not ORGAO_VOTACOES or (r.get("siglaOrgao") or "").upper() == ORGAO_VOTACOES]
    datas = [d for d in datas if d]
    return max(datas).date() if datas else None


def _linha_prop(vid, p):
    """Proposição da API (objetosPossiveis/proposicoesAfetadas) no formato das linhas do arquivo."""
    sigla, numero, ano = (p.get("siglaTipo") or "").strip(), p.get("numero"), p.get("ano")
    titulo = f"{sigla} {numero}/{ano}" if sigla and numero and ano else sigla
    return {"idVotacao": vid, "proposicao_id": str(p.get("id") or ""), "proposicao_ementa": p.get("ementa") or "",
            "proposicao_siglaTipo": sigla, "proposicao_numero": str(numero or ""),
            "proposicao_ano": str(ano or ""), "proposicao_titulo": titulo}


def _linhas_orientacoes_api(vid):
    saida = []
    for o in (get_json(f"/votacoes/{vid}/orientacoes") or {}).get("dados") or []:
        rot = (o.get("siglaPartidoBloco") or o.get("siglaBancada") or "").strip()
        if not rot and (o.get("codTipoLideranca") or "").upper() == "G":
            rot = "Governo"
        if rot:
            saida.append({"idVotacao": vid, "siglaBancada": rot,
                          "orientacao": o.get("orientacaoVoto") or o.get("orientacao") or ""})
    return saida


def votacoes_pela_api(ano, desde, ja=(), hoje=None):
    """Votações nominais do Plenário de `desde` até hoje, lidas da API e devolvidas no mesmo formato
    das linhas dos arquivos anuais (para passar por gravar_votacoes_ano). Falha da API propaga."""
    hoje = hoje or date.today()
    ini, fim = max(desde, date(ano, 1, 1)), min(hoje, date(ano, 12, 31))
    out = {n: [] for n in _NOMES_VOT}
    lista, d = [], ini
    while d <= fim:   # janelas de 30 dias (a API limita o intervalo de datas)
        f = min(fim, d + timedelta(days=29))
        lista += paginado(f"/votacoes?dataInicio={d}&dataFim={f}&itens=200&ordem=ASC&ordenarPor=dataHoraRegistro")
        d = f + timedelta(days=1)
    vistos = set(ja)
    for v in lista:
        vid = str(v.get("id") or "").strip()
        if not vid or vid in vistos:
            continue
        if ORGAO_VOTACOES and (v.get("siglaOrgao") or "").upper() != ORGAO_VOTACOES:
            continue
        vistos.add(vid)
        votos_ = paginado(f"/votacoes/{vid}/votos")
        det = (get_json(f"/votacoes/{vid}") or {}).get("dados") or {}
        cont = Counter()
        for x in votos_:
            dep = x.get("deputado_") or x.get("deputado") or {}
            voto = x.get("tipoVoto") or x.get("voto") or ""
            n = norm_voto(voto)
            cont["sim" if n == "SIM" else "nao" if n == "NÃO" else "outros"] += 1
            out["votacoesVotos"].append({
                "idVotacao": vid, "voto": voto, "deputado_id": str(dep.get("id") or ""),
                "deputado_nome": dep.get("nome") or "", "deputado_siglaPartido": dep.get("siglaPartido") or "",
                "deputado_siglaUf": dep.get("siglaUf") or ""})
        aprov = det.get("aprovacao", v.get("aprovacao"))
        abertura = det.get("ultimaAberturaVotacao") or {}
        apres = det.get("ultimaApresentacaoProposicao") or {}
        out["votacoes"].append({
            "id": vid, "siglaOrgao": v.get("siglaOrgao") or det.get("siglaOrgao") or "",
            "data": det.get("data") or v.get("data") or "",
            "dataHoraRegistro": det.get("dataHoraRegistro") or v.get("dataHoraRegistro") or "",
            "aprovacao": "" if aprov in (None, "") else str(int(aprov)),
            "votosSim": str(cont["sim"]), "votosNao": str(cont["nao"]), "votosOutros": str(cont["outros"]),
            "descricao": det.get("descricao") or v.get("descricao") or "",
            "ultimaAberturaVotacao_descricao": abertura.get("descricao") or "",
            "ultimaApresentacaoProposicao_descricao": apres.get("descricao") or "",
            "ultimaApresentacaoProposicao_idProposicao": str(id_da_uri(apres.get("uriProposicaoCitada")) or ""),
        })
        out["votacoesOrientacoes"] += _linhas_orientacoes_api(vid)
        objetos = det.get("objetosPossiveis") or []
        afetadas = det.get("proposicoesAfetadas") or []
        ids = set()
        for p in afetadas + objetos:
            if p.get("id") not in ids:
                ids.add(p.get("id"))
                out["votacoesProposicoes"].append(_linha_prop(vid, p))
        out["votacoesObjetos"] += [_linha_prop(vid, p) for p in objetos]
    return out


def _orientacoes_guardadas(conn, ano):
    """Orientações já gravadas do ano, no formato do arquivo (usadas quando o arquivo vem vazio)."""
    q = (select(orientacoes.c.votacao_id, orientacoes.c.lideranca, orientacoes.c.voto)
         .where(orientacoes.c.votacao_id.in_(select(votacoes.c.id).where(votacoes.c.ano == ano))))
    return [{"idVotacao": vid, "siglaBancada": lid, "orientacao": voto} for vid, lid, voto in conn.execute(q)]


def coletar_votacoes(anos) -> int:
    """Um ano que falhar não interrompe os seguintes: fica anotado como pendente e é refeito
    automaticamente na próxima coleta."""
    chave = "votacoes_anos_pendentes"
    pendentes = _anos_pendentes(chave)
    alvo = sorted(set(anos) | pendentes)
    total, falhas = 0, set()
    hoje = date.today()
    for ano in alvo:
        arqs = {}
        try:
            for nome in ("votacoes", "votacoesVotos", "votacoesOrientacoes", "votacoesProposicoes"):
                arqs[nome] = baixar(arquivo_anual(nome, ano))
            try:   # opcional: só enriquece a etapa da votação
                arqs["votacoesObjetos"] = baixar(arquivo_anual("votacoesObjetos", ano))
            except Exception as e:
                log.warning("votacoesObjetos %s indisponível (%s)", ano, e)
                arqs["votacoesObjetos"] = None
            linhas = {n: list(ler_csv(arqs[n])) if arqs.get(n) else [] for n in _NOMES_VOT}
            if not arqs["votacoes"] or not arqs["votacoesVotos"]:
                linhas["votacoes"], linhas["votacoesVotos"] = [], []
            ultima = ultima_data_arquivo(linhas["votacoes"])
            com_voto = {(r.get("idVotacao") or "").strip() for r in linhas["votacoesVotos"]}
            if VOTACOES_API and ano <= hoje.year and (ultima is None or ultima < min(hoje, date(ano, 12, 31))):
                # a partir do próprio dia da última votação do arquivo (a sessão pode ter seguido)
                api = votacoes_pela_api(ano, ultima or date(ano, 1, 1), ja=com_voto, hoje=hoje)
                if api["votacoes"]:
                    log.info("votações %s: arquivo vai até %s; %d votação(ões) nominal(is) completada(s) pela API",
                             ano, ultima or "—", len(api["votacoes"]))
                api_ori = api.pop("votacoesOrientacoes")
                for n, rows in api.items():
                    linhas[n] += rows
            else:
                api_ori = []
            if not linhas["votacoes"]:
                log.info("votações %s: arquivo ainda não publicado", ano)
                continue
            with engine.begin() as c:
                if not linhas["votacoesOrientacoes"] and com_voto:
                    # arquivo de orientações vazio (aconteceu em 23/11/2026): não apaga as que já temos
                    guardadas = _orientacoes_guardadas(c, ano)
                    log.warning("votações %s: arquivo de orientações vazio — mantidas %d orientações já gravadas",
                                ano, len(guardadas))
                    linhas["votacoesOrientacoes"] = guardadas
                    if not guardadas and VOTACOES_API:
                        linhas["votacoesOrientacoes"] = _orientacoes_faltantes_api(linhas)
                linhas["votacoesOrientacoes"] += api_ori
                total += gravar_votacoes_ano(c, ano, linhas["votacoes"], linhas["votacoesVotos"],
                                             linhas["votacoesOrientacoes"], linhas["votacoesProposicoes"],
                                             linhas["votacoesObjetos"])
        except Exception as e:
            log.error("votações de %s não carregadas (%s) — ficam para a próxima coleta", ano, e)
            falhas.add(ano)
        finally:
            _apagar(*arqs.values())
    _salvar_pendentes(chave, (pendentes - set(alvo)) | falhas)
    return total


def _orientacoes_faltantes_api(linhas, limite=400):
    """Sem arquivo e sem nada gravado: busca na API as orientações das votações do Plenário do arquivo."""
    com_voto = {(r.get("idVotacao") or "").strip() for r in linhas["votacoesVotos"]}
    ids = [(r.get("id") or "").strip() for r in linhas["votacoes"]
           if (not ORGAO_VOTACOES or (r.get("siglaOrgao") or "").upper() == ORGAO_VOTACOES)
           and (r.get("id") or "").strip() in com_voto][:limite]
    saida = []
    for vid in ids:
        try:
            saida += _linhas_orientacoes_api(vid)
        except Exception as e:
            log.warning("orientações de %s pela API: %s", vid, e)
    log.info("orientações recuperadas pela API: %d (%d votações)", len(saida), len(ids))
    return saida


def _ids_completos(conn, ini, fim):
    """Votações nominais do período já gravadas com votos e orientações: não precisam ser relidas."""
    q = (select(votacoes.c.id)
         .where(votacoes.c.data >= datetime.combine(ini, datetime.min.time()),
                votacoes.c.data < datetime.combine(fim + timedelta(days=1), datetime.min.time()),
                votacoes.c.id.in_(select(votos.c.votacao_id)),
                votacoes.c.id.in_(select(orientacoes.c.votacao_id))))
    return {r[0] for r in conn.execute(q)}


def atualizar_votacoes_periodo(inicio, fim, hoje=None) -> int:
    """Atualização ao vivo: lê na API as votações do Plenário de `inicio` a `fim` e grava só elas,
    sem esperar a coleta diária nem o arquivo anual. As já completas (com votos e orientações) não são
    relidas; as simbólicas e as que ainda vieram sem orientação são conferidas de novo.
    Devolve quantas votações (nominais ou simbólicas) entraram ou mudaram."""
    hoje = hoje or date.today()
    fim = min(fim, hoje)
    total = 0
    for ano in range(inicio.year, fim.year + 1):
        ini_a, fim_a = max(inicio, date(ano, 1, 1)), min(fim, date(ano, 12, 31))
        if ini_a > fim_a:
            continue
        with engine.connect() as c:
            ja = _ids_completos(c, ini_a, fim_a)
        api = votacoes_pela_api(ano, ini_a, ja=ja, hoje=fim_a)
        if not api["votacoes"]:
            continue
        with engine.begin() as c:
            antes = _retrato_periodo(c, ini_a, fim_a)
            gravar_votacoes_ano(c, ano, api["votacoes"], api["votacoesVotos"], api["votacoesOrientacoes"],
                                api["votacoesProposicoes"], api["votacoesObjetos"], parcial=True)
            depois = _retrato_periodo(c, ini_a, fim_a)
        total += sum(1 for k, v in depois.items() if antes.get(k) != v)
        log.info("votações ao vivo %s a %s: %d lida(s) da API", ini_a, fim_a, len(api["votacoes"]))
    # simbólicas que a Câmara ainda não publicou em /votacoes, achadas na tramitação das matérias da pauta
    try:
        import simbolicas_provisorias
        total += simbolicas_provisorias.atualizar(inicio, fim)
    except Exception as e:
        log.warning("simbólicas provisórias %s a %s: %s", inicio, fim, e)
    return total


def _retrato_periodo(conn, ini, fim):
    """{id: placar/orientações} das votações nominais e simbólicas do período, para saber o que mudou."""
    a, b = datetime.combine(ini, datetime.min.time()), datetime.combine(fim + timedelta(days=1), datetime.min.time())
    n_ori = select(orientacoes.c.votacao_id, func.count().label("n")).group_by(orientacoes.c.votacao_id).subquery()
    out = {r[0]: ("N", r[1], r[2], r[3]) for r in conn.execute(
        select(votacoes.c.id, votacoes.c.qtd_sim, votacoes.c.qtd_nao, n_ori.c.n)
        .outerjoin(n_ori, n_ori.c.votacao_id == votacoes.c.id)
        .where(votacoes.c.data >= a, votacoes.c.data < b))}
    out.update({r[0]: ("S", r[1], r[2]) for r in conn.execute(
        select(votacoes_simbolicas.c.id, votacoes_simbolicas.c.sigla_materia, votacoes_simbolicas.c.aprovacao)
        .where(votacoes_simbolicas.c.data >= a, votacoes_simbolicas.c.data < b))})
    return out


# ---------------- CEAP (cota parlamentar) ----------------

_TIPO_DOC = {"0": "Nota fiscal", "1": "Recibo", "2": "Despesa no exterior", "3": "Despesa no exterior",
             "4": "Nota fiscal eletrônica"}


def parse_ceap_csv(linhas, cadastro=None):
    """Arquivo anual da Câmara (Ano-AAAA.csv). ideCadastro = id do deputado na API.
    Linhas sem deputado (lideranças, gabinetes institucionais) e de outra legislatura ficam de fora."""
    out = []
    for r in linhas:
        cod = _int(r.get("ideCadastro"))
        if not cod:
            continue
        leg = _int(r.get("codLegislatura"))
        if leg and leg != LEGISLATURA:
            continue
        if cadastro is not None and cod not in cadastro:
            continue
        det = " · ".join(x for x in (r.get("txtDescricaoEspecificacao"), r.get("txtPassageiro"),
                                     r.get("txtTrecho")) if x)
        out.append({
            "ano": _int(r.get("numAno")), "mes": _int(r.get("numMes")),
            "data": (r.get("datEmissao") or "")[:10] or None,
            "codigo_deputado": cod, "nome": (r.get("txNomeParlamentar") or "").strip(),
            "tipo": (r.get("txtDescricao") or "").strip().rstrip("."),
            "tipo_documento": _TIPO_DOC.get(str(r.get("indTipoDocumento") or "").strip()),
            "fornecedor": (r.get("txtFornecedor") or "").strip(), "cnpj_cpf": r.get("txtCNPJCPF") or None,
            "documento": r.get("txtNumero") or None, "detalhamento": det or None,
            "valor": _num(r.get("vlrLiquido")), "url": (r.get("urlDocumento") or None),
        })
    return out


def parse_ceap_api(codigo: int, nome: str, itens):
    """Plano B: /deputados/{id}/despesas."""
    out = []
    for d in itens or []:
        out.append({
            "ano": _int(d.get("ano")), "mes": _int(d.get("mes")),
            "data": (d.get("dataDocumento") or "")[:10] or None, "codigo_deputado": codigo, "nome": nome,
            "tipo": (d.get("tipoDespesa") or "").strip().rstrip("."), "tipo_documento": d.get("tipoDocumento"),
            "fornecedor": (d.get("nomeFornecedor") or "").strip(), "cnpj_cpf": d.get("cnpjCpfFornecedor"),
            "documento": d.get("numDocumento"), "detalhamento": None,
            "valor": _num(d.get("valorLiquido")), "url": d.get("urlDocumento"),
        })
    return out


def gravar_ceaps(conn, ano: int, linhas):
    """Substitui o ano inteiro (a Câmara revisa lançamentos retroativamente)."""
    conn.execute(delete(despesas).where(despesas.c.ano == ano))
    linhas = [r for r in linhas if r["ano"] == ano]
    _lote(conn, despesas, linhas)
    return len(linhas)


def coletar_ceaps(anos):
    with engine.connect() as c:
        cad = dict(c.execute(select(deputados.c.codigo, deputados.c.nome)).all())
    for ano in anos:
        linhas, caminho = None, None
        try:
            caminho = baixar(CEAP_ZIP.format(ano=ano))
            if caminho:
                linhas = parse_ceap_csv(ler_csv(caminho))
        except Exception as e:
            log.warning("arquivo da CEAP %s falhou (%s); usando a API por deputado", ano, e)
        finally:
            _apagar(caminho)
        if not linhas:
            linhas = []
            for cod, nome in cad.items():
                try:
                    linhas += parse_ceap_api(cod, nome, paginado(f"/deputados/{cod}/despesas?ano={ano}&itens=100"))
                except Exception as e:
                    log.warning("despesas do deputado %s em %s: %s", cod, ano, e)
        if linhas:
            with engine.begin() as c:
                n = gravar_ceaps(c, ano, linhas)
            log.info("CEAP %s: %d lançamentos", ano, n)


# ---------------- Proposições ----------------

# Emendas, substitutivos, pareceres e redações ficam de fora: são peças de outras proposições
TIPOS_EXCLUIDOS = set(filter(None, os.environ.get(
    "TIPOS_EXCLUIDOS",
    "EMC,EMP,EMR,EMS,EML,EMA,EMD,EMPV,EMRP,ESB,ESP,SBT,SBE,SSP,SBT-A,PAR,PRL,PPR,PEP,PPP,REL,RDF,ERD,"
    "PRV,CVO,SUM,DTQ,DVT").split(",")))
_ENCERRADA = ("arquivad", "transformad", "norma juridica", "retirad", "vetad", "prejudicad", "rejeitad",
              "devolvid", "perdeu a eficacia", "tramitacao finalizada", "cancelad", "declarad")


def _situacao(r):
    sit = (r.get("ultimoStatus_descricaoSituacao") or "").strip()
    return sit or (r.get("ultimoStatus_descricaoTramitacao") or "").strip() or None


def parse_proposicao(r: dict, ano_carga: int) -> dict:
    pid = int(r["id"])
    sigla, numero, ano = (r.get("siglaTipo") or "").strip().upper(), (r.get("numero") or "").strip(), _int(r.get("ano"))
    sit = _situacao(r)
    s = norm_nome(sit or "")
    norma = "norma juridica" in s or "transformad" in s
    return {
        "id": pid, "ano_carga": ano_carga, "identificacao": f"{sigla} {numero}/{ano}" if sigla else str(pid),
        "sigla": sigla or None, "numero": numero or None, "ano": ano,
        "ementa": (r.get("ementa") or "").strip() or None, "autoria": None,
        "tipo_documento": (r.get("descricaoTipo") or "").strip() or None,
        "tipo_conteudo": (r.get("ultimoStatus_apreciacao") or "").strip() or None,
        "situacao": sit[:200] if sit else None,
        "data_apresentacao": (r.get("dataApresentacao") or "")[:10] or None,
        "data_situacao": (r.get("ultimoStatus_dataHora") or "")[:10] or None,
        "tramitando": (not any(k in s for k in _ENCERRADA)) if sit else None,
        "virou_norma": norma, "url": f"https://www.camara.leg.br/propostas-legislativas/{pid}",
        "regime": (r.get("ultimoStatus_regime") or "").strip()[:120] or None,
        "orgao": (r.get("ultimoStatus_siglaOrgao") or "").strip()[:40] or None,
        "relator_id": id_da_uri(r.get("ultimoStatus_uriRelator")),
    }


def id_da_uri(uri):
    """".../api/v2/deputados/204554" → 204554."""
    m = re.search(r"/(\d+)\s*$", (uri or "").strip())
    return int(m.group(1)) if m else None


DIAS_NOTIFICACOES = 180   # notificações mais antigas são apagadas
DIAS_NOVA = 30            # "proposição nova" só para as apresentadas nos últimos 30 dias


def gravar_processos_ano(conn, ano: int, linhas_props, linhas_autores, cadastro: set, notificar=False,
                         hoje=None):
    """Substitui as proposições apresentadas num ano por deputados do cadastro.

    Com notificar=True (coletas depois da carga inicial), compara com o que estava no banco e
    registra em `notificacoes` cada proposição que mudou de situação e cada proposição nova."""
    autores = defaultdict(list)   # proposição -> [(ordem, nome, codigo)]
    for r in linhas_autores:
        pid = _int(r.get("idProposicao"))
        if pid is None:
            continue
        if (r.get("proponente") or "1").strip() not in ("1", "true", "True"):
            continue  # apoiadores não contam como autores
        cod = _int(r.get("idDeputadoAutor"))
        ordem = _int(r.get("ordemAssinatura")) or 999
        autores[pid].append((ordem, (r.get("nomeAutor") or "").strip(), cod))
    alvo = {pid for pid, lst in autores.items() if any(c in cadastro for _, _, c in lst)}

    linhas, auts = [], []
    for r in linhas_props:
        pid = _int(r.get("id"))
        # entram as de autoria do cadastro e as relatadas agora (relator atual) por deputado do cadastro
        if pid is None or (pid not in alvo and id_da_uri(r.get("ultimoStatus_uriRelator")) not in cadastro):
            continue
        p = parse_proposicao(r, ano)
        if p["sigla"] in TIPOS_EXCLUIDOS:
            continue
        lst = sorted(autores[pid])
        p["autoria"] = ", ".join(n for _, n, _ in lst if n)[:2000] or None
        primeiro = lst[0][0] if lst else None
        linhas.append(p)
        vistos = set()
        for ordem, _, cod in lst:
            if cod in cadastro and cod not in vistos:
                vistos.add(cod)
                auts.append({"processo_id": pid, "codigo_deputado": cod, "principal": ordem == primeiro})

    ids = [p["id"] for p in linhas]
    antes = {}
    if notificar:
        q = select(processos.c.id, processos.c.situacao, processos.c.data_situacao)
        for r in conn.execute(q.where(processos.c.ano_carga == ano)):
            antes[r[0]] = (r[1], r[2])
        for i in range(0, len(ids), 900):
            for r in conn.execute(q.where(processos.c.id.in_(ids[i:i + 900]))):
                antes[r[0]] = (r[1], r[2])

    antigos = select(processos.c.id).where(processos.c.ano_carga == ano)
    conn.execute(delete(autorias).where(autorias.c.processo_id.in_(antigos)))
    conn.execute(delete(processos).where(processos.c.ano_carga == ano))
    for i in range(0, len(ids), 900):
        conn.execute(delete(autorias).where(autorias.c.processo_id.in_(ids[i:i + 900])))
        conn.execute(delete(processos).where(processos.c.id.in_(ids[i:i + 900])))
    _lote(conn, processos, linhas)
    _lote(conn, autorias, auts)
    if notificar:
        n = registrar_notificacoes(conn, linhas, auts, antes, hoje)
        log.info("proposições %s: %d notificações", ano, n)
    log.info("proposições %s: %d (%d autorias)", ano, len(linhas), len(auts))
    return len(linhas)


def registrar_notificacoes(conn, linhas, auts, antes, hoje=None):
    """Mudou a situação (ou a data dela) → avisa cada autor do cadastro; proposição que não
    estava no banco e foi apresentada há pouco → "nova"."""
    hoje = hoje or date.today()
    corte_nova = (hoje - timedelta(days=DIAS_NOVA)).isoformat()
    autores = defaultdict(list)
    for a in auts:
        autores[a["processo_id"]].append(a["codigo_deputado"])
    agora, novas = datetime.now(), []
    for p in linhas:
        if p["id"] in antes:
            sit, dsit = antes[p["id"]]
            if p["situacao"] and ((sit or "") != (p["situacao"] or "") or (dsit or "") != (p["data_situacao"] or "")):
                novas += [{"codigo_deputado": cod, "processo_id": p["id"], "identificacao": p["identificacao"],
                           "tipo": "situacao", "situacao_anterior": sit, "situacao_nova": p["situacao"],
                           "data_situacao": p["data_situacao"], "detectado_em": agora} for cod in autores[p["id"]]]
        elif (p["data_apresentacao"] or "") >= corte_nova:
            novas += [{"codigo_deputado": cod, "processo_id": p["id"], "identificacao": p["identificacao"],
                       "tipo": "nova", "situacao_anterior": None, "situacao_nova": p["situacao"],
                       "data_situacao": p["data_apresentacao"], "detectado_em": agora} for cod in autores[p["id"]]]
    _lote(conn, notificacoes, novas)
    return len(novas)


def limpar_notificacoes(conn, hoje=None):
    limite = datetime.combine((hoje or date.today()) - timedelta(days=DIAS_NOTIFICACOES), datetime.min.time())
    conn.execute(delete(notificacoes).where(notificacoes.c.detectado_em < limite))


def coletar_processos(anos):
    chave = "proposicoes_anos_pendentes"
    pendentes = _anos_pendentes(chave)
    alvo = sorted(set(anos) | pendentes)
    with engine.connect() as c:
        cad = {r[0] for r in c.execute(select(deputados.c.codigo))}
    # na carga inicial tudo é "novo": só notifica depois que as proposições já foram coletadas uma vez
    notificar = _etapa_concluida("proposições")
    falhas = set()
    for ano in alvo:
        a_props = a_aut = None
        try:
            a_aut = baixar(arquivo_anual("proposicoesAutores", ano))
            a_props = baixar(arquivo_anual("proposicoes", ano))
            if not a_aut or not a_props:
                log.info("proposições %s: arquivo ainda não publicado", ano)
                continue
            with engine.begin() as c:
                gravar_processos_ano(c, ano, ler_csv(a_props), ler_csv(a_aut), cad, notificar)
        except Exception as e:
            log.error("proposições de %s não carregadas (%s) — ficam para a próxima coleta", ano, e)
            falhas.add(ano)
        finally:
            _apagar(a_props, a_aut)
    _salvar_pendentes(chave, (pendentes - set(alvo)) | falhas)
    with engine.begin() as c:
        limpar_notificacoes(c)


# ---------------- Perfil (detalhe, órgãos, frentes) ----------------

def gravar_perfil(conn, codigo: int, detalhe, orgaos_, frentes):
    conn.execute(delete(perfis).where(perfis.c.codigo == codigo))
    conn.execute(insert(perfis).values(
        codigo=codigo, detalhe=json.dumps(detalhe or {}, ensure_ascii=False),
        orgaos=json.dumps(orgaos_ or [], ensure_ascii=False),
        frentes=json.dumps(frentes or [], ensure_ascii=False), atualizado=datetime.now()))
    civil = ((detalhe or {}).get("dados") or {}).get("nomeCivil")
    if civil:
        conn.execute(update(deputados).where(deputados.c.codigo == codigo).values(nome_completo=civil))


def coletar_perfis(codigos=None):
    with engine.connect() as c:
        q = select(deputados.c.codigo)
        if codigos:
            q = q.where(deputados.c.codigo.in_(codigos))
        alvo = [r[0] for r in c.execute(q)]
    import ex_deputados
    alvo = [x for x in alvo if x not in ex_deputados.codigos()]   # perfil próprio (período do mandato)
    for cod in alvo:
        try:
            det = get_json(f"/deputados/{cod}")
            org = paginado(f"/deputados/{cod}/orgaos?dataInicio={INICIO_LEGISLATURA}&itens=100")
            fre = (get_json(f"/deputados/{cod}/frentes") or {}).get("dados") or []
        except Exception as e:
            log.warning("perfil %s: %s", cod, e)
            continue
        with engine.begin() as c:
            gravar_perfil(c, cod, det, org, fre)
    log.info("perfis atualizados: %d", len(alvo))


# ---------------- manutenção ----------------

def limpar_deputados_sem_atividade(conn):
    """Remove do cadastro quem não está em exercício e não tem voto nem despesa registrados."""
    from sqlalchemy import exists
    sem_voto = ~exists().where(votos.c.codigo_deputado == deputados.c.codigo)
    sem_desp = ~exists().where(despesas.c.codigo_deputado == deputados.c.codigo)
    import ex_deputados
    alvo = [r[0] for r in conn.execute(select(deputados.c.codigo).where(
        deputados.c.em_exercicio.is_not(True), sem_voto, sem_desp))
        if r[0] not in ex_deputados.codigos()]     # ex-deputado acompanhado fica no cadastro
    if alvo:
        conn.execute(delete(autorias).where(autorias.c.codigo_deputado.in_(alvo)))
        conn.execute(delete(perfis).where(perfis.c.codigo.in_(alvo)))
        conn.execute(delete(deputados).where(deputados.c.codigo.in_(alvo)))
        conn.execute(delete(processos).where(
            ~exists().where(autorias.c.processo_id == processos.c.id),
            ~exists().where(deputados.c.codigo == processos.c.relator_id)))
    log.info("deputados sem atividade removidos do cadastro: %d", len(alvo))
    return len(alvo)


def marcar_coleta(conn):
    agora = datetime.now().isoformat(timespec="seconds")
    conn.execute(delete(meta).where(meta.c.chave == "ultima_coleta"))
    conn.execute(insert(meta).values(chave="ultima_coleta", valor=agora))


def _passo(nome, fn, *a):
    """Uma etapa que falha não derruba as outras. Registra a conclusão em `meta`."""
    try:
        fn(*a)
    except Exception:
        log.exception("etapa %s falhou", nome)
        return False
    with engine.begin() as c:
        chave = f"etapa_ok:{nome}"
        c.execute(delete(meta).where(meta.c.chave == chave))
        c.execute(insert(meta).values(chave=chave, valor=datetime.now().isoformat(timespec="seconds")))
    return True


def _etapa_concluida(nome):
    with engine.connect() as c:
        return c.execute(select(meta.c.valor).where(meta.c.chave == f"etapa_ok:{nome}")).first() is not None


def anos_rotina(hoje=None):
    """Dia a dia: o ano corrente (e o anterior em janeiro, quando ainda chegam ajustes)."""
    hoje = hoje or date.today()
    return list(range(hoje.year - 1 if hoje.month == 1 else hoje.year, hoje.year + 1))


def anos_ceap_rotina(hoje=None):
    """A CEAP aceita lançamentos atrasados: o ano anterior é recarregado até março."""
    hoje = hoje or date.today()
    return list(range(hoje.year - 1 if hoje.month <= 3 else hoje.year, hoje.year + 1))


def anos_legislatura(hoje=None):
    """Proposições: os arquivos anuais trazem a situação atual de cada proposição, então todos os
    anos da legislatura são relidos na rotina — é assim que o monitor percebe as mudanças de situação."""
    hoje = hoje or date.today()
    return list(range(int(INICIO_LEGISLATURA[:4]), hoje.year + 1))


def executar(desde: date | None = None, so_deputados=False):
    init_db()
    coletar_deputados()
    if not so_deputados:
        hoje = date.today()
        if desde is not None:
            anos = anos_ceap = list(range(max(desde.year, int(INICIO_LEGISLATURA[:4])), hoje.year + 1))
        else:
            anos, anos_ceap = anos_rotina(hoje), anos_ceap_rotina(hoje)
        _passo("votações", coletar_votacoes, anos)
        _passo("CEAP", coletar_ceaps, anos_ceap)
        with engine.begin() as c:
            limpar_deputados_sem_atividade(c)
        _passo("proposições", coletar_processos, anos if desde is not None else anos_legislatura(hoje))
        _passo("perfis", coletar_perfis)
        import ex_deputados
        _passo("ex-deputados", ex_deputados.atualizar)
        import coleta_meu
        _passo("meu parlamentar", coleta_meu.executar)
    with engine.begin() as c:
        if not so_deputados:
            limpar_deputados_sem_atividade(c)
        marcar_coleta(c)


def _marcar_recarga_etapas(anos):
    """Uma vez só (por versão): a coluna 'etapa' e depois as votações simbólicas surgiram depois — os anos
    antigos (que a rotina não relê) entram como pendentes para serem recarregados com elas."""
    for chave in ("votacoes_etapa_v1", "votacoes_simbolicas_v3"):
        _marcar_recarga(chave, anos)


def _marcar_recarga(chave, anos):
    with engine.begin() as c:
        if c.execute(select(meta.c.valor).where(meta.c.chave == chave)).first() is not None:
            return
        c.execute(insert(meta).values(chave=chave, valor="1"))
    _salvar_pendentes("votacoes_anos_pendentes", _anos_pendentes("votacoes_anos_pendentes") | set(anos))


def carga_inicial_se_vazio():
    """Usado no primeiro deploy: se o banco estiver vazio, coleta a legislatura inteira."""
    init_db()
    with engine.connect() as c:
        tem_votacoes = c.execute(select(votacoes.c.id).limit(1)).first() is not None
    tem = {"despesas": _etapa_concluida("CEAP"), "processos": _etapa_concluida("proposições"),
           "perfis": _etapa_concluida("perfis")}
    inicio = int(INICIO_LEGISLATURA[:4])
    todos = list(range(inicio, date.today().year + 1))
    _marcar_recarga_etapas(todos)
    if not tem_votacoes:
        log.info("banco vazio — carga completa desde %s", INICIO_LEGISLATURA)
        executar(date.fromisoformat(INICIO_LEGISLATURA))
        return
    # deploy interrompido no meio: completa só o que falta
    coletar_deputados()
    _passo("votações", coletar_votacoes, anos_rotina())
    if not tem["despesas"]:
        _passo("CEAP", coletar_ceaps, todos)
    with engine.begin() as c:
        limpar_deputados_sem_atividade(c)
    if not tem["processos"]:
        _passo("proposições", coletar_processos, todos)
    if not tem["perfis"]:
        _passo("perfis", coletar_perfis)
    with engine.begin() as c:
        limpar_deputados_sem_atividade(c)
        marcar_coleta(c)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", help="AAAA-MM-DD (carga completa a partir desta data)")
    ap.add_argument("--deputados", action="store_true", help="só o cadastro de deputados")
    ap.add_argument("--se-vazio", action="store_true", help="carga completa só se o banco estiver vazio")
    ap.add_argument("--limpar", action="store_true", help="só remove deputados sem atividade")
    a = ap.parse_args()
    if a.limpar:
        init_db()
        with engine.begin() as c:
            limpar_deputados_sem_atividade(c)
    elif a.se_vazio:
        carga_inicial_se_vazio()
    else:
        executar(date.fromisoformat(a.desde) if a.desde else None, a.deputados)
