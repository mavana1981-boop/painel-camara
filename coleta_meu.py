"""Coleta do "Meu Parlamentar": agenda pública da Câmara, relatorias (pelas pautas) e radar de notícias.

Roda na atualização diária (depois das etapas principais) e também sob demanda pelo botão
"Atualizar agora".

- Agenda: /eventos da API v2 (7 dias para trás e 45 para a frente). Para as reuniões e sessões
  deliberativas, lê também a pauta (/eventos/{id}/pauta), que traz o relator de cada item.
- Relatorias: a Câmara não tem um serviço de "relatorias do deputado"; elas são montadas a partir
  das pautas — cada item com relator vira uma relatoria daquele deputado, com o resultado do item
  (ex.: "Aprovado o Parecer") e a data da reunião. Na primeira execução, lê também as pautas dos
  últimos 180 dias (MEU_DIAS_HISTORICO) para já começar com histórico.
- Notícias: só para deputados com pelo menos um usuário vinculado.
"""
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from sqlalchemy import delete, insert, select, update

from db import (agenda, deputados, engine, meta, noticias, relatorias, temas_radar, usuario_deputados)

log = logging.getLogger("meu_parlamentar")

DIAS_AGENDA_PASSADO, DIAS_AGENDA_FUTURO = 7, 45
DIAS_NOTICIAS = 60
DIAS_HISTORICO = int(os.environ.get("MEU_DIAS_HISTORICO", "180"))
CHAVE_HISTORICO = "meu:historico_pautas"

# ---------------- utilitários ----------------


def _data_hora(v):
    """'2026-10-23T10:00' → ('2026-10-23', '10:00')."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2}))?", str(v or ""))
    if not m:
        return None, None
    return m[1], (f"{m[2]}:{m[3]}" if m[2] else None)


def _ident(p):
    """Identificação "PL 1234/2025" a partir do objeto proposicao_ da pauta."""
    if not isinstance(p, dict):
        return None
    sig, num, ano = (p.get("siglaTipo") or "").strip(), p.get("numero"), p.get("ano")
    if sig and num and ano:
        return f"{sig} {num}/{ano}"
    return None


def deliberativo(ev):
    """Reuniões/sessões com votação — as únicas cuja pauta tem relatores."""
    t = (ev.get("descricaoTipo") or "").lower()
    return "deliberativ" in t or t.startswith("sessão") or t.startswith("sessao")


def _orgao(ev):
    orgs = [o for o in ev.get("orgaos") or [] if isinstance(o, dict)]
    return orgs[0] if orgs else {}


# ---------------- agenda ----------------

def parse_evento(ev, pauta=None):
    """Evento da API (+ pauta, se houver) → linha da tabela agenda."""
    dia, hora = _data_hora(ev.get("dataHoraInicio"))
    if not dia or ev.get("id") is None:
        return None
    org = _orgao(ev)
    sigla = (org.get("sigla") or "").strip() or None
    tipo = (ev.get("descricaoTipo") or "").strip()
    if sigla == "PLEN":
        titulo = f"Plenário · {tipo}" if tipo else "Sessão do Plenário"
    else:
        nome = (org.get("apelido") or org.get("nomeResumido") or org.get("nomePublicacao") or org.get("nome")
                or sigla or "")
        titulo = " · ".join(x for x in (nome, tipo) if x) or "Evento"
    lc = ev.get("localCamara") or {}
    local = (lc.get("nome") if isinstance(lc, dict) else None) or ev.get("localExterno")
    partes, relatores = [], set()
    for it in pauta or []:
        ident = _ident(it.get("proposicao_")) or (it.get("titulo") or "").strip()
        if not ident:
            continue
        rel = it.get("relator") if isinstance(it.get("relator"), dict) else None
        txt = ident
        if rel and rel.get("nome"):
            txt += f" (relator: {rel['nome']}" + (f", {rel.get('siglaPartido')}-{rel.get('siglaUf')}"
                                                  if rel.get("siglaPartido") else "") + ")"
            if rel.get("id"):
                relatores.add(int(rel["id"]))
        if it.get("situacaoItem"):
            txt += f" — {it['situacaoItem']}"
        partes.append(txt)
    desc = " · ".join(partes) or re.sub(r"\s+", " ", (ev.get("descricao") or "")).strip()
    return {
        "origem": "camara", "chave": f"E{ev['id']}", "data": dia, "hora": hora, "titulo": titulo[:300],
        "colegiado": (sigla or "")[:30] or None, "local": (local or "")[:200] or None,
        "situacao": ((ev.get("situacao") or "").strip() or None) and ev["situacao"].strip()[:80],
        "descricao": desc[:1800] or None,
        "relatores": ("," + ",".join(str(r) for r in sorted(relatores)) + ",") if relatores else None,
        "url": f"https://www.camara.leg.br/evento-legislativo/{ev['id']}",
    }


def gravar_agenda_camara(conn, eventos, inicio: date, fim: date):
    """Substitui a agenda pública do período pela versão mais recente (reuniões são remarcadas)."""
    conn.execute(delete(agenda).where(agenda.c.origem == "camara", agenda.c.data >= inicio.isoformat(),
                                      agenda.c.data <= fim.isoformat()))
    agora, vistos, n = datetime.now(), set(), 0
    for ev in eventos:
        if ev and ev["chave"] not in vistos and inicio.isoformat() <= ev["data"] <= fim.isoformat():
            vistos.add(ev["chave"])
            conn.execute(insert(agenda).values(**ev, codigo_deputado=None, criado_em=agora))
            n += 1
    return n


def eventos_periodo(ini: date, fim: date):
    """Eventos da Câmara entre duas datas (mês a mês, para não estourar a paginação)."""
    from collector import paginado
    out, d = [], ini
    while d <= fim:
        ate = min(fim, (d.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1))
        out += paginado(f"/eventos?dataInicio={d}&dataFim={ate}&itens=100&ordem=ASC&ordenarPor=dataHoraInicio")
        d = ate + timedelta(days=1)
    vistos, unicos = set(), []
    for ev in out:
        if isinstance(ev, dict) and ev.get("id") not in vistos:
            vistos.add(ev.get("id"))
            unicos.append(ev)
    return unicos


def pauta(evento_id):
    from collector import get_json
    try:
        doc = get_json(f"/eventos/{evento_id}/pauta")
    except Exception as e:
        log.warning("pauta do evento %s: %s", evento_id, e)
        return []
    return [x for x in (doc or {}).get("dados") or [] if isinstance(x, dict)]


def coletar_agenda(hoje=None):
    hoje = hoje or date.today()
    ini, fim = hoje - timedelta(days=DIAS_AGENDA_PASSADO), hoje + timedelta(days=DIAS_AGENDA_FUTURO)
    evs = eventos_periodo(ini, fim)
    if not evs:
        log.warning("agenda: nenhum evento lido — API indisponível ou formato mudou")
        return 0
    linhas, n_rel = [], 0
    for ev in evs:
        itens = pauta(ev["id"]) if deliberativo(ev) else []
        linhas.append(parse_evento(ev, itens))
        if itens:
            with engine.begin() as c:
                n_rel += gravar_relatorias_pauta(c, ev, itens)
    with engine.begin() as c:
        n = gravar_agenda_camara(c, linhas, ini, fim)
    log.info("agenda da Câmara: %d eventos (%d itens de pauta com relator)", n, n_rel)
    return n


def colegiados_do_deputado(codigo):
    """Siglas dos órgãos de que o deputado participa hoje (do perfil coletado)."""
    import paineis
    pf = paineis.perfil(codigo)
    return {c["sigla"] for c in pf["comissoes"] + pf["cargos"] if c.get("sigla")}


# ---------------- relatorias (pelas pautas) ----------------

def relatorias_da_pauta(ev, itens):
    """Itens da pauta com relator → [(codigo_deputado, linha da relatoria)]."""
    dia, _ = _data_hora(ev.get("dataHoraInicio"))
    sigla = (_orgao(ev).get("sigla") or "").strip() or None
    out = []
    for it in itens:
        rel, p = it.get("relator"), it.get("proposicao_")
        if not isinstance(rel, dict) or not rel.get("id") or not isinstance(p, dict) or not p.get("id"):
            continue
        ident = _ident(p) or (it.get("titulo") or "").strip()
        if not ident:
            continue
        out.append((int(rel["id"]), {
            "chave": f"{p['id']}|{sigla or ''}"[:120], "identificacao": ident[:60], "codigo_materia": int(p["id"]),
            "ementa": (p.get("ementa") or "").strip() or None, "colegiado": (sigla or "")[:30] or None,
            "data_designacao": dia, "data_destituicao": None,
            "situacao": (it.get("situacaoItem") or "").strip()[:200] or None,
            "data_situacao": dia if (it.get("situacaoItem") or "").strip() else None,
            "url": f"https://www.camara.leg.br/propostas-legislativas/{p['id']}",
        }))
    return out


def gravar_relatorias_pauta(conn, ev, itens, cadastro=None):
    if cadastro is None:
        cadastro = {r[0] for r in conn.execute(select(deputados.c.codigo))}
    agora, n = datetime.now(), 0
    for cod, r in relatorias_da_pauta(ev, itens):
        if cod not in cadastro:
            continue
        atual = conn.execute(select(relatorias.c.data_designacao, relatorias.c.data_situacao).where(
            relatorias.c.codigo_deputado == cod, relatorias.c.chave == r["chave"])).first()
        if not atual:
            conn.execute(insert(relatorias).values(codigo_deputado=cod, **r, atualizado=agora))
        else:
            valores = {"atualizado": agora, "ementa": r["ementa"], "identificacao": r["identificacao"]}
            if r["data_designacao"] and (not atual[0] or r["data_designacao"] < atual[0]):
                valores["data_designacao"] = r["data_designacao"]
            # o resultado mais recente prevalece
            if r["situacao"] and (not atual[1] or (r["data_situacao"] or "") >= atual[1]):
                valores.update(situacao=r["situacao"], data_situacao=r["data_situacao"])
            conn.execute(update(relatorias).where(relatorias.c.codigo_deputado == cod,
                                                  relatorias.c.chave == r["chave"]).values(**valores))
        n += 1
    return n


def _meta(chave):
    with engine.connect() as c:
        return c.execute(select(meta.c.valor).where(meta.c.chave == chave)).scalar()


def _gravar_meta(chave, valor):
    with engine.begin() as c:
        c.execute(delete(meta).where(meta.c.chave == chave))
        c.execute(insert(meta).values(chave=chave, valor=valor))


def coletar_historico_pautas(hoje=None, dias=DIAS_HISTORICO):
    """Uma vez só: lê as pautas das reuniões deliberativas dos últimos `dias` para montar as relatorias."""
    if _meta(CHAVE_HISTORICO):
        return 0
    hoje = hoje or date.today()
    ini, fim = hoje - timedelta(days=dias), hoje - timedelta(days=DIAS_AGENDA_PASSADO + 1)
    n = 0
    if ini <= fim:
        with engine.connect() as c:
            cad = {r[0] for r in c.execute(select(deputados.c.codigo))}
        for ev in eventos_periodo(ini, fim):
            if not deliberativo(ev):
                continue
            itens = pauta(ev["id"])
            if itens:
                with engine.begin() as c:
                    n += gravar_relatorias_pauta(c, ev, itens, cad)
    _gravar_meta(CHAVE_HISTORICO, datetime.now().isoformat(timespec="seconds"))
    log.info("histórico de pautas (%d dias): %d itens com relator", dias, n)
    return n


# ---------------- radar de notícias ----------------

def url_noticias(consulta):
    return ("https://news.google.com/rss/search?q=" + quote(f"{consulta} when:7d")
            + "&hl=pt-BR&gl=BR&ceid=BR:pt-419")


def parse_rss(xml_texto):
    out = []
    try:
        raiz = ET.fromstring(xml_texto)
    except ET.ParseError:
        return out
    for it in raiz.iter("item"):
        titulo = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not titulo or not link:
            continue
        fonte = (it.findtext("source") or "").strip() or None
        if fonte and titulo.endswith(f" - {fonte}"):
            titulo = titulo[: -len(fonte) - 3].strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).replace(tzinfo=None)
        except (TypeError, ValueError):
            pub = None
        out.append({"titulo": titulo[:500], "link": link[:1000], "fonte": fonte, "publicado": pub})
    return out


def consultas_radar(codigo):
    with engine.connect() as c:
        s = c.execute(select(deputados.c.nome).where(deputados.c.codigo == codigo)).first()
        temas = [r[0] for r in c.execute(select(temas_radar.c.termo).where(
            temas_radar.c.codigo_deputado == codigo).order_by(temas_radar.c.termo))]
    out = []
    if s:
        out.append((s[0], f'"{s[0]}" deputado'))
    out += [(t, t) for t in temas]
    return out


def gravar_noticias(conn, codigo, termo, itens, hoje=None):
    agora = datetime.now()
    conhecidos = {r[0] for r in conn.execute(select(noticias.c.link).where(noticias.c.codigo_deputado == codigo))}
    n = 0
    for it in itens:
        if it["link"] in conhecidos:
            continue
        conhecidos.add(it["link"])
        conn.execute(insert(noticias).values(codigo_deputado=codigo, termo=termo, coletado_em=agora, **it))
        n += 1
    corte = datetime.combine((hoje or date.today()) - timedelta(days=DIAS_NOTICIAS), datetime.min.time())
    conn.execute(delete(noticias).where(noticias.c.codigo_deputado == codigo, noticias.c.coletado_em < corte))
    return n


def coletar_noticias(codigo):
    import requests
    total = 0
    for termo, consulta in consultas_radar(codigo):
        try:
            r = requests.get(url_noticias(consulta), timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 (painel-camara)"})
            if r.status_code != 200:
                log.warning("radar %s/%s: HTTP %s", codigo, termo, r.status_code)
                continue
            itens = parse_rss(r.text)
        except Exception as e:
            log.warning("radar %s/%s: %s", codigo, termo, e)
            continue
        with engine.begin() as c:
            total += gravar_noticias(c, codigo, termo, itens[:40])
    log.info("radar deputado %s: %d notícias novas", codigo, total)
    return total


# ---------------- orquestração ----------------

def deputados_vinculados():
    with engine.connect() as c:
        return sorted({r[0] for r in c.execute(select(usuario_deputados.c.codigo_deputado))})


def executar(codigos=None, agenda_tambem=True):
    """Etapa diária do Meu Parlamentar (também usada pelo botão 'Atualizar agora')."""
    if agenda_tambem:
        for fn in (coletar_agenda, coletar_historico_pautas):
            try:
                fn()
            except Exception:
                log.exception(fn.__name__)
    for cod in (codigos if codigos is not None else deputados_vinculados()):
        try:
            coletar_noticias(cod)
        except Exception:
            log.exception("notícias do deputado %s", cod)
