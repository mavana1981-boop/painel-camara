"""Clipping automático: toda aprovação do parlamentar fica registrada para sempre.

O clipping olhava só a situação *atual* das proposições e relatorias: quando um projeto aprovado
mudava de situação (ex.: "Aprovado" → "Aguardando Apreciação pelo Senado"), sumia do clipping.
Aqui cada aprovação vira uma linha em clipping_aprovacoes assim que é vista — pela coleta diária,
pelas notificações de mudança de situação ou ao abrir o painel — e não sai mais.
Uma linha por proposição/relatoria e situação de aprovação (etapas diferentes viram registros diferentes).
"""
import logging
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


def registrar(cod):
    """Grava as aprovações novas do deputado no histórico do clipping. Devolve quantas entraram."""
    agora = datetime.now()
    with engine.begin() as c:
        ja = {(r[0], r[1], r[2]) for r in c.execute(
            select(clipping_aprovacoes.c.tipo, clipping_aprovacoes.c.chave, clipping_aprovacoes.c.situacao)
            .where(clipping_aprovacoes.c.codigo_deputado == cod))}
        novos = []
        for x in sorted(_candidatos(c, cod), key=lambda x: x["data"]):   # a data mais antiga de cada etapa fica
            k = (x["tipo"], x["chave"], (x["situacao"] or "")[:200])
            if k in ja:
                continue
            ja.add(k)
            novos.append({**x, "situacao": k[2], "codigo_deputado": cod, "registrado_em": agora,
                          "identificacao": (x["identificacao"] or "")[:60] or None})
        if novos:
            c.execute(clipping_aprovacoes.insert(), novos)
    return len(novos)


def registrar_todos():
    """Etapa da coleta diária: todos os deputados acompanhados no Meu Parlamentar."""
    with engine.connect() as c:
        cods = sorted({r[0] for r in c.execute(select(usuario_deputados.c.codigo_deputado))})
    n = 0
    for cod in cods:
        try:
            n += registrar(cod)
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
