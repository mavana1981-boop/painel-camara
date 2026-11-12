"""Radar → "Seleção da IA".

Uma vez por dia (a partir das 7h) e pelo botão "Gerar nova seleção", a IA lê as notícias das últimas 72 horas
de cada tema acompanhado (o termo que trouxe cada notícia: nome do deputado e temas cadastrados), agrupa as que
tratam do mesmo fato e escolhe os assuntos mais comentados, espalhando os tópicos pelo maior número possível de
temas e áreas do parlamentar. Cada tópico vem com área, resumo, providência concreta, as notícias que o
sustentam e o tipo:
  - comissao: o caminho é uma providência em comissão (requerimento de audiência pública, convocação ou
    convite de autoridade, fiscalização, pedido de informações tratado na comissão);
  - outros: os demais assuntos.

Cada rodada forma um LOTE com o dia e a hora em que foi gerado. Os tópicos ficam em "Seleção da IA — pendentes
de abertura": a solicitação só é aberta quando um usuário confirma (um tópico ou vários de uma vez), com
responsável escolhido ou sorteado entre os servidores da equipe daquele tipo (equilibrando a carga). Também
dá para descartar o que não interessa.
Equipes padrão (editáveis na própria tela): comissão = Lianna; outros = Marcelo, Elyezer, Vinicius,
Garibaldi e André Alencar. Os nomes são casados com os usuários do sistema, preferindo a equipe Oposição.
"""
import json
import logging
import os
import random
import re
import threading
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, insert, or_, select, update

from db import anexos, engine, meta, noticias, norm_nome, selecao_ia, solicitacoes, usuarios

log = logging.getLogger("selecao_ia")

HORAS = int(os.environ.get("SELECAO_HORAS", "72"))           # janela de notícias lidas
DIAS = 7                                                      # tópicos recentes que a IA não deve repetir
MAX_LOTES = 15                                                # lotes mostrados na tela
MIN_TOPICOS = 2
MAX_TOKENS = int(os.environ.get("SELECAO_MAX_TOKENS", "16000"))  # resposta da IA (JSON longo)
MAX_POR_TEMA = 40                                             # notícias por tema enviadas à IA
RESERVA_POR_TEMA = int(os.environ.get("SELECAO_RESERVA_POR_TEMA", "12"))   # versão enxuta para as IAs de reserva
RESERVA_TOTAL = int(os.environ.get("SELECAO_RESERVA_TOTAL", "60"))
RESERVA_MAX_TOKENS = 6000
PRAZO_DIAS = int(os.environ.get("SELECAO_PRAZO_DIAS", "3"))   # prazo das solicitações criadas
TIPOS = {"comissao": "Comissão", "outros": "Outros assuntos"}
EQUIPES_PADRAO = {"comissao": ["Lianna"], "outros": ["Marcelo", "Elyezer", "Vinicius", "Garibaldi", "André Alencar"]}
CHAVE_EQUIPES = "selecao_ia:equipes"

AREAS = ["Saúde", "Segurança Pública", "Educação", "Economia", "Tributação", "Previdência", "Trabalho",
         "Política Externa", "Defesa", "Agropecuária", "Meio Ambiente", "Infraestrutura", "Energia",
         "Justiça e Direitos", "Família e Costumes", "Liberdade de Expressão", "Comunicação e Redes",
         "Corrupção e Transparência", "Política e Eleições", "Administração Pública", "Assistência Social",
         "Ciência e Tecnologia", "Cultura e Esporte", "Outros"]
INSTRUMENTOS = ["Requerimento de Informação (RIC)", "Requerimento de convocação de ministro",
                "Requerimento de convite de autoridade", "Requerimento de audiência pública",
                "Requerimento de fiscalização e controle (PFC)", "Projeto de Lei (PL)", "Projeto de Decreto Legislativo (PDL)",
                "Proposta de Emenda à Constituição (PEC)", "Emenda a proposição em tramitação", "Indicação ao Executivo",
                "Representação ao MPF/TCU/PGR", "Nota à imprensa", "Discurso em Plenário", "Post para redes sociais",
                "Ofício a órgão público", "Pedido de CPI (coleta de assinaturas)"]
GENERICAS = re.compile(r"\b(avaliar|analisar|acompanhar|monitorar|verificar|estudar|observar|levantar informa)", re.I)

from radar import VIES  # noqa: E402  (mesma linha editorial de oposição do radar)

SISTEMA = f"""Você assessora a Liderança da Oposição na Câmara dos Deputados. Recebe as manchetes das últimas 72 horas
que o radar coletou para um deputado, separadas por tema acompanhado (as áreas de interesse do parlamentar), com o
veículo de cada uma. Agrupe pelo contexto as manchetes que tratam do mesmo fato, decisão, crise ou debate e escolha
os ASSUNTOS MAIS COMENTADOS — os que aparecem em mais manchetes e em mais veículos diferentes — com potencial de
ação da Oposição.

Distribuição (importante):
- Espalhe os tópicos pelo MAIOR NÚMERO POSSÍVEL de temas acompanhados e de áreas temáticas diferentes; não
  concentre a seleção num só tema ou numa só área.
- Para cada tema com notícias, escolha 2 tópicos (no máximo 3); se o tema tiver um único assunto, 1 basta.
- Prefira um assunto muito comentado de uma área ainda não coberta a um terceiro tópico da mesma área.

Regras do tópico:
- O título descreve o fato ou a discussão em poucas palavras, com contexto: "Corte de verbas da Polícia Federal",
  "Alta do preço dos alimentos", "Acordo do governo com a Venezuela". NUNCA use só um nome de pessoa, partido ou
  palavra solta ("Lula", "Flávio", "Governo", "Ministério").
- Indique a área temática, escolhendo uma desta lista: {", ".join(AREAS)}.

{VIES}

Regras das providências (o mais importante):
- Para cada tópico, ofereça de 2 a 3 ALTERNATIVAS com instrumentos DIFERENTES desta lista, para o analista escolher
  o melhor caminho: {"; ".join(INSTRUMENTOS)}.
- Cada "acao" tem NO MÁXIMO 20 palavras: a quem se dirige e o que se cobra ou propõe, direto. Ex.:
  "RIC ao Ministério da Justiça cobrando o valor cortado da PF e o impacto nas fronteiras" ou
  "Nota do Líder denunciando o acordo e os prejuízos ao comércio brasileiro".
- Ordene da mais forte (cobrança, convocação, denúncia) para a mais simples.
- É PROIBIDO sugerir "avaliar", "analisar", "acompanhar", "monitorar" ou "verificar as notícias".
- tipo = "comissao" quando a PRIMEIRA alternativa é requerimento de convocação, convite, audiência pública ou
  fiscalização (PFC), ou outro pedido votado em comissão; caso contrário, tipo = "outros".

Responda APENAS com JSON puro, sem texto antes ou depois:
{{"temas": [{{"tema": "<igual ao recebido>", "topicos": [{{"topico": "<título com contexto>", "area": "<da lista>",
"resumo": "<1-2 frases com o fato>", "alternativas": [{{"instrumento": "<da lista>", "acao": "<até 20 palavras>"}}],
"tipo": "comissao|outros", "noticias": [<ids das notícias do tópico>]}}]}}]}}"""


# ---------------- equipes ----------------

def _tokens(u):
    return set(re.split(r"[\s._-]+", norm_nome((u.get("login") or "").replace(".", " ") + " " + (u.get("nome") or ""))))


def casar_nome(nome, usuarios_ativos):
    """Login do usuário que corresponde ao nome (ex.: 'André Alencar'); None se não achar ou houver empate."""
    alvo = [t for t in norm_nome(nome).split() if t]
    if not alvo:
        return None

    def casa(u, folga):
        toks = _tokens(u)
        return all(any(t.startswith(a) or (folga and len(a) >= 5 and t[:4] == a[:4]) for t in toks) for a in alvo)

    for folga in (False, True):                       # exato pelo começo; depois tolera grafia (Elyezer/Elyesley)
        cand = [u for u in usuarios_ativos if casa(u, folga)]
        if len(cand) > 1:
            op = [u for u in cand if u.get("equipe") == "oposicao"]
            cand = op if op else cand
        if len(cand) == 1:
            return cand[0]["login"]
        if cand:
            return None
    return None


def _ativos():
    with engine.connect() as c:
        return [dict(r) for r in c.execute(select(usuarios.c.login, usuarios.c.nome, usuarios.c.equipe)
                                           .where(usuarios.c.ativo.is_(True))).mappings()]


def equipes():
    """{"comissao": [logins], "outros": [logins]} — a salva na tela ou a padrão casada pelos nomes."""
    with engine.connect() as c:
        v = c.execute(select(meta.c.valor).where(meta.c.chave == CHAVE_EQUIPES)).scalar()
    ativos = {u["login"] for u in _ativos()}
    if v:
        try:
            d = json.loads(v)
            return {k: [x for x in d.get(k, []) if x in ativos] for k in TIPOS}
        except ValueError:
            pass
    us = _ativos()
    return {k: [x for x in (casar_nome(n, us) for n in nomes) if x] for k, nomes in EQUIPES_PADRAO.items()}


def nao_encontrados():
    """Nomes da equipe padrão que não casaram com nenhum usuário (para avisar na tela)."""
    with engine.connect() as c:
        if c.execute(select(meta.c.valor).where(meta.c.chave == CHAVE_EQUIPES)).scalar():
            return []
    us = _ativos()
    return [n for nomes in EQUIPES_PADRAO.values() for n in nomes if not casar_nome(n, us)]


def salvar_equipes(comissao, outros):
    ativos = {u["login"] for u in _ativos()}
    d = {"comissao": [x for x in comissao if x in ativos], "outros": [x for x in outros if x in ativos]}
    with engine.begin() as c:
        c.execute(meta.delete().where(meta.c.chave == CHAVE_EQUIPES))
        c.execute(insert(meta).values(chave=CHAVE_EQUIPES, valor=json.dumps(d)))
    return d


def sortear(tipos_dos_topicos, eq, rnd=random):
    """Responsáveis sorteados, equilibrando a carga: cada um recebe de novo só depois que todos receberam."""
    filas, out = {}, []
    for tipo in tipos_dos_topicos:
        pool = eq.get(tipo) or eq.get("outros") or eq.get("comissao") or []
        if not pool:
            out.append(None)
            continue
        fila = filas.setdefault(tipo, [])
        if not fila:
            fila.extend(rnd.sample(pool, len(pool)))
        out.append(fila.pop())
    return out


SORTEAR = "__sortear__"     # valor do campo responsável que pede o sorteio
_COMISSAO = re.compile(r"convoca|convite|convidar|audiencia publica|fiscalizacao e controle|\bpfc\b|comissao", re.I)


def tipo_do_texto(texto):
    """'comissao' quando o pedido é providência de comissão (convocação, convite, audiência, PFC); senão 'outros'."""
    return "comissao" if _COMISSAO.search(norm_nome(texto or "")) else "outros"


def sortear_um(texto, rnd=random):
    """(tipo, login) para um pedido avulso: equipe do tipo, entre quem tem menos solicitações em aberto."""
    from sqlalchemy import func
    tipo = tipo_do_texto(texto)
    eq = equipes()
    pool = eq.get(tipo) or eq.get("outros") or eq.get("comissao") or []
    if not pool:
        return tipo, None
    with engine.connect() as c:
        carga = dict(c.execute(select(solicitacoes.c.responsavel, func.count())
                               .where(solicitacoes.c.responsavel.in_(pool),
                                      solicitacoes.c.status.in_(("aberta", "andamento")))
                               .group_by(solicitacoes.c.responsavel)).all())
    menor = min(carga.get(x, 0) for x in pool)
    return tipo, rnd.choice([x for x in pool if carga.get(x, 0) == menor])


# ---------------- geração ----------------

def _noticias_por_tema(cod, horas):
    corte = datetime.now() - timedelta(hours=horas)
    with engine.connect() as c:
        rows = [dict(r) for r in c.execute(
            select(noticias.c.id, noticias.c.termo, noticias.c.titulo, noticias.c.resumo, noticias.c.fonte,
                   noticias.c.publicado, noticias.c.link)
            .where(noticias.c.codigo_deputado == cod, noticias.c.publicado >= corte)
            .order_by(noticias.c.publicado.desc())).mappings()]
    temas = {}
    for r in rows:
        temas.setdefault(r["termo"] or "Outros", []).append(r)
    return temas


def _abertos(cod):
    """Tópicos recentes ainda pendentes de abertura ou com solicitação em andamento (para a IA não repetir)."""
    with engine.connect() as c:
        rows = c.execute(select(selecao_ia.c.tema, selecao_ia.c.topico)
                         .select_from(selecao_ia.outerjoin(solicitacoes, solicitacoes.c.id == selecao_ia.c.solicitacao_id))
                         .where(selecao_ia.c.codigo_deputado == cod,
                                selecao_ia.c.criado_em >= datetime.now() - timedelta(days=DIAS),
                                or_(selecao_ia.c.status.in_(("pendente", "prelista")),
                                    solicitacoes.c.status.in_(("aberta", "andamento"))))).all()
    return [f"{t} — {p}" for t, p in rows]


def montar_mensagem(nome_deputado, temas, abertos, por_tema=None, com_resumo=True, total=None):
    """Mensagem para a IA. Versão enxuta (por_tema/total menores, sem resumos) para as IAs de reserva, que têm
    limites de tokens por minuto bem menores que o Gemini."""
    por_tema = por_tema or MAX_POR_TEMA
    usadas = 0
    partes = [f"Deputado acompanhado: {nome_deputado}",
              f"Temas acompanhados (áreas de interesse): {', '.join(temas)}",
              f"Notícias das últimas {HORAS} horas, por tema:"]
    for tema, itens in temas.items():
        veiculos = len({(n.get("fonte") or "").strip().lower() for n in itens if n.get("fonte")})
        partes.append(f"\n### Tema: {tema} ({len(itens)} notícias, {veiculos} veículos)")
        for n in itens[:por_tema]:
            if total and usadas >= total:
                break
            usadas += 1
            res = re.sub(r"\s+", " ", n.get("resumo") or "")[:160] if com_resumo else ""
            partes.append(f"[{n['id']}] {n['titulo']}" + (f" ({n['fonte']})" if n.get("fonte") else "")
                          + (f" — {res}" if res else ""))
    if abertos:
        partes.append("\nTópicos já selecionados e ainda pendentes ou em andamento (não repita): " + "; ".join(abertos[:30]))
    partes.append("\nResponda APENAS com o JSON.")
    return "\n".join(partes)




def _area(v):
    v = str(v or "").strip()
    return next((a for a in AREAS if norm_nome(a) == norm_nome(v)), None) or ("Outros" if v else None)


def _instrumento(v):
    """Instrumento da lista a partir do que a IA escreveu ("RIC", "PL", "Nota à imprensa"...)."""
    n = norm_nome(str(v or ""))
    if not n:
        return None
    for i in INSTRUMENTOS:
        base = norm_nome(i.split(" (")[0])
        sigla = norm_nome(i[i.index("(") + 1:i.index(")")]) if "(" in i else None
        if n in (norm_nome(i), base, sigla) or (sigla and n.split()[0] == sigla):
            return i
    return next((i for i in INSTRUMENTOS if norm_nome(i.split(" (")[0]) in n or n in norm_nome(i)), None)


def _alt_ok(inst_txt, acao_txt):
    from radar import _curta
    acao = _curta(acao_txt)[:400]
    inst = _instrumento(inst_txt)
    if not acao or not inst:
        return None
    if GENERICAS.match(acao) or (GENERICAS.search(acao) and len(acao.split()) < 12):
        return None
    return {"instrumento": inst, "acao": acao}


def _topico_ok(p):
    """Tópico com contexto (não um nome solto) e ao menos uma providência concreta; devolve os campos limpos ou None.
    As alternativas (2 a 3, instrumentos diferentes) vêm em "alternativas"; respostas antigas, em
    instrumento/acao_sugerida."""
    titulo = re.sub(r"\s+", " ", str(p.get("topico") or "")).strip()[:200]
    if len(titulo.split()) < 3:
        return None
    brutas = p.get("alternativas") if isinstance(p.get("alternativas"), list) else []
    if not brutas and p.get("acao_sugerida"):
        brutas = [{"instrumento": p.get("instrumento"), "acao": p.get("acao_sugerida")}]
    alts, vistos = [], set()
    for a in brutas:
        if not isinstance(a, dict):
            continue
        ok = _alt_ok(a.get("instrumento") or a.get("tipo_acao"), a.get("acao") or a.get("acao_sugerida"))
        if ok and ok["instrumento"] not in vistos:
            vistos.add(ok["instrumento"])
            alts.append(ok)
    alts = alts[:3]
    if not alts:
        return None
    inst = alts[0]["instrumento"]
    tipo = p.get("tipo") if p.get("tipo") in TIPOS else "outros"
    if any(x in norm_nome(inst) for x in ("convocacao", "convite", "audiencia", "fiscalizacao")):
        tipo = "comissao"
    return {"topico": titulo, "area": _area(p.get("area")) or "Outros", "instrumento": inst,
            "resumo": str(p.get("resumo") or "").strip()[:1500] or None, "acao_sugerida": alts[0]["acao"],
            "alternativas": alts, "tipo": tipo}


def _topicos_validos(d, temas):
    return sum(1 for t in d.get("temas") or [] for p in (t.get("topicos") or []) if _topico_ok(p)) >= min(2, 2 * len(temas))


def _verificar(cod):
    """Confere, antes de chamar a IA, se há o que gerar. Devolve as notícias por tema."""
    import questoes
    temas = _noticias_por_tema(cod, HORAS)
    if not temas:
        raise ValueError(f"Não há notícias das últimas {HORAS} horas para selecionar tópicos.")
    if not questoes.configurado():
        raise ValueError("A Seleção da IA precisa de uma chave de IA: configure GEMINI_API_KEY (ou ZAI_API_KEY / GROQ_API_KEY / "
                         "CF_ACCOUNT_ID + CF_API_TOKEN) no Railway.")
    return temas


# ---------------- geração pelo botão (em segundo plano) ----------------
# A chamada à IA pode levar mais de 2 minutos (até duas tentativas de 150 s). Feita dentro da requisição, o
# gunicorn (--timeout 120) matava o processo antes de gravar o lote e nada aparecia. Agora o botão dispara a
# geração numa thread; a página mostra o andamento e o resultado (ou o erro) da última rodada.
ESPERA_BOTAO = 20                       # segundos que a requisição aguarda antes de responder "em andamento"
_estado_lock = threading.Lock()
_estado = {}                            # cod -> {"rodando": bool, "inicio", "fim", "erro", "lote", "n"}


def estado(cod):
    with _estado_lock:
        return dict(_estado.get(cod) or {})


def gerar_em_segundo_plano(cod, autor="Seleção da IA", esperar=ESPERA_BOTAO):
    """Dispara a geração sem prender a página. Devolve o estado: se terminar em até `esperar` segundos, já vem
    com o lote (ou o erro); senão, rodando=True. ValueError imediato se não houver notícias ou chave."""
    import meu
    _verificar(cod)
    with _estado_lock:
        atual = _estado.get(cod) or {}
        if atual.get("rodando"):
            return dict(atual)
        _estado[cod] = {"rodando": True, "inicio": meu.agora_brt()}

    def rodar():
        fim = {"rodando": False}
        try:
            r = gerar(cod, autor=autor)
            fim.update(lote=r["lote"], n=len(r["topicos"]), gerado_em=r["gerado_em"], provedor=r.get("provedor"), reserva=r.get("reserva"))
        except Exception as e:                    # noqa: BLE001 — o erro vai para a tela
            log.exception("seleção da IA do deputado %s (botão)", cod)
            fim["erro"] = str(e) if isinstance(e, ValueError) else f"Falha inesperada: {e}"
        with _estado_lock:
            fim["inicio"] = (_estado.get(cod) or {}).get("inicio")
            fim["fim"] = meu.agora_brt()
            _estado[cod] = fim

    t = threading.Thread(target=rodar, daemon=True)
    t.start()
    if esperar:
        t.join(esperar)
    return estado(cod)


def _descartar(ruins, quem, principal):
    """Resposta inutilizável: a próxima tentativa vai para a IA seguinte da cadeia. O Gemini principal ganha uma
    segunda chance (com o aviso de formato); a Z.ai, quando vem na frente, passa a vez para o Gemini."""
    if quem and not (quem == principal and quem.startswith("gemini:")) and quem not in ruins:
        ruins.append(quem)


def gerar(cod, autor="Seleção da IA", sessao=None, rnd=random, agora=None):
    """Gera um lote de tópicos para o deputado, PENDENTES DE ABERTURA (nenhuma solicitação é criada aqui).
    Devolve {lote, gerado_em, topicos}."""
    import paineis
    import questoes
    import meu
    import radar as rd
    agora = agora or meu.agora_brt()          # lote e datas no horário de Brasília
    marca = datetime.now()
    s = paineis.deputado(cod) or {}
    temas = _verificar(cod)
    abertos = _abertos(cod)
    msg = montar_mensagem(s.get("nome") or str(cod), temas, abertos)
    msg_curta = montar_mensagem(s.get("nome") or str(cod), temas, abertos[:10], por_tema=RESERVA_POR_TEMA,
                                com_resumo=False, total=RESERVA_TOTAL)
    dados, erro, provedor = None, None, None
    modelo_sel = questoes.modelo_env("SELECAO_MODEL", questoes.modelo())
    preferidas = questoes.preferidas_radar()
    cad = questoes.cadeia(modelo_sel, preferidas)
    principal = cad[0][0] if cad else f"gemini:{modelo_sel}"
    aviso = ""
    ruins = []                                      # IAs de reserva que já devolveram resposta inutilizável
    for tentativa in range(3):                      # novas tentativas se a resposta vier quebrada ou genérica
        texto = ""
        quem = None
        try:
            questoes.ULTIMA.stop_reason = None
            questoes.ULTIMA.provedor = None
            texto = questoes.chamar_ia(msg + aviso, sessao, sistema=SISTEMA, modelo_=modelo_sel,
                                       max_tokens=MAX_TOKENS, timeout=240, pular=ruins,
                                       mensagem_reserva=msg_curta + aviso, max_tokens_reserva=RESERVA_MAX_TOKENS,
                                       preferir=preferidas)
            quem = getattr(questoes.ULTIMA, "provedor", None)
            d = rd.extrair_json(texto)
            if isinstance(d.get("temas"), list) and _topicos_validos(d, temas):
                dados = d
                provedor = quem
                break
            log.warning("seleção da IA (tentativa %d, %s): tópicos inválidos | início da resposta: %r",
                        tentativa + 1, quem, (texto or "")[:500])
            _descartar(ruins, quem, principal)
            erro = "a resposta não trouxe tópicos com providências concretas"
            aviso = ("\n\nATENÇÃO: a resposta anterior veio com providências genéricas ou sem tópicos válidos. "
                     "Siga o formato e as regras à risca.")
        except Exception as e:
            parada = getattr(questoes.ULTIMA, "stop_reason", None)
            if parada == "max_tokens":
                erro = "a resposta da IA foi cortada por ser longa demais"
                aviso = ("\n\nATENÇÃO: a resposta anterior ficou longa demais e foi cortada. Seja CONCISO: no máximo "
                         "2 tópicos por tema, resumo de UMA frase e 2 alternativas por tópico.")
            elif texto:                              # respondeu fora do formato: na próxima, outra IA de reserva
                _descartar(ruins, quem or getattr(questoes.ULTIMA, "provedor", None), principal)
                inicio = re.sub(r"\s+", " ", texto)[:120]
                erro = f"a IA não respondeu em JSON — começou com: {inicio!r}"
                aviso = ("\n\nATENÇÃO: a resposta anterior não veio em JSON. Responda SOMENTE com o objeto JSON, "
                         "começando por { e terminando por }, sem nenhum texto antes ou depois.")
            else:
                erro = str(e)
            log.warning("seleção da IA (tentativa %d, parada=%s): %s | início da resposta: %r",
                        tentativa + 1, parada, e, (texto or "")[:500])
            if not texto and tentativa >= 1:         # nenhuma IA respondeu duas vezes: não insistir
                break
    if not dados:
        cota = "429" in (erro or "") or "sem cota" in (erro or "")
        raise ValueError(f"A IA não devolveu uma seleção utilizável ({erro}). "
                         + ("As cotas gratuitas de IA acabaram por hoje; configure uma IA de reserva ou tente "
                            "mais tarde." if cota else "Tente de novo em alguns minutos."))
    validos = {n["id"]: (tema, n) for tema, itens in temas.items() for n in itens}
    topicos = []
    for t in dados["temas"]:
        tema = str(t.get("tema") or "").strip()
        if tema not in temas:      # a IA pode reescrever o nome do tema: casa pelo normalizado
            tema = next((x for x in temas if norm_nome(x) == norm_nome(tema)), None)
        if not tema:
            continue
        for p in (t.get("topicos") or [])[:3]:
            ok = _topico_ok(p)
            if not ok:
                continue
            ids = [int(i) for i in (p.get("noticias") or []) if str(i).isdigit() and int(i) in validos][:12]
            topicos.append({"tema": tema, **ok, "noticias": ids})
    if not topicos:
        raise ValueError("A IA não devolveu tópicos utilizáveis. Tente de novo em alguns minutos.")
    lote = agora.isoformat(timespec="seconds")
    with engine.begin() as c:
        while c.execute(select(selecao_ia.c.id).where(selecao_ia.c.codigo_deputado == cod,
                                                      selecao_ia.c.lote == lote).limit(1)).first():
            agora += timedelta(seconds=1)                 # dois lotes no mesmo segundo não se misturam
            lote = agora.isoformat(timespec="seconds")
        for t in topicos:
            t["id"] = c.execute(insert(selecao_ia).values(
                codigo_deputado=cod, lote=lote, tema=t["tema"], topico=t["topico"], resumo=t["resumo"], tipo=t["tipo"],
                acao_sugerida=t["acao_sugerida"], area=t["area"], instrumento=t["instrumento"],
                alternativas=json.dumps(t["alternativas"], ensure_ascii=False),
                noticias=json.dumps(t["noticias"]), status="pendente", criado_em=marca)).inserted_primary_key[0]
    log.info("seleção da IA do deputado %s: lote %s com %d tópico(s) pendentes de abertura", cod, lote, len(topicos))
    return {"lote": lote, "gerado_em": agora, "topicos": topicos, "provedor": provedor,
            "reserva": bool(provedor) and provedor != principal}


# ---------------- confirmação humana ----------------

def alternativas(t):
    from radar import alternativas_de
    return alternativas_de(t, inst="instrumento")


def _descricao(c, t, escolha=None):
    from radar import texto_alternativas
    ids = json.loads(t["noticias"] or "[]")
    links = {r[0]: (r[1], r[2]) for r in c.execute(select(noticias.c.id, noticias.c.titulo, noticias.c.link)
                                                     .where(noticias.c.id.in_(ids or [0])))}
    lote = datetime.fromisoformat(t["lote"]).strftime("%d/%m/%Y às %H:%M") if t.get("lote") else ""
    linhas = [f"Tema acompanhado: {t['tema']} · Área: {t['area'] or 'Outros'} · {TIPOS.get(t['tipo'], 'Outros assuntos')}"
              + (f" · Seleção da IA de {lote}" if lote else ""),
              t["resumo"]]
    alts = alternativas(t)
    if escolha is not None and 0 <= escolha < len(alts):
        a = alts[escolha]
        rot = f" ({a['instrumento']})" if a.get("instrumento") else ""
        linhas.append(f"Caminho escolhido{rot}: {a['acao']}")
        outras = [x for i, x in enumerate(alts) if i != escolha]
        if outras:
            linhas.append(texto_alternativas(outras, "Outras alternativas sugeridas pela IA"))
    else:
        linhas.append(texto_alternativas(alts))
    if ids:
        linhas.append("Notícias:\n" + "\n".join(f"- {links[i][0]} — {links[i][1]}" for i in ids if i in links))
    return "\n\n".join(x for x in linhas if x)


def abrir(cod, ids, autor, responsaveis=None, rnd=random, agora=None, escolhas=None):
    """Abre a solicitação dos tópicos pendentes escolhidos (confirmação humana). `responsaveis` = {id: login,
    "" (sem responsável) ou SORTEAR}; o que não vier é sorteado pela equipe do tipo. Devolve [(id, solicitação)]."""
    import meu
    responsaveis = responsaveis or {}
    agora = agora or meu.agora_brt()
    marca = datetime.now()
    ids = [int(i) for i in ids if str(i).isdigit()]
    with engine.connect() as c:
        tops = [dict(r) for r in c.execute(select(selecao_ia).where(
            selecao_ia.c.codigo_deputado == cod, selecao_ia.c.id.in_(ids or [0]),
            selecao_ia.c.status == "pendente").order_by(selecao_ia.c.id)).mappings()]
    if not tops:
        return []
    ativos = {u["login"] for u in _ativos()}
    sortear_ = [t for t in tops if responsaveis.get(t["id"], SORTEAR) == SORTEAR]
    sorteados = dict(zip([t["id"] for t in sortear_], sortear([t["tipo"] for t in sortear_], equipes(), rnd)))
    hoje = agora.date()
    prazo = (hoje + timedelta(days=PRAZO_DIAS)).isoformat()
    feitos = []
    with engine.begin() as c:
        for t in tops:
            escolhido = responsaveis.get(t["id"], SORTEAR)
            resp = sorteados.get(t["id"]) if escolhido == SORTEAR else (escolhido if escolhido in ativos else None)
            ok = c.execute(update(selecao_ia).where(selecao_ia.c.id == t["id"], selecao_ia.c.status == "pendente")
                           .values(status="aberta", decidido_por=autor, decidido_em=marca, responsavel=resp)).rowcount
            if not ok:                                   # outra pessoa confirmou ao mesmo tempo
                continue
            sid = c.execute(insert(solicitacoes).values(
                codigo_deputado=cod, titulo=("Seleção da IA: " + t["topico"])[:200],
                descricao=_descricao(c, t, (escolhas or {}).get(t["id"])),
                solicitante="Seleção da IA (radar)", status="aberta", data_solicitacao=hoje.isoformat(), prazo=prazo,
                responsavel=resp, area=t["area"], atribuido_por=autor if resp else None,
                atribuido_em=marca if resp else None, criado_por=autor, criado_em=marca,
                atualizado_em=marca)).inserted_primary_key[0]
            c.execute(update(selecao_ia).where(selecao_ia.c.id == t["id"]).values(solicitacao_id=sid))
            nids = json.loads(t["noticias"] or "[]")
            if nids:          # as notícias passam a seguir a solicitação (se ainda não tinham uma)
                c.execute(update(noticias).where(noticias.c.id.in_(nids), noticias.c.solicitacao_id.is_(None))
                          .values(solicitacao_id=sid))
            feitos.append((t["id"], sid))
    log.info("seleção da IA do deputado %s: %d solicitação(ões) aberta(s) por %s", cod, len(feitos), autor)
    return feitos


def descartar(cod, ids, autor):
    """Tira tópicos pendentes da fila sem abrir solicitação. Devolve quantos."""
    ids = [int(i) for i in ids if str(i).isdigit()]
    with engine.begin() as c:
        return c.execute(update(selecao_ia).where(selecao_ia.c.codigo_deputado == cod, selecao_ia.c.id.in_(ids or [0]),
                                                  selecao_ia.c.status == "pendente")
                         .values(status="descartada", decidido_por=autor, decidido_em=datetime.now())).rowcount


def restaurar(cod, ids):
    """Volta tópicos descartados para pendentes de abertura."""
    ids = [int(i) for i in ids if str(i).isdigit()]
    with engine.begin() as c:
        return c.execute(update(selecao_ia).where(selecao_ia.c.codigo_deputado == cod, selecao_ia.c.id.in_(ids or [0]),
                                                  selecao_ia.c.status == "descartada")
                         .values(status="pendente", decidido_por=None, decidido_em=None)).rowcount


CHAVE_DEVOLVIDAS = "selecao_ia:devolvidas_pendentes"


def devolver_abertas_pela_ia():
    """Uma vez só: as solicitações que a IA abriu sozinha (versão anterior) e que ninguém tocou — ainda
    "aberta", sem anexo nem resumo do que foi feito — saem de Solicitações e voltam a ser tópicos pendentes de
    abertura, no lote em que foram geradas. As que alguém já trabalhou ficam como estão. Devolve quantas."""
    with engine.begin() as c:
        if c.execute(select(meta.c.valor).where(meta.c.chave == CHAVE_DEVOLVIDAS)).first():
            return 0
        rows = c.execute(select(selecao_ia.c.id, selecao_ia.c.solicitacao_id)
                         .where(selecao_ia.c.status.is_(None))).all()
        n = 0
        for rid, sid in rows:
            st = c.execute(select(solicitacoes.c.status, solicitacoes.c.resumo_realizado)
                           .where(solicitacoes.c.id == sid)).first() if sid else None
            tem_anexo = sid and c.execute(select(anexos.c.id).where(anexos.c.solicitacao_id == sid).limit(1)).first()
            if st and (st[0] != "aberta" or st[1] or tem_anexo):
                c.execute(update(selecao_ia).where(selecao_ia.c.id == rid).values(status="aberta"))
                continue
            if sid:
                c.execute(update(noticias).where(noticias.c.solicitacao_id == sid).values(solicitacao_id=None))
                c.execute(delete(solicitacoes).where(solicitacoes.c.id == sid))
            c.execute(update(selecao_ia).where(selecao_ia.c.id == rid)
                      .values(status="pendente", solicitacao_id=None, responsavel=None))
            n += 1
        c.execute(insert(meta).values(chave=CHAVE_DEVOLVIDAS, valor=datetime.now().isoformat(timespec="seconds")))
    if n:
        log.info("seleção da IA: %d solicitação(ões) abertas pela IA voltaram a ser pendentes de abertura", n)
    return n


def limpar_selecao_por_palavras():
    """Apaga o que a versão anterior gerou sem IA (tópicos de uma palavra só, providência genérica), desde que a
    solicitação ainda esteja aberta, sem anexos e sem resumo. Roda na subida do app; não mexe em nada trabalhado."""
    from db import anexos
    from sqlalchemy import delete
    with engine.begin() as c:
        rows = c.execute(select(selecao_ia.c.id, selecao_ia.c.solicitacao_id).where(
            selecao_ia.c.acao_sugerida == "Avaliar as notícias e propor a providência cabível.")).all()
        n = 0
        for rid, sid in rows:
            if sid:
                st = c.execute(select(solicitacoes.c.status, solicitacoes.c.resumo_realizado)
                               .where(solicitacoes.c.id == sid)).first()
                tem_anexo = c.execute(select(anexos.c.id).where(anexos.c.solicitacao_id == sid).limit(1)).first()
                if st and (st[0] != "aberta" or st[1] or tem_anexo):
                    continue
                c.execute(update(noticias).where(noticias.c.solicitacao_id == sid).values(solicitacao_id=None))
                c.execute(delete(solicitacoes).where(solicitacoes.c.id == sid))
            c.execute(delete(selecao_ia).where(selecao_ia.c.id == rid))
            n += 1
    if n:
        log.info("seleção da IA: %d tópico(s) gerados por palavras soltas foram removidos", n)
    return n


# ---------------- leitura ----------------

def n_pendentes(cod):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(selecao_ia).where(
            selecao_ia.c.codigo_deputado == cod, selecao_ia.c.status == "pendente")).scalar() or 0


def lotes(cod, limite=MAX_LOTES):
    """Lotes da Seleção da IA, do mais recente ao mais antigo: sempre o último gerado e os que ainda têm tópico
    pendente de abertura. Cada lote traz o dia e a hora em que foi gerado, os tópicos por tema e as contagens."""
    with engine.connect() as c:
        ultimo = c.execute(select(selecao_ia.c.lote).where(selecao_ia.c.codigo_deputado == cod)
                           .order_by(selecao_ia.c.lote.desc()).limit(1)).scalar()
        if not ultimo:
            return []
        com_pend = [r[0] for r in c.execute(select(selecao_ia.c.lote).where(
            selecao_ia.c.codigo_deputado == cod, selecao_ia.c.status == "pendente").distinct())]
        escolhidos = sorted(set(com_pend) | {ultimo}, reverse=True)[:limite]
        rows = [dict(r) for r in c.execute(
            select(selecao_ia, solicitacoes.c.status.label("status_sol"), solicitacoes.c.responsavel.label("resp_atual"))
            .select_from(selecao_ia.outerjoin(solicitacoes, solicitacoes.c.id == selecao_ia.c.solicitacao_id))
            .where(selecao_ia.c.codigo_deputado == cod, selecao_ia.c.lote.in_(escolhidos))
            .order_by(selecao_ia.c.id)).mappings()]
        ids = {i for r in rows for i in json.loads(r["noticias"] or "[]")}
        nots = {r["id"]: dict(r) for r in c.execute(select(noticias.c.id, noticias.c.titulo, noticias.c.link,
                                                           noticias.c.fonte, noticias.c.publicado)
                                                    .where(noticias.c.id.in_(ids or [0]))).mappings()}
    por_lote = {}
    for r in rows:
        r["noticias_l"] = [nots[i] for i in json.loads(r["noticias"] or "[]") if i in nots]
        r["responsavel"] = r["resp_atual"] or r["responsavel"]
        r["status"] = r["status"] or ("aberta" if r["solicitacao_id"] else "pendente")
        r["alts"] = alternativas(r)
        por_lote.setdefault(r["lote"], []).append(r)
    out = []
    for lote in escolhidos:
        itens = por_lote.get(lote, [])
        temas = {}
        for r in itens:
            if r["status"] != "descartada":
                temas.setdefault(r["tema"], []).append(r)
        conta = {k: sum(1 for r in itens if r["status"] == k) for k in ("pendente", "aberta", "descartada", "prelista")}
        out.append({"lote": lote, "gerado_em": datetime.fromisoformat(lote), "temas": temas, "total": len(itens),
                    "areas": len({r["area"] for r in itens if r["area"]}), "ultimo": lote == ultimo,
                    "descartados": [r for r in itens if r["status"] == "descartada"], **conta})
    return out


def ultimo_lote(cod):
    """Compatibilidade: o lote mais recente (mesmo formato de lotes())."""
    ls = lotes(cod, limite=1)
    return ls[0] if ls else None


def precisa_gerar(cod, agora=None):
    """Uma seleção por dia, a partir das 7h (horário de Brasília)."""
    import meu
    agora = agora or meu.agora_brt()
    if agora.hour < 7:
        return False
    with engine.connect() as c:
        lote = c.execute(select(selecao_ia.c.lote).where(selecao_ia.c.codigo_deputado == cod)
                         .order_by(selecao_ia.c.lote.desc()).limit(1)).scalar()
    return not lote or lote[:10] < agora.date().isoformat()
