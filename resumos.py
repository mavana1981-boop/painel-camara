"""Ementa resumida (IA) das proposições, usada no relatório em PDF, no Excel/CSV e na tela.

O Gemini recebe as ementas em lotes e devolve uma frase curta em linguagem simples para cada uma.
Os resumos ficam na tabela `resumos_ia` (chave = id da proposição + hash da ementa) e são reaproveitados
em todas as exportações. Ao abrir a aba Proposições, os resumos do recorte começam a ser gerados em
segundo plano; na exportação o painel espera um tempo limitado pelos que faltarem e, se algum ainda
não estiver pronto, o relatório sai com a ementa oficial e o restante continua sendo gerado.
"""
import hashlib
import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import delete, insert, select

from db import engine, resumos_ia

log = logging.getLogger("resumos")

LOTE = 40                 # ementas por chamada à IA
PRAZO_EXPORTACAO = 45     # segundos que a exportação espera por resumos que faltam (gunicorn: 120 s)

SISTEMA = """Você resume ementas de proposições legislativas da Câmara dos Deputados para um relatório de assessoria parlamentar.

Para cada ementa, escreva UMA frase curta (no máximo 22 palavras) dizendo, em linguagem simples, o que a proposição faz na prática.
- Comece com verbo no presente: Cria, Proíbe, Amplia, Reduz, Obriga, Susta, Pede, Convoca, Homenageia...
- Diga o efeito concreto (quem é afetado e o quê); não repita "Altera a Lei nº ..." quando a finalidade estiver na ementa.
- Para requerimentos, diga o que se pede e a quem.
- Não invente o que não está na ementa, não opine e não use aspas.

Responda APENAS com JSON puro: [{"id": <id>, "resumo": "<frase>"}, ...] com todos os ids recebidos."""

_lock = threading.Lock()
_em_andamento = set()


def _hash(ementa):
    return hashlib.sha1((ementa or "").strip().encode("utf-8")).hexdigest()


def resumos_de(itens):
    """{id: resumo} dos itens (dicts com id e ementa) que já têm resumo válido para a ementa atual."""
    alvo = {i["id"]: _hash(i.get("ementa")) for i in itens if i.get("id") and (i.get("ementa") or "").strip()}
    out, ids = {}, list(alvo)
    with engine.connect() as c:
        for k in range(0, len(ids), 900):
            for r in c.execute(select(resumos_ia).where(resumos_ia.c.processo_id.in_(ids[k:k + 900]))).mappings():
                if r["hash"] == alvo[r["processo_id"]] and r["resumo"]:
                    out[r["processo_id"]] = r["resumo"]
    return out


def anexar(itens):
    """Coloca it["resumo"] nos itens que já têm resumo. Devolve quantos ficaram sem."""
    rs = resumos_de(itens)
    falta = 0
    for it in itens:
        it["resumo"] = rs.get(it.get("id"))
        if not it["resumo"] and (it.get("ementa") or "").strip():
            falta += 1
    return falta


def _json(texto):
    """JSON (lista ou objeto) de uma resposta que pode vir com ``` ou texto em volta."""
    t = (texto or "").strip()
    if "```" in t:
        for bloco in t.split("```"):
            b = bloco.strip()
            b = b[4:].strip() if b.lower().startswith("json") else b
            if b and b[0] in "[{":
                t = b
                break
    for a, f in (("[", "]"), ("{", "}")):
        i, j = t.find(a), t.rfind(f) + 1
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j])
            except ValueError:
                continue
    raise ValueError("sem JSON")


def _extrair(texto):
    try:
        dados = _json(texto)
    except ValueError:
        return {}
    if isinstance(dados, dict):
        dados = dados.get("resumos") or dados.get("itens") or [dados]
    out = {}
    for d in dados if isinstance(dados, list) else []:
        try:
            pid = int(d.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        r = " ".join(str(d.get("resumo") or "").split()).strip(' "')
        if r:
            out[pid] = r[:400]
    return out


def _gerar_lote(lote, sessao=None):
    import questoes
    msg = "Ementas:\n" + "\n".join(
        json.dumps({"id": i["id"], "tipo": i.get("sigla") or "", "ementa": " ".join(i["ementa"].split())},
                   ensure_ascii=False) for i in lote)
    texto = questoes.chamar_ia(msg, sessao, sistema=SISTEMA, modelo_=questoes.modelo_rapido(),
                               max_tokens=90 * len(lote) + 200, timeout=90)
    res = _extrair(texto)
    hashes = {i["id"]: _hash(i["ementa"]) for i in lote}
    novos = [{"processo_id": pid, "hash": hashes[pid], "resumo": r, "modelo": questoes.modelo_rapido(),
              "criado_em": datetime.now()} for pid, r in res.items() if pid in hashes]
    if novos:
        with engine.begin() as c:
            c.execute(delete(resumos_ia).where(resumos_ia.c.processo_id.in_([n["processo_id"] for n in novos])))
            c.execute(insert(resumos_ia), novos)
    return {n["processo_id"]: n["resumo"] for n in novos}


def gerar(itens, prazo=None, sessao=None):
    """Gera os resumos que faltam, em lotes, até acabar ou estourar o prazo (s). Devolve {id: resumo} novos.
    Sem chave do Gemini, não faz nada."""
    import questoes
    if not questoes.configurado():
        return {}
    ja = resumos_de(itens)
    vistos, faltam = set(), []
    for i in itens:
        if i.get("id") and (i.get("ementa") or "").strip() and i["id"] not in ja and i["id"] not in vistos:
            vistos.add(i["id"])
            faltam.append(i)
    with _lock:                                  # não pede de novo o que outra thread já está resumindo
        faltam = [i for i in faltam if i["id"] not in _em_andamento]
        _em_andamento.update(i["id"] for i in faltam)
    inicio, novos = time.monotonic(), {}
    try:
        for k in range(0, len(faltam), LOTE):
            if prazo is not None and time.monotonic() - inicio > prazo:
                break
            lote = faltam[k:k + LOTE]
            try:
                novos.update(_gerar_lote(lote, sessao))
            except Exception as e:               # IA fora ou cota esgotada: segue com a ementa oficial
                log.warning("resumos: lote de %s ementas falhou: %s", len(lote), e)
                break
            finally:
                with _lock:
                    _em_andamento.difference_update(i["id"] for i in lote)
    finally:
        with _lock:
            _em_andamento.difference_update(i["id"] for i in faltam)
    return novos


def gerar_em_segundo_plano(itens):
    import questoes
    if not questoes.configurado():
        return
    copia = [{"id": i.get("id"), "ementa": i.get("ementa"), "sigla": i.get("sigla")} for i in itens]
    if not any(i["ementa"] for i in copia):
        return
    threading.Thread(target=lambda: gerar(copia), daemon=True, name="resumos-ementas").start()
