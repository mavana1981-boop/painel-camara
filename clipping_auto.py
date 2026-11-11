"""Clipping automático: toda aprovação do parlamentar fica registrada para sempre.

O clipping olhava só a situação *atual* das proposições e relatorias: quando um projeto aprovado
mudava de situação (ex.: "Aprovado" → "Aguardando Apreciação pelo Senado"), sumia do clipping.
Aqui cada aprovação vira uma linha em clipping_aprovacoes assim que é vista — pela coleta diária,
pelas notificações de mudança de situação ou ao abrir o painel — e não sai mais.
Uma linha por proposição/relatoria e situação de aprovação (etapas diferentes viram registros diferentes).
"""
import logging
import os
import re
import unicodedata
from datetime import date, datetime

from sqlalchemy import select, text

from db import clipping_aprovacoes, engine, usuario_deputados

log = logging.getLogger(__name__)

_APROVACAO = re.compile(r"(aprovad|transformad[ao] em norma|remetid[ao] (a|ao) (senado|camara|sancao)|"
                        r"enviad[ao] (a|ao) (senado|sancao)|aguardando (sancao|promulgacao)|"
                        r"aguardando (envio|remessa) ao senado|promulgad|sancionad)")


def _sem_acento(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def eh_aprovacao(situacao):
    s = _sem_acento(situacao)
    return bool(_APROVACAO.search(s)) and "rejeitad" not in s and "nao aprovad" not in s


def _candidatos(c, cod):
    """Aprovações visíveis agora: situação atual das proposições, notificações guardadas e relatorias."""
    p = {"c": cod}
    out = []
    for r in c.execute(text("""
            SELECT pr.id, pr.identificacao, pr.ementa, pr.situacao, pr.data_situacao, pr.url, a.principal
            FROM processos pr JOIN autorias a ON a.processo_id = pr.id
            WHERE a.codigo_deputado = :c AND pr.situacao IS NOT NULL"""), p).mappings():
        if eh_aprovacao(r["situacao"]) and r["data_situacao"]:
            out.append({"tipo": "autoria", "chave": f"p:{r['id']}", "identificacao": r["identificacao"],
                        "ementa": r["ementa"], "situacao": r["situacao"], "data": r["data_situacao"],
                        "url": r["url"], "colegiado": None, "principal": bool(r["principal"]),
                        "origem": "proposicao"})
    # mudanças de situação já notificadas: recupera aprovações cuja situação já mudou de novo
    for r in c.execute(text("""
            SELECT n.processo_id, n.identificacao, n.situacao_nova, n.data_situacao, n.detectado_em,
                   pr.ementa, pr.url, a.principal
            FROM notificacoes n
            LEFT JOIN processos pr ON pr.id = n.processo_id
            LEFT JOIN autorias a ON a.processo_id = n.processo_id AND a.codigo_deputado = n.codigo_deputado
            WHERE n.codigo_deputado = :c AND n.tipo = 'situacao' AND n.situacao_nova IS NOT NULL"""), p).mappings():
        if not eh_aprovacao(r["situacao_nova"]):
            continue
        dt = r["data_situacao"] or (r["detectado_em"].date().isoformat() if isinstance(r["detectado_em"], datetime)
                                    else str(r["detectado_em"] or "")[:10])
        if not dt:
            continue
        out.append({"tipo": "autoria", "chave": f"p:{r['processo_id']}", "identificacao": r["identificacao"],
                    "ementa": r["ementa"], "situacao": r["situacao_nova"], "data": dt, "url": r["url"],
                    "colegiado": None, "principal": bool(r["principal"]), "origem": "notificacao"})
    for r in c.execute(text("""
            SELECT chave, identificacao, ementa, situacao, data_situacao, url, colegiado
            FROM relatorias WHERE codigo_deputado = :c AND situacao IS NOT NULL"""), p).mappings():
        if eh_aprovacao(r["situacao"]) and r["data_situacao"]:
            out.append({"tipo": "relatoria", "chave": r["chave"], "identificacao": r["identificacao"],
                        "ementa": r["ementa"], "situacao": r["situacao"], "data": r["data_situacao"],
                        "url": r["url"], "colegiado": r["colegiado"], "principal": None, "origem": "relatoria"})
    return out


def registrar(cod, extras=()):
    """Grava as aprovações novas do deputado no histórico do clipping. Devolve quantas entraram.
    `extras`: aprovações achadas na tramitação (buscar_tramitacoes)."""
    agora = datetime.now()
    with engine.begin() as c:
        linhas = c.execute(select(clipping_aprovacoes.c.tipo, clipping_aprovacoes.c.chave, clipping_aprovacoes.c.situacao,
                                  clipping_aprovacoes.c.data)
                           .where(clipping_aprovacoes.c.codigo_deputado == cod)).all()
        ja = {(r[0], r[1], r[2]) for r in linhas}
        no_dia = {(r[0], r[1], r[3]) for r in linhas}
        cands = _candidatos(c, cod)
        ordem = {"proposicao": 0, "notificacao": 1, "relatoria": 0, "tramitacao": 2}
        novos = []
        for x in sorted(list(cands) + list(extras), key=lambda x: (x["data"], ordem.get(x["origem"], 3))):
            k = (x["tipo"], x["chave"], (x["situacao"] or "")[:200])
            if k in ja:
                continue
            # da tramitação, só o que ainda não tem registro da mesma proposição no mesmo dia
            if x["origem"] == "tramitacao" and (x["tipo"], x["chave"], x["data"]) in no_dia:
                continue
            ja.add(k)
            no_dia.add((x["tipo"], x["chave"], x["data"]))
            novos.append({**x, "situacao": k[2], "codigo_deputado": cod, "registrado_em": agora,
                          "identificacao": (x["identificacao"] or "")[:60] or None})
        if novos:
            c.execute(clipping_aprovacoes.insert(), novos)
    return len(novos)


# ---------------- tramitação dos últimos dias (API da Câmara) ----------------

DIAS_TRAMITACAO = 7
MAX_PROPOSICOES = 150                  # por deputado e por busca
# despacho de votação que aprova a própria matéria (não um requerimento sobre ela)
_DESPACHO_APROV = re.compile(r"^\s*aprovad[oa]s?\s+(?:(?:o|a|os|as)\s+)?(?:projeto|proposta|parecer|substitutivo|"
                             r"redacao final|emenda|pec|mensagem|texto|materia|subemenda|em (?:primeiro|segundo|turno))")


def aprovacoes_na_tramitacao(trams, inicio: str, fim: str):
    """Tramitações (formato da API v2) → aprovações entre inicio e fim (AAAA-MM-DD).
    Aprovação = situação nova de aprovação ou despacho que aprova a matéria (ex.: "Aprovado o Parecer.")."""
    out, vistos = [], set()
    for t in sorted((t for t in trams or [] if isinstance(t, dict)), key=lambda t: t.get("dataHora") or ""):
        dt = (t.get("dataHora") or "")[:10]
        if not dt or not inicio <= dt <= fim:
            continue
        orgao = (t.get("siglaOrgao") or "").strip() or None
        desp = re.sub(r"\s+", " ", t.get("despacho") or "").strip()
        sit = (t.get("descricaoSituacao") or "").strip()
        if desp and _DESPACHO_APROV.match(_sem_acento(desp)):
            texto = re.split(r"(?<=[.;])\s", desp, maxsplit=1)[0].rstrip(".;")[:160]
            texto = f"{texto} ({orgao})" if orgao else texto
        elif sit and eh_aprovacao(sit):
            texto = sit
        else:
            continue
        k = (dt, orgao)
        if k in vistos:
            continue
        vistos.add(k)
        out.append({"data": dt, "situacao": texto[:200], "colegiado": orgao})
    return out


def _ler_tramitacoes(pid, inicio, fim):
    from collector import get_json
    doc = get_json(f"/proposicoes/{pid}/tramitacoes?dataInicio={inicio}&dataFim={fim}")
    return (doc or {}).get("dados") or []


def buscar_tramitacoes(cod, dias=DIAS_TRAMITACAO, hoje=None, ler=None):
    """Lê na Câmara a tramitação dos últimos `dias` das proposições do deputado que se mexeram no período
    e devolve as aprovações achadas (pega etapas que já foram seguidas de outra no mesmo intervalo)."""
    from datetime import timedelta
    hoje = hoje or date.today()
    ini, fim = (hoje - timedelta(days=dias)).isoformat(), hoje.isoformat()
    ler = ler or _ler_tramitacoes
    with engine.connect() as c:
        props = [dict(r) for r in c.execute(text("""
            SELECT pr.id, pr.identificacao, pr.ementa, pr.url, a.principal
            FROM processos pr JOIN autorias a ON a.processo_id = pr.id
            WHERE a.codigo_deputado = :c AND pr.data_situacao >= :ini
            ORDER BY pr.data_situacao DESC"""), {"c": cod, "ini": ini}).mappings()][:MAX_PROPOSICOES]
    out = []
    for p in props:
        try:
            trams = ler(p["id"], ini, fim)
        except Exception as e:
            log.warning("tramitação de %s: %s", p["identificacao"], e)
            continue
        for a in aprovacoes_na_tramitacao(trams, ini, fim):
            out.append({"tipo": "autoria", "chave": f"p:{p['id']}", "identificacao": p["identificacao"],
                        "ementa": p["ementa"], "url": p["url"], "principal": bool(p["principal"]),
                        "origem": "tramitacao", **a})
    return out


def registrar_todos(com_tramitacao=True, dias=DIAS_TRAMITACAO):
    """Etapa da coleta diária: todos os deputados acompanhados no Meu Parlamentar
    (com a tramitação dos últimos `dias` na Câmara, se com_tramitacao)."""
    with engine.connect() as c:
        cods = sorted({r[0] for r in c.execute(select(usuario_deputados.c.codigo_deputado))})
    n = 0
    for cod in cods:
        try:
            extras = []
            if com_tramitacao and os.environ.get("CLIPPING_TRAMITACAO") != "0":
                try:
                    extras = buscar_tramitacoes(cod, dias)
                except Exception:
                    log.exception("tramitação do deputado %s", cod)
            n += registrar(cod, extras)
        except Exception:
            log.exception("clipping automático do deputado %s", cod)
    log.info("clipping automático: %d aprovação(ões) nova(s) registrada(s)", n)
    return n


def do_periodo(cod, inicio: date, fim: date):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(
            select(clipping_aprovacoes).where(clipping_aprovacoes.c.codigo_deputado == cod,
                                              clipping_aprovacoes.c.data >= inicio.isoformat(),
                                              clipping_aprovacoes.c.data <= fim.isoformat())
            .order_by(clipping_aprovacoes.c.data.desc(), clipping_aprovacoes.c.id)).mappings()]


# ---------------- busca disparada pela tela ----------------

import threading  # noqa: E402

_buscando = set()
_buscando_lock = threading.Lock()


def buscar_em_segundo_plano(cod, dias=DIAS_TRAMITACAO):
    """Botão do clipping: lê a tramitação dos últimos dias deste deputado sem travar a página."""
    with _buscando_lock:
        if cod in _buscando:
            return False
        _buscando.add(cod)

    def rodar():
        try:
            n = registrar(cod, buscar_tramitacoes(cod, dias))
            log.info("clipping do deputado %s: %d aprovação(ões) dos últimos %d dias", cod, n, dias)
        except Exception:
            log.exception("busca de aprovações do deputado %s", cod)
        finally:
            with _buscando_lock:
                _buscando.discard(cod)
    threading.Thread(target=rodar, daemon=True).start()
    return True


def buscando(cod):
    return cod in _buscando


CHAVE_CARGA_INICIAL = "clipping:tramitacao_inicial"


def carga_inicial_se_preciso():
    """Uma vez, logo após o deploy: aprovações dos últimos 7 dias de todos os deputados acompanhados."""
    from db import init_db, meta
    from sqlalchemy import delete, insert
    try:
        init_db()                          # a tabela nova pode não existir ainda no primeiro deploy
    except Exception:
        log.exception("init_db antes da carga do clipping")
    with engine.connect() as c:
        if c.execute(select(meta.c.valor).where(meta.c.chave == CHAVE_CARGA_INICIAL)).scalar():
            return 0
    n = registrar_todos(com_tramitacao=True)
    with engine.begin() as c:
        c.execute(delete(meta).where(meta.c.chave == CHAVE_CARGA_INICIAL))
        c.execute(insert(meta).values(chave=CHAVE_CARGA_INICIAL, valor=datetime.now().isoformat(timespec="seconds")))
    return n
