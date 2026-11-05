"""Radar de notícias do Meu Parlamentar, no modelo do Monitor Legislativo (repositório `oposicao`).

Como o Monitor:
- lê os portais cadastrados (RSS) — a lista padrão é a mesma do Monitor e o admin pode
  acrescentar, desligar ou excluir portais na própria tela do radar;
- ignora notícias já vistas;
- passa cada notícia nova por uma IA que responde se ela é relevante, a categoria, o tipo de
  ação parlamentar e a ação sugerida, com justificativa;
- a equipe acompanha cada item (Pendente → Em análise → Ação efetuada, com assessor e o que foi feito);
- a equipe pode pesquisar um termo no que já foi coletado, buscar o termo na hora no Google Notícias
  e incluir manualmente uma notícia pelo link (origem "manual"; a IA analisa em seguida).

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
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import and_, delete, func, insert, or_, select, update

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
TIPOS_ACAO = ["Requerimento de informações", "Requerimento de convocação", "Requerimento de audiência pública",
              "Denúncia ao TCU/MPF", "Projeto de Lei", "Projeto de Decreto Legislativo", "PEC", "Nota à imprensa",
              "Discurso em Plenário", "Post para redes sociais", "Ofício"]

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

VIES = """Linha editorial (obrigatória): você trabalha para a OPOSIÇÃO ao governo federal atual. Não fique em cima do
muro nem proponha nada neutro ou conciliador. Em primeiro lugar, as providências são de CRÍTICA, COBRANÇA e
FISCALIZAÇÃO incisiva do governo atual, de seus ministérios, órgãos e aliados: pedidos de informação duros e
específicos, convocação de ministros, denúncia a órgãos de controle, nota pública de crítica, proposta que corrija
a falha. Em segundo lugar, a DEFESA de valores caros à oposição: segurança pública e combate ao crime, liberdade de
expressão e contra a censura, família, vida, liberdade econômica, menos impostos e responsabilidade fiscal, defesa
do agro e da propriedade, combate à corrupção. Seja incisivo no tom, mas sempre ancorado nos fatos da notícia:
não invente números, nomes ou acusações que a notícia não sustente."""

ALT_REGRAS = """Ofereça de 2 a 3 ALTERNATIVAS de providência, com instrumentos DIFERENTES, para o analista escolher o
melhor caminho. Cada "acao" tem NO MÁXIMO 20 palavras: diga a quem se dirige e o que se cobra ou propõe, direto, sem
rodeios e sem repetir a notícia. Ordene da mais forte para a mais simples."""

SISTEMA = f"""Você é um analista político especializado em assessorar a Liderança da Oposição na {CASA} do Brasil, acompanhando o mandato de um(a) parlamentar específico(a).

Sua tarefa é analisar uma notícia que cita o(a) parlamentar ou um tema acompanhado pelo gabinete e decidir se ela é relevante para uma ação parlamentar dele(a). O escopo é amplo: além de política, também interessam notícias de segurança pública, saúde, educação, economia, crimes, corrupção, direitos civis e qualquer tema que possa fundamentar uma ação parlamentar — inclusive defender o(a) parlamentar de uma notícia negativa ou aproveitar uma positiva.

{VIES}

{ALT_REGRAS}

Responda APENAS com JSON puro, sem markdown, sem texto antes ou depois, sem blocos de código.

Formato obrigatório:
{{"relevante": true, "categoria": "Saúde pública", "alternativas": [{{"tipo_acao": "Requerimento de informações", "acao": "Até 20 palavras"}}, {{"tipo_acao": "Nota à imprensa", "acao": "Até 20 palavras"}}], "justificativa": "Uma frase curta"}}

Categorias possíveis:
{chr(10).join('- "' + c + '"' for c in CATEGORIAS)}

Tipos de ação parlamentar:
{chr(10).join('- "' + t + '"' for t in TIPOS_ACAO)}

Marque relevante=true sempre que a notícia revelar falha do governo, omissão de órgão público, dado preocupante sobre serviço público, escândalo, crime com envolvimento de agente público, ameaça a valores da oposição, algo que envolva diretamente o(a) parlamentar, ou qualquer situação que justifique fiscalização parlamentar.

Se não for relevante: {{"relevante": false, "categoria": null, "alternativas": [], "justificativa": "Breve motivo"}}"""


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


MAX_PALAVRAS_ALT = 28          # folga sobre as 20 pedidas; acima disso a frase é cortada


def _curta(t):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    palavras = t.split()
    return t if len(palavras) <= MAX_PALAVRAS_ALT else " ".join(palavras[:MAX_PALAVRAS_ALT]).rstrip(",;:") + "…"


def normalizar_alternativas(lista, chave_inst="tipo_acao", maximo=3):
    """[{instrumento, acao}] limpas: sem vazias, sem instrumento repetido, frases curtas."""
    out, vistos = [], set()
    for a in lista if isinstance(lista, list) else []:
        if not isinstance(a, dict):
            continue
        inst = _txt(a.get(chave_inst) or a.get("instrumento") or a.get("tipo_acao"), 80)
        acao = _curta(a.get("acao") or a.get("acao_sugerida"))
        if not acao or (inst or "").lower() in vistos:
            continue
        vistos.add((inst or "").lower())
        out.append({"instrumento": inst, "acao": acao[:400]})
        if len(out) == maximo:
            break
    return out


def alternativas_de(row, inst="tipo_acao", acao="acao_sugerida"):
    """Alternativas de uma notícia/tópico; registros antigos viram uma alternativa só."""
    try:
        alts = json.loads(row.get("alternativas") or "[]")
    except ValueError:
        alts = []
    if alts:
        return alts
    return [{"instrumento": row.get(inst), "acao": row.get(acao)}] if row.get(acao) else []


def texto_alternativas(alts, titulo="Alternativas sugeridas pela IA (o analista escolhe o caminho)"):
    if not alts:
        return None
    return titulo + ":\n" + "\n".join(f"{i}. {a['instrumento'] + ': ' if a.get('instrumento') else ''}{a['acao']}"
                                       for i, a in enumerate(alts, 1))


def analisar(codigo, limite=MAX_ANALISES, sessao=None, ids=None):
    """Analisa as notícias ainda não analisadas do parlamentar (ou só as de `ids`). Devolve quantas analisou."""
    import questoes
    if not questoes.configurado():
        return 0
    col = getattr(noticias.c, COL)
    with engine.connect() as c:
        p = c.execute(select(parlamentares).where(parlamentares.c.codigo == codigo)).mappings().first()
        qp = select(noticias).where(col == codigo, noticias.c.analisado_em.is_(None))
        if ids:
            qp = qp.where(noticias.c.id.in_(list(ids)))
        pend = [dict(r) for r in c.execute(qp
                                           .order_by(noticias.c.publicado.desc().nullslast(), noticias.c.id.desc())
                                           .limit(limite)).mappings()]
    if not p:
        return 0
    quem = f"{p['nome']} ({p.get('partido') or 'sem partido'}-{p.get('uf') or ''})"
    n = 0
    for it in pend:
        msg = (f"Parlamentar acompanhado(a): {CARGO} {quem}\n"
               f"Termo que trouxe a notícia: {it['termo']}\n"
               f"Fonte: {it.get('fonte') or ('Google Notícias' if it.get('origem') == 'google' else '—')}"
               f"{' (incluída manualmente pela equipe)' if it.get('origem') == 'manual' else ''}\n"
               f"Título: {it['titulo']}\n"
               f"Resumo: {it.get('resumo') or 'Sem resumo'}\n\n"
               f"Responda APENAS com JSON puro conforme o formato solicitado.")
        try:
            texto = questoes.chamar_ia(msg, sessao, sistema=SISTEMA, modelo_=modelo(), max_tokens=700, timeout=60)
        except Exception as e:
            log.warning("radar: IA indisponível (%s) — o restante fica para o próximo ciclo", e)
            break
        try:
            r = extrair_json(texto)
            alts = normalizar_alternativas(r.get("alternativas"))
            if not alts and r.get("acao_sugerida"):          # resposta no formato antigo
                alts = normalizar_alternativas([{"tipo_acao": r.get("tipo_acao"), "acao": r.get("acao_sugerida")}])
            relevante = bool(r.get("relevante"))
            valores = {"relevante": relevante, "categoria": _txt(r.get("categoria"), 80),
                       "tipo_acao": alts[0]["instrumento"] if relevante and alts else None,
                       "acao_sugerida": alts[0]["acao"] if relevante and alts else None,
                       "alternativas": json.dumps(alts, ensure_ascii=False) if relevante and alts else None,
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


def lista(codigo, termo="", filtro="", limite=200, busca=""):
    col = getattr(noticias.c, COL)
    q = select(noticias).where(col == codigo)
    if termo:
        q = q.where(noticias.c.termo == termo)
    if filtro == "relevantes":
        q = q.where(noticias.c.relevante.is_(True))
    elif filtro in ("portal", "google", "manual"):
        q = q.where(noticias.c.origem == filtro)
    elif filtro == "Pendente":   # relevantes ainda sem andamento + qualquer notícia atribuída e pendente
        q = q.where(or_(and_(noticias.c.relevante.is_(True), func.coalesce(noticias.c.status, "Pendente") == "Pendente"),
                        and_(noticias.c.solicitacao_id.isnot(None), noticias.c.status == "Pendente")))
    elif filtro in STATUS:
        q = q.where(noticias.c.status == filtro)
    q = q.order_by(noticias.c.publicado.desc().nullslast(), noticias.c.id.desc())
    with engine.connect() as c:
        linhas = [dict(r) for r in c.execute(q if busca else q.limit(limite)).mappings()]
    if busca:
        linhas = [n for n in linhas if casa_busca(n, busca)][:limite]
    for n in linhas:
        n["alts"] = alternativas_de(n)
    return linhas


_CAMPOS_BUSCA = ("titulo", "resumo", "fonte", "termo", "link", "categoria", "tipo_acao", "acao_sugerida", "alternativas",
                 "justificativa", "assessor", "acao_efetuada")


def casa_busca(n, busca):
    """Todas as palavras da busca aparecem na notícia (sem diferenciar acento nem maiúsculas)."""
    palavras = norm_nome(busca).split()
    if not palavras:
        return True
    alvo = norm_nome(" ".join(str(n.get(k) or "") for k in _CAMPOS_BUSCA))
    return all(p in alvo for p in palavras)


# ---------------- busca avulsa no Google Notícias e inclusão manual ----------------

def buscar_google(codigo, consulta, buscar=None, dias=30):
    """Pesquisa um termo no Google Notícias na hora; marca as que já estão no radar deste parlamentar."""
    consulta = re.sub(r"\s+", " ", consulta or "").strip()[:200]
    if not consulta:
        return []
    url = ("https://news.google.com/rss/search?q=" + quote(f"{consulta} when:{dias}d")
           + "&hl=pt-BR&gl=BR&ceid=BR:pt-419")
    conteudo = (buscar or baixar)(url)
    itens = parse_feed(conteudo)[:30] if conteudo else []
    col = getattr(noticias.c, COL)
    with engine.connect() as c:
        ja = {r[0]: r[1] for r in c.execute(select(noticias.c.link, noticias.c.id).where(col == codigo))}
    return [{**it, "ja_id": ja.get(it["link"])} for it in itens]


_HEADERS_PAGINA = {**_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                   "Accept-Language": "pt-BR,pt;q=0.9"}


def baixar_pagina(url):
    import requests
    r = requests.get(url, headers=_HEADERS_PAGINA, timeout=15, allow_redirects=True)
    if r.status_code != 200:
        raise ValueError(f"o site respondeu HTTP {r.status_code}")
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text[:600_000], r.url


def _meta(pagina, *nomes):
    for nome in nomes:
        for padrao in (rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(nome)}["\'][^>]*content=["\']([^"\']*)',
                       rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']{re.escape(nome)}["\']'):
            m = re.search(padrao, pagina, re.I)
            if m and m.group(1).strip():
                return html.unescape(m.group(1)).strip()
    return None


def _data_iso(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        return d if d.tzinfo is None else d.astimezone(timezone(timedelta(hours=-3))).replace(tzinfo=None)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", v.strip())
        return datetime.fromisoformat(m.group(1)) if m else None


def metadados(url, baixar_=None):
    """Título, resumo, veículo e data de uma página de notícia (Open Graph / meta tags)."""
    pagina, final = (baixar_ or baixar_pagina)(url)
    titulo = _meta(pagina, "og:title", "twitter:title")
    if not titulo:
        m = re.search(r"<title[^>]*>(.*?)</title>", pagina, re.I | re.S)
        titulo = _limpar_html(m.group(1), 500) if m else None
    fonte = _meta(pagina, "og:site_name", "application-name")
    if titulo and fonte:
        for sep in (" - ", " | ", " – ", " — "):
            if titulo.endswith(sep + fonte):
                titulo = titulo[: -len(sep + fonte)].strip()
    return {"titulo": _limpar_html(titulo, 500) if titulo else None,
            "resumo": _limpar_html(_meta(pagina, "og:description", "description", "twitter:description")) or None,
            "fonte": (fonte or "")[:200] or None,
            "publicado": _data_iso(_meta(pagina, "article:published_time", "datePublished", "pubdate",
                                         "publishdate", "date", "DC.date.issued")),
            "final": final}


def _dominio(url):
    m = re.match(r"https?://(?:www\.)?([^/:?#]+)", url or "")
    return m.group(1) if m else None


def incluir_manual(codigo, f, autor=None, baixar_=None):
    """Inclui uma notícia pelo link. Devolve (id, nova?). Título/resumo/data vêm do formulário ou da página."""
    link = (f.get("link") or "").strip()
    if not re.match(r"^https?://\S+\.\S+", link):
        raise ValueError("Informe o link completo da notícia (começando por http:// ou https://).")
    link = link[:1000]
    col = getattr(noticias.c, COL)
    with engine.connect() as c:
        ja = c.execute(select(noticias.c.id).where(col == codigo, noticias.c.link == link)).scalar()
    if ja:
        return ja, False
    titulo = (f.get("titulo") or "").strip()[:500]
    resumo = (f.get("resumo") or "").strip()[:2000]
    fonte = (f.get("fonte") or "").strip()[:200]
    pub = _data_iso(f.get("publicado") or "")
    meta_ = {}
    if not (titulo and fonte and pub) and "news.google." not in link:     # completa pelo que a página informa
        try:
            meta_ = metadados(link, baixar_)
        except Exception as e:
            log.info("radar: metadados de %s indisponíveis (%s)", link, e)
            if not titulo:
                raise ValueError("Não consegui abrir a página para ler o título. Informe o título manualmente.")
    titulo = titulo or meta_.get("titulo")
    if not titulo:
        raise ValueError("A página não informa o título da notícia. Preencha o título manualmente.")
    tema = re.sub(r"\s+", " ", f.get("termo") or "").strip()[:120] or "Inclusão manual"
    item = {"titulo": titulo, "link": link, "fonte": fonte or meta_.get("fonte") or _dominio(meta_.get("final") or link),
            "publicado": pub or meta_.get("publicado") or datetime.now().replace(microsecond=0),
            "resumo": resumo or meta_.get("resumo")}
    with engine.begin() as c:
        gravar_noticias(c, codigo, tema, [item], origem="manual")
        nid = c.execute(select(noticias.c.id).where(col == codigo, noticias.c.link == link)).scalar()
    log.info("radar: notícia incluída manualmente por %s para %s: %s", autor, codigo, link)
    return nid, True


def analisar_em_segundo_plano(codigo, nid):
    """Análise da IA logo após a inclusão manual, sem esperar o próximo ciclo."""
    import questoes
    if not questoes.configurado():
        return False

    def rodar():
        try:
            analisar(codigo, limite=1, ids=[nid])
        except Exception:
            log.exception("radar: análise da notícia incluída")
    threading.Thread(target=rodar, daemon=True).start()
    return True


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
                import questoes
                import selecao_ia
                try:
                    import areas
                    areas.classificar()
                except Exception:
                    log.exception("áreas das solicitações")
                if questoes.configurado() and selecao_ia.precisa_gerar(cod):   # 1x por dia, a partir das 7h
                    try:
                        selecao_ia.gerar(cod, sessao=sessao)
                    except ValueError as e:
                        log.info("seleção da IA %s: %s", cod, e)
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
