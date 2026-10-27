"""Aba Plenário: a pauta mais recente preparada no app Minha Pauta (repositório plenario-assessoria),
com as notas da equipe.

O painel lê o banco do Minha Pauta (somente leitura) pela variável MINHA_PAUTA_DATABASE_URL e guarda
uma cópia das últimas pautas na tabela `plenario_pautas`; assim a aba continua abrindo mesmo se o
outro app estiver fora do ar. A cópia é renovada a cada 30 minutos (PLENARIO_MINUTOS), na atualização
diária e pelo botão "Atualizar agora".

De cada item vêm: a ordem e os dados da pauta (projeto, ementa, autor, relator, situação, seção), a
nota da equipe (resumo da matéria — a nota técnica —, orientação e resumo do parecer, com quem salvou
e quando), as orientações por grupo (PL, NOVO, Oposição, Minoria) e o resumo da ementa feito pela IA.
As notas do Minha Pauta são por proposição, então uma nota feita para uma sessão anterior também
aparece quando a matéria volta à pauta.
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from html import escape
from html.parser import HTMLParser

from markupsafe import Markup
from sqlalchemy import create_engine, delete, insert, select, text, update

from db import agenda, engine, meta, plenario_pautas

log = logging.getLogger("plenario")

MANTER = 30                                   # pautas guardadas
LER = int(os.environ.get("PLENARIO_PAUTAS", "8"))   # pautas lidas do Minha Pauta a cada sincronização
MINUTOS = float(os.environ.get("PLENARIO_MINUTOS", "30"))
GRUPOS = [("PL", "PL"), ("NOVO", "NOVO"), ("oposicao", "Oposição"), ("minoria", "Minoria")]
_lock = threading.Lock()
_fonte = {"url": None, "engine": None}


# ---------------- conexão com o Minha Pauta ----------------

def _url():
    url = (os.environ.get("MINHA_PAUTA_DATABASE_URL") or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def configurado():
    return bool(_url())


def fonte():
    url = _url()
    if _fonte["url"] != url:
        _fonte.update(url=url, engine=create_engine(url, pool_pre_ping=True, future=True) if url else None)
    return _fonte["engine"]


def _consulta(c, sql, p=None):
    """Consulta tolerante: tabelas opcionais do Minha Pauta podem não existir."""
    try:
        with c.begin_nested():
            return [dict(r) for r in c.execute(text(sql), p or {}).mappings()]
    except Exception as e:
        log.info("Minha Pauta: %s", str(e).splitlines()[0][:160])
        return []


def _colunas(c, tabela):
    from sqlalchemy import inspect
    try:
        return {x["name"] for x in inspect(c).get_columns(tabela)}
    except Exception:
        return set()


# ---------------- leitura ----------------

def ler_pautas(conn, n=LER):
    """Últimas `n` pautas do Minha Pauta (da mais nova para a mais antiga), já com notas e orientações."""
    cols = _colunas(conn, "pauta_cache_db")
    extra_cols = ", ".join(x for x in ("fetched_at", "reordered_at", "reordered_by") if x in cols)
    rows = _consulta(conn, f"SELECT evento_id, json_pauta, last_updated, last_saved_by"
                           f"{', ' + extra_cols if extra_cols else ''} FROM pauta_cache_db "
                           f"ORDER BY evento_id DESC LIMIT :n", {"n": n})
    out = []
    for r in rows:
        try:
            itens = json.loads(r["json_pauta"] or "[]")
        except ValueError:
            continue
        if not isinstance(itens, list):
            continue
        ev = int(r["evento_id"])
        extras = _consulta(conn, "SELECT id_proposicao, projeto, ementa, autor, relator, created_by, created_at "
                                 "FROM extra_pauta WHERE evento_id = :e ORDER BY created_at", {"e": ev})
        for x in extras:
            if any(str(i.get("id_principal")) == str(x["id_proposicao"]) for i in itens):
                continue
            itens.append({"ordem": "extra", "id_principal": str(x["id_proposicao"]), "projeto": x["projeto"],
                          "ementa": x["ementa"], "autor": x["autor"], "relator": x["relator"],
                          "eh_extra_pauta": True, "incluido_por": x["created_by"]})
        chaves = [f"PROP_{i.get('id_principal')}" for i in itens if i.get("id_principal")]
        notas = {}
        if chaves:
            ncols = _colunas(conn, "notas")
            resp = ", responsavel_username" if "responsavel_username" in ncols else ""
            for i in range(0, len(chaves), 500):
                lote = chaves[i:i + 500]
                ph = ", ".join(f":k{j}" for j in range(len(lote)))
                for nrow in _consulta(conn, f"SELECT item_key, resumo_materia, orientacao, resumo_parecer, saved_by, "
                                            f"saved_at{resp} FROM notas WHERE item_key IN ({ph})",
                                      {f"k{j}": k for j, k in enumerate(lote)}):
                    notas[nrow["item_key"]] = nrow
        oris = {}
        for o in _consulta(conn, "SELECT id_principal, grupo, orientacao, comentario FROM orientacoes_grupo "
                                 "WHERE evento_id = :e", {"e": ev}):
            oris.setdefault(str(o["id_principal"]), {})[o["grupo"]] = {"orientacao": o["orientacao"],
                                                                       "comentario": o["comentario"]}
        resumos = {str(x["id_proposicao"]): x["resumo"] for x in _consulta(
            conn, "SELECT id_proposicao, resumo FROM resumos_ia WHERE evento_id = :e", {"e": ev})}
        out.append({"evento_id": ev, "itens": [_item(i, notas, oris, resumos) for i in itens],
                    "atualizado_fonte": r.get("last_updated") or r.get("reordered_at") or r.get("fetched_at"),
                    "atualizado_por": r.get("last_saved_by") or r.get("reordered_by")})
    return out


def _item(i, notas, oris, resumos):
    idp = str(i.get("id_principal") or "")
    n = notas.get(f"PROP_{idp}") or {}

    def _v(campo):   # a nota gravada prevalece sobre a cópia que veio no cache da pauta (como no Minha Pauta)
        return (n.get(campo) if n.get(campo) not in (None, "") else i.get(campo)) or ""
    nota = {"resumo_materia": _v("resumo_materia"), "orientacao": _v("orientacao"),
            "resumo_parecer": _v("resumo_parecer"), "saved_by": _v("saved_by"), "saved_at": _v("saved_at"),
            "responsavel": n.get("responsavel_username") or ""}
    projeto = (i.get("projeto") or i.get("projeto_original") or "").strip()
    return {
        "ordem": str(i.get("ordem") or ""), "id_principal": idp, "projeto": projeto,
        "projeto_original": (i.get("projeto_original") or "").split(" ao ")[0].strip() or projeto,
        "ementa": i.get("ementa") or "", "autor": i.get("autor") or "", "relator": i.get("relator") or "",
        "situacao": i.get("situacao") or "", "secao": i.get("secao") or "",
        "eh_extra_pauta": bool(i.get("eh_extra_pauta")), "incluido_por": i.get("incluido_por") or "",
        "nota": nota, "tem_nota": bool(_texto_puro(nota["resumo_materia"]) or _texto_puro(nota["resumo_parecer"])),
        "orientacoes_grupo": oris.get(idp, {}), "resumo_ia": resumos.get(idp) or "",
        "url": f"https://www.camara.leg.br/propostas-legislativas/{idp}" if idp.isdigit() else None,
    }


def _evento_info(evento_id):
    """Data, hora e situação da sessão: da agenda já coletada ou da API da Câmara."""
    with engine.connect() as c:
        a = c.execute(select(agenda).where(agenda.c.chave == f"E{evento_id}")).mappings().first()
    if a:
        return {"data": a["data"], "hora": a["hora"], "titulo": a["titulo"], "situacao": a["situacao"],
                "local": a["local"]}
    try:
        from collector import get_json
        d = (get_json(f"/eventos/{evento_id}") or {}).get("dados") or {}
    except Exception as e:
        log.warning("evento %s: %s", evento_id, e)
        d = {}
    dh = d.get("dataHoraInicio") or ""
    lc = d.get("localCamara") if isinstance(d.get("localCamara"), dict) else {}
    return {"data": dh[:10] or None, "hora": dh[11:16] or None,
            "titulo": d.get("descricaoTipo") or "Sessão Deliberativa", "situacao": d.get("situacao"),
            "local": (lc or {}).get("nome") or "Plenário Ulysses Guimarães"}


# ---------------- sincronização ----------------

def sincronizar(conn_fonte=None):
    """Copia as últimas pautas do Minha Pauta. Devolve quantas pautas foram gravadas (None sem configuração)."""
    if conn_fonte is None and not configurado():
        return None
    with _lock:
        if conn_fonte is not None:
            pautas = ler_pautas(conn_fonte)
        else:
            with fonte().connect() as c:
                pautas = ler_pautas(c)
        agora = datetime.now()
        with engine.connect() as c:
            conhecidas = {r[0]: r[1] for r in c.execute(select(plenario_pautas.c.evento_id, plenario_pautas.c.data))}
        for p in pautas:
            info = _evento_info(p["evento_id"]) if not conhecidas.get(p["evento_id"]) else {}
            valores = {"itens": json.dumps(p["itens"], ensure_ascii=False),
                       "atualizado_fonte": str(p["atualizado_fonte"] or "")[:40] or None,
                       "atualizado_por": str(p["atualizado_por"] or "")[:120] or None, "sincronizado_em": agora}
            with engine.begin() as c:
                if p["evento_id"] in conhecidas:
                    if info:
                        valores.update({k: info.get(k) for k in ("data", "hora", "titulo", "situacao", "local")})
                    c.execute(update(plenario_pautas).where(plenario_pautas.c.evento_id == p["evento_id"])
                              .values(**valores))
                else:
                    c.execute(insert(plenario_pautas).values(evento_id=p["evento_id"], **valores,
                                                             **{k: info.get(k) for k in ("data", "hora", "titulo",
                                                                                         "situacao", "local")}))
        with engine.begin() as c:
            velhas = [r[0] for r in c.execute(select(plenario_pautas.c.evento_id)
                                              .order_by(plenario_pautas.c.evento_id.desc()).offset(MANTER))]
            if velhas:
                c.execute(delete(plenario_pautas).where(plenario_pautas.c.evento_id.in_(velhas)))
            c.execute(delete(meta).where(meta.c.chave == "plenario:ultima_sync"))
            c.execute(insert(meta).values(chave="plenario:ultima_sync", valor=agora.isoformat(timespec="seconds")))
    log.info("Plenário: %d pauta(s) copiada(s) do Minha Pauta", len(pautas))
    return len(pautas)


def precisa_sincronizar(agora=None):
    if not configurado():
        return False
    with engine.connect() as c:
        v = c.execute(select(meta.c.valor).where(meta.c.chave == "plenario:ultima_sync")).scalar()
    if not v:
        return True
    try:
        return (agora or datetime.now()) - datetime.fromisoformat(v) >= timedelta(minutes=MINUTOS)
    except ValueError:
        return True


def ultima_sincronizacao():
    with engine.connect() as c:
        return c.execute(select(meta.c.valor).where(meta.c.chave == "plenario:ultima_sync")).scalar()


# ---------------- consulta para a tela ----------------

def pautas_recentes():
    with engine.connect() as c:
        return [dict(r) for r in c.execute(select(
            plenario_pautas.c.evento_id, plenario_pautas.c.data, plenario_pautas.c.hora, plenario_pautas.c.titulo,
            plenario_pautas.c.situacao).order_by(plenario_pautas.c.evento_id.desc())).mappings()]


def pauta(evento_id=None, filtro=""):
    q = select(plenario_pautas)
    q = q.where(plenario_pautas.c.evento_id == evento_id) if evento_id else \
        q.order_by(plenario_pautas.c.evento_id.desc()).limit(1)
    with engine.connect() as c:
        r = c.execute(q).mappings().first()
    if not r:
        return None
    p = dict(r)
    itens = json.loads(p.pop("itens") or "[]")
    p["total"] = len(itens)
    p["com_nota"] = sum(1 for i in itens if i["tem_nota"])
    p["com_orientacao"] = sum(1 for i in itens if i["nota"]["orientacao"])
    if filtro == "com_nota":
        itens = [i for i in itens if i["tem_nota"]]
    elif filtro == "sem_nota":
        itens = [i for i in itens if not i["tem_nota"]]
    p["itens"] = itens
    return p


# ---------------- HTML das notas (vem do editor do Minha Pauta) ----------------

_PERMITIDAS = {"p", "br", "b", "strong", "i", "em", "u", "s", "ul", "ol", "li", "h1", "h2", "h3", "h4",
               "blockquote", "span", "a", "sub", "sup", "hr"}
_VAZIAS = {"br", "hr"}
_DESCARTAR = {"script", "style", "iframe", "object", "embed", "noscript", "template"}


class _Limpa(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.pular = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _DESCARTAR:
            self.pular += 1
            return
        if self.pular or tag not in _PERMITIDAS:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if re.match(r"^https?://", href, re.I):
                self.out.append(f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">')
            else:
                self.out.append("<a>")
        else:
            self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        if not self.pular and tag in _VAZIAS:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in _DESCARTAR:
            self.pular = max(0, self.pular - 1)
            return
        if not self.pular and tag in _PERMITIDAS and tag not in _VAZIAS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.pular:
            self.out.append(escape(data))


def html_seguro(valor):
    """Mantém a formatação da nota (negrito, listas, títulos, links) e descarta qualquer outra coisa."""
    if not valor:
        return Markup("")
    s = str(valor)
    if not re.search(r"</?[a-zA-Z][^>]*>", s):     # texto simples: preserva as quebras de linha
        return Markup("<br>".join(escape(x) for x in s.splitlines()))
    p = _Limpa()
    p.feed(s)
    p.close()
    return Markup("".join(p.out))


def _texto_puro(valor):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(valor or ""))).replace("&nbsp;", " ").strip()
