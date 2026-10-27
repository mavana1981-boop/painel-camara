"""Radar de notícias do Meu Parlamentar, no modelo do Monitor Legislativo (repositório `oposicao`).

Como o Monitor:
- lê os portais cadastrados (RSS) — a lista padrão é a mesma do Monitor e o admin pode
  acrescentar, desligar ou excluir portais na própria tela do radar;
- ignora notícias já vistas;
- passa cada notícia nova por uma IA que responde se ela é relevante, a categoria, o tipo de
  ação parlamentar e a ação sugerida, com justificativa;
- a equipe acompanha cada item (Pendente → Em análise → Ação efetuada, com assessor e o que foi feito).

Diferenças: aqui o radar é por parlamentar — dos portais entram só as notícias que citam o nome
dele ou um dos temas cadastrados — e o Google Notícias complementa a busca. A IA é o Claude
(mesma ANTHROPIC_API_KEY das questões de ordem; modelo em RADAR_MODEL). O ciclo roda a cada
2 horas (RADAR_HORAS), na atualização diária e no botão "Atualizar agora".
"""
import html
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import delete, insert, or_, select, update

from db import deputados as parlamentares
from db import engine, fontes_radar, meta, noticias, norm_nome, temas_radar, usuario_deputados

log = logging.getLogger("radar")

CASA = "Câmara dos Deputados"
CARGO = "deputado"
COL = "codigo_deputado"                 # coluna do parlamentar nas tabelas do Meu Parlamentar

DIAS_NOTICIAS = 60
MAX_POR_PORTAL = 20                     # entradas lidas de cada feed por ciclo (como no Monitor)
MAX_ANALISES = int(os.environ.get("RADAR_MAX_ANALISES", "30"))   # por parlamentar e ciclo
HORAS_CICLO = float(os.environ.get("RADAR_HORAS", "2"))
MODELO_PADRAO = "claude-haiku-5-5"
STATUS = ["Pendente", "Em análise", "Ação efetuada"]

# Mesma lista padrão do Monitor Legislativo
FONTES_PADRAO = [
    ("G1 Política", "https://g1.globo.com/rss/g1/politica/"),
    ("G1 Economia", "https://g1.globo.com/rss/g1/economia/"),
    ("G1 Saúde", "https://g1.globo.com/rss/g1/ciencia-e-saude/"),
    ("G1 Educação", "https://g1.globo.com/rss/g1/educacao/"),
    ("Estadão Política", "https://feeds.estadao.com.br/rss/politica"),
    ("Estadão Economia", "https://feeds.estadao.com.br/rss/economia"),
    ("Estadão Brasil", "https://feeds.estadao.com.br/rss/brasil"),
    ("Folha Poder", "https://feeds.folha.uol.com.br/poder/rss091.xml"),
    ("Folha Mercado", "https://feeds.folha.uol.com.br/mercado/rss091.xml"),
    ("Folha Saúde", "https://feeds.folha.uol.com.br/equilibrioesaude/rss091.xml"),
    ("Folha Educação", "https://feeds.folha.uol.com.br/educacao/rss091.xml"),
    ("Folha Cotidiano", "https://feeds.folha.uol.com.br/cotidiano/rss091.xml"),
    ("O Antagonista", "https://www.oantagonista.com/feed/"),
    ("Poder360", "https://www.poder360.com.br/feed/"),
    ("Metrópoles Política", "https://www.metropoles.com/brasil/politica-brasil/feed"),
    ("Metrópoles Crimes", "https://www.metropoles.com/brasil/policia/feed"),
    ("CNN Brasil", "https://www.cnnbrasil.com.br/feed/"),
    ("Veja Brasil", "https://veja.abril.com.br/feed/"),
]

CATEGORIAS = ["Gastos públicos / corrupção", "Segurança pública e crimes", "Saúde pública", "Educação",
              "Economia e emprego", "Direitos e liberdades civis", "Meio ambiente", "Política externa",
              "Reforma institucional", "Infraestrutura e serviços públicos", "Outros"]
TIPOS_ACAO = ["Requerimento de informações", "Requerimento de audiência pública", "Nota à imprensa",
              "Projeto de Lei", "PEC", "Ofício", "Denúncia ao TCU/MPF", "Requerimento de convocação"]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def modelo():
    return os.environ.get("RADAR_MODEL", MODELO_PADRAO)


# ---------------- leitura dos feeds ----------------

def _limpar_html(s, limite=500):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:limite] + "...") if len(s) > limite else s


def parse_feed(conteudo):
    """RSS ou Atom → [{titulo, link, fonte, publicado, resumo}]."""
    import feedparser
    feed = feedparser.parse(conteudo)
    out = []
    for e in feed.entries:
        titulo = _limpar_html(e.get("title", ""), 500)
        link = (e.get("link") or "").strip()
        if not titulo or not link:
            continue
        src = e.get("source") or {}
        fonte = (src.get("title") if isinstance(src, dict) else None) or None
        if fonte and titulo.endswith(f" - {fonte}"):
            titulo = titulo[: -len(fonte) - 3].strip()
        pub = None
        if e.get("published_parsed"):
            pub = datetime(*e.published_parsed[:6])
        resumo = _limpar_html(e.get("summary") or e.get("description") or "")
        if fonte and resumo.endswith(fonte):          # Google Notícias repete título + fonte no resumo
            resumo = ""
        out.append({"titulo": titulo[:500], "link": link[:1000], "fonte": fonte, "publicado": pub,
                    "resumo": resumo or None})
    return out


def baixar(url):
    import requests
    r = requests.get(url, headers=_HEADERS, timeout=15)
    if r.status_code != 200:
        log.warning("radar: HTTP %s em %s", r.status_code, url)
        return None
    return r.content


# ---------------- Google Notícias ----------------

def url_noticias(consulta):
    return ("https://news.google.com/rss/search?q=" + quote(f"{consulta} when:7d")
            + "&hl=pt-BR&gl=BR&ceid=BR:pt-419")


def consultas_radar(codigo):
    """[(termo, consulta no Google)]: o nome do parlamentar e os temas cadastrados."""
    with engine.connect() as c:
        s = c.execute(select(parlamentares.c.nome).where(parlamentares.c.codigo == codigo)).first()
        temas = [r[0] for r in c.execute(select(temas_radar.c.termo).where(
            getattr(temas_radar.c, COL) == codigo).order_by(temas_radar.c.termo))]
    out = []
    if s:
        out.append((s[0], f'"{s[0]}" {CARGO}'))
    out += [(t, t) for t in temas]
    return out


def gravar_noticias(conn, codigo, termo, itens, hoje=None, origem="google"):
    """Grava as notícias ainda não vistas para este parlamentar e apaga as antigas sem acompanhamento."""
    agora = datetime.now()
    col = getattr(noticias.c, COL)
    conhecidos = {r[0] for r in conn.execute(select(noticias.c.link).where(col == codigo))}
    n = 0
    for it in itens:
        if it["link"] in conhecidos:
            continue
        conhecidos.add(it["link"])
        conn.execute(insert(noticias).values(**{COL: codigo}, termo=termo[:120], coletado_em=agora, origem=origem,
                                             titulo=it["titulo"], link=it["link"], fonte=it.get("fonte"),
                                             publicado=it.get("publicado"), resumo=it.get("resumo"),
                                             status="Pendente"))
        n += 1
    corte = datetime.combine((hoje or date.today()) - timedelta(days=DIAS_NOTICIAS), datetime.min.time())
    conn.execute(delete(noticias).where(col == codigo, noticias.c.coletado_em < corte,
                                        or_(noticias.c.status.is_(None), noticias.c.status == "Pendente"),
                                        noticias.c.assessor.is_(None)))
    return n


def coletar_google(codigo, buscar=baixar):
    total = 0
    for termo, consulta in consultas_radar(codigo):
        try:
            conteudo = buscar(url_noticias(consulta))
            itens = parse_feed(conteudo) if conteudo else []
        except Exception as e:
            log.warning("radar Google %s/%s: %s", codigo, termo, e)
            continue
        with engine.begin() as c:
            total += gravar_noticias(c, codigo, termo, itens[:40], origem="google")
    return total


# ---------------- portais (como no Monitor) ----------------

def garantir_fontes():
    """Cadastra os portais padrão uma única vez (exclusões feitas pelo admin são respeitadas)."""
    with engine.begin() as c:
        if c.execute(select(meta.c.valor).where(meta.c.chave == "radar:fontes_padrao")).first():
            return
        existentes = {r[0] for r in c.execute(select(fontes_radar.c.url))}
        for nome, url in FONTES_PADRAO:
            if url not in existentes:
                c.execute(insert(fontes_radar).values(nome=nome, url=url, ativa=True))
        c.execute(insert(meta).values(chave="radar:fontes_padrao", valor=datetime.now().isoformat(timespec="seconds")))


def lista_fontes():
    garantir_fontes()
    with engine.connect() as c:
        return [dict(r) for r in c.execute(select(fontes_radar).order_by(fontes_radar.c.nome)).mappings()]


def alterar_fonte(acao, f):
    with engine.begin() as c:
        if acao == "fonte_add":
            nome, url = (f.get("nome") or "").strip()[:120], (f.get("url") or "").strip()[:500]
            if not nome or not re.match(r"^https?://", url):
                raise ValueError("Informe o nome do portal e o endereço do feed RSS (começando por http).")
            if c.execute(select(fontes_radar.c.id).where(fontes_radar.c.url == url)).first():
                raise ValueError("Esse feed já está cadastrado.")
            c.execute(insert(fontes_radar).values(nome=nome, url=url, ativa=True))
        elif acao == "fonte_toggle":
            c.execute(update(fontes_radar).where(fontes_radar.c.id == f.get("fonte", type=int))
                      .values(ativa=~fontes_radar.c.ativa))
        elif acao == "fonte_del":
            c.execute(delete(fontes_radar).where(fontes_radar.c.id == f.get("fonte", type=int)))


def _termos_norm(codigo):
    return [(t, norm_nome(t)) for t, _ in consultas_radar(codigo) if len(norm_nome(t)) >= 4]


def casa_termo(texto, termos):
    """Primeiro termo (nome ou tema) que aparece como palavra(s) inteira(s) no texto."""
    alvo = f" {norm_nome(texto)} "
    for rotulo, tn in termos:
        if f" {tn} " in alvo:
            return rotulo
    return None


def coletar_portais(codigos, buscar=baixar):
    """Lê cada portal ativo uma vez e distribui as notícias que citam cada parlamentar ou tema."""
    termos = {cod: _termos_norm(cod) for cod in codigos}
    termos = {k: v for k, v in termos.items() if v}
    if not termos:
        return 0
    with engine.connect() as c:
        fontes = [dict(r) for r in c.execute(select(fontes_radar).where(fontes_radar.c.ativa.is_(True))).mappings()]
    total = 0
    for f in fontes:
        try:
            conteudo = buscar(f["url"])
            itens = parse_feed(conteudo)[:MAX_POR_PORTAL] if conteudo else []
        except Exception as e:
            log.warning("radar portal %s: %s", f["nome"], e)
            continue
        for cod, ts in termos.items():
            achados = {}
            for it in itens:
                t = casa_termo(f"{it['titulo']} {it.get('resumo') or ''}", ts)
                if t:
                    achados.setdefault(t, []).append({**it, "fonte": f["nome"]})
            for termo, lst in achados.items():
                with engine.begin() as c:
                    total += gravar_noticias(c, cod, termo, lst, origem="portal")
    return total


# ---------------- análise por IA (prompt do Monitor, por parlamentar) ----------------

SISTEMA = f"""Você é um analista político especializado em assessorar a Liderança da Oposição na {CASA} do Brasil, acompanhando o mandato de um(a) parlamentar específico(a).

Sua tarefa é analisar uma notícia que cita o(a) parlamentar ou um tema acompanhado pelo gabinete e decidir se ela é relevante para uma ação parlamentar dele(a). O escopo é amplo: além de política, também interessam notícias de segurança pública, saúde, educação, economia, crimes, corrupção, direitos civis e qualquer tema que possa fundamentar uma ação parlamentar — inclusive defender o(a) parlamentar de uma notícia negativa ou aproveitar uma positiva.

Responda APENAS com JSON puro, sem markdown, sem texto antes ou depois, sem blocos de código.

Formato obrigatório:
{{"relevante": true, "categoria": "Saúde pública", "tipo_acao": "Requerimento de informações", "acao_sugerida": "Texto curto aqui", "justificativa": "Texto curto aqui"}}

Categorias possíveis:
{chr(10).join('- "' + c + '"' for c in CATEGORIAS)}

Tipos de ação parlamentar:
{chr(10).join('- "' + t + '"' for t in TIPOS_ACAO)}

Marque relevante=true sempre que a notícia revelar falha do governo, omissão de órgão público, dado preocupante sobre serviço público, escândalo, crime com envolvimento de agente público, algo que envolva diretamente o(a) parlamentar, ou qualquer situação que justifique fiscalização parlamentar.

Se não for relevante: {{"relevante": false, "categoria": null, "tipo_acao": null, "acao_sugerida": null, "justificativa": "Breve motivo"}}"""


def extrair_json(texto):
    """Tenta extrair JSON válido de uma string com possível lixo ao redor (igual ao Monitor)."""
    texto = (texto or "").strip()
    if "```" in texto:
        for bloco in texto.split("```"):
            bloco = bloco.strip()
            if bloco.lower().startswith("json"):
                bloco = bloco[4:].strip()
            if bloco.startswith("{"):
                texto = bloco
                break
    inicio, fim = texto.find("{"), texto.rfind("}") + 1
    if inicio >= 0 and fim > inicio:
        texto = texto[inicio:fim]
    return json.loads(texto)


def _txt(v, limite):
    return str(v).strip()[:limite] if v not in (None, "", "null") else None


def analisar(codigo, limite=MAX_ANALISES, sessao=None):
    """Analisa as notícias ainda não analisadas do parlamentar. Devolve quantas analisou."""
    import questoes
    if not questoes.configurado():
        return 0
    col = getattr(noticias.c, COL)
    with engine.connect() as c:
        p = c.execute(select(parlamentares).where(parlamentares.c.codigo == codigo)).mappings().first()
        pend = [dict(r) for r in c.execute(select(noticias).where(col == codigo, noticias.c.analisado_em.is_(None))
                                           .order_by(noticias.c.publicado.desc().nullslast(), noticias.c.id.desc())
                                           .limit(limite)).mappings()]
    if not p:
        return 0
    quem = f"{p['nome']} ({p.get('partido') or 'sem partido'}-{p.get('uf') or ''})"
    n = 0
    for it in pend:
        msg = (f"Parlamentar acompanhado(a): {CARGO} {quem}\n"
               f"Termo que trouxe a notícia: {it['termo']}\n"
               f"Fonte: {it.get('fonte') or ('Google Notícias' if it.get('origem') == 'google' else '—')}\n"
               f"Título: {it['titulo']}\n"
               f"Resumo: {it.get('resumo') or 'Sem resumo'}\n\n"
               f"Responda APENAS com JSON puro conforme o formato solicitado.")
        try:
            texto = questoes.chamar_ia(msg, sessao, sistema=SISTEMA, modelo_=modelo(), max_tokens=600, timeout=60)
        except Exception as e:
            log.warning("radar: IA indisponível (%s) — o restante fica para o próximo ciclo", e)
            break
        try:
            r = extrair_json(texto)
            valores = {"relevante": bool(r.get("relevante")), "categoria": _txt(r.get("categoria"), 80),
                       "tipo_acao": _txt(r.get("tipo_acao"), 80), "acao_sugerida": _txt(r.get("acao_sugerida"), 2000),
                       "justificativa": _txt(r.get("justificativa"), 2000)}
        except (ValueError, AttributeError):
            valores = {"relevante": None, "justificativa": "A resposta da IA não pôde ser lida."}
        with engine.begin() as c:
            c.execute(update(noticias).where(noticias.c.id == it["id"]).values(analisado_em=datetime.now(), **valores))
        n += 1
    return n


# ---------------- acompanhamento pela equipe ----------------

def atualizar_item(codigo, nid, f):
    status = f.get("status") if f.get("status") in STATUS else "Pendente"
    with engine.begin() as c:
        c.execute(update(noticias).where(noticias.c.id == nid, getattr(noticias.c, COL) == codigo).values(
            status=status, assessor=(f.get("assessor") or "").strip()[:120] or None,
            acao_efetuada=(f.get("acao_efetuada") or "").strip()[:4000] or None))


def lista(codigo, termo="", filtro="", limite=200):
    col = getattr(noticias.c, COL)
    q = select(noticias).where(col == codigo)
    if termo:
        q = q.where(noticias.c.termo == termo)
    if filtro == "relevantes":
        q = q.where(noticias.c.relevante.is_(True))
    elif filtro in ("portal", "google"):
        q = q.where(noticias.c.origem == filtro)
    elif filtro in STATUS:
        q = q.where(noticias.c.relevante.is_(True), noticias.c.status == filtro)
    with engine.connect() as c:
        return [dict(r) for r in c.execute(q.order_by(noticias.c.publicado.desc().nullslast(), noticias.c.id.desc())
                                           .limit(limite)).mappings()]


# ---------------- ciclo ----------------

def vinculados():
    with engine.connect() as c:
        return sorted({r[0] for r in c.execute(select(getattr(usuario_deputados.c, COL)))})


_lock = threading.Lock()   # um ciclo por vez (agendador de 2h, rotina diária e "Atualizar agora")


def ciclo(codigos=None, buscar=baixar, sessao=None):
    """Portais → Google Notícias → análise por IA. Sem `codigos`: todos os parlamentares vinculados."""
    with _lock:
        return _ciclo(codigos, buscar, sessao)


def _ciclo(codigos, buscar, sessao):
    garantir_fontes()
    cods = list(codigos) if codigos is not None else vinculados()
    res = {"portais": 0, "google": 0, "analisadas": 0}
    if cods:
        try:
            res["portais"] = coletar_portais(cods, buscar)
        except Exception:
            log.exception("radar: portais")
        for cod in cods:
            try:
                res["google"] += coletar_google(cod, buscar)
                res["analisadas"] += analisar(cod, sessao=sessao)
            except Exception:
                log.exception("radar do parlamentar %s", cod)
    if codigos is None:
        with engine.begin() as c:
            c.execute(delete(meta).where(meta.c.chave == "radar:ultimo_ciclo"))
            c.execute(insert(meta).values(chave="radar:ultimo_ciclo", valor=datetime.now().isoformat(timespec="seconds")))
    log.info("radar: %s", res)
    return res


def precisa_ciclo(agora=None):
    with engine.connect() as c:
        v = c.execute(select(meta.c.valor).where(meta.c.chave == "radar:ultimo_ciclo")).scalar()
    if not v:
        return True
    try:
        return (agora or datetime.now()) - datetime.fromisoformat(v) >= timedelta(hours=HORAS_CICLO)
    except ValueError:
        return True
