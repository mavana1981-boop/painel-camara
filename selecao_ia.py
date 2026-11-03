"""Radar → "Seleção da IA".

Uma vez por dia (e pelo botão "Gerar nova seleção"), a IA lê as notícias dos últimos dias de cada tema
acompanhado (o termo que trouxe cada notícia: nome do deputado e temas cadastrados) e escolhe, por contexto,
pelo menos dois tópicos por tema — os assuntos que mais aparecem. Cada tópico vem com resumo, ação sugerida,
as notícias que o sustentam e o tipo:
  - comissao: o caminho é uma providência em comissão (requerimento de audiência pública, convocação ou
    convite de autoridade, fiscalização, pedido de informações tratado na comissão);
  - outros: os demais assuntos.

Cada tópico vira na hora uma solicitação do parlamentar com responsável sorteado entre os servidores da
equipe daquele tipo (equilibrando a carga do lote) — o responsável recebe o aviso de tarefa na tela inicial.
Equipes padrão (editáveis na própria tela): comissão = Lianna; outros = Marcelo, Elyezer, Vinicius,
Garibaldi e André Alencar. Os nomes são casados com os usuários do sistema, preferindo a equipe Oposição.
"""
import json
import logging
import os
import random
import re
from datetime import date, datetime, timedelta

from sqlalchemy import insert, select, update

from db import engine, meta, noticias, norm_nome, selecao_ia, solicitacoes, usuarios

log = logging.getLogger("selecao_ia")

DIAS = int(os.environ.get("SELECAO_DIAS", "7"))              # janela de notícias lidas
MIN_TOPICOS = 2
MAX_POR_TEMA = 40                                             # notícias por tema enviadas à IA
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

SISTEMA = f"""Você assessora a Liderança da Oposição na Câmara dos Deputados. Recebe as manchetes recentes que o radar
coletou para um deputado, separadas por tema acompanhado. Para CADA tema, identifique pelo contexto os ASSUNTOS que
mais aparecem — agrupe as manchetes que tratam do mesmo fato, decisão, crise ou debate — e escolha pelo menos 2 (no
máximo 3) tópicos por tema, priorizando os mais recorrentes e com mais potencial de ação da Oposição.

Regras do tópico:
- O título descreve o fato ou a discussão em poucas palavras, com contexto: "Corte de verbas da Polícia Federal",
  "Alta do preço dos alimentos", "Acordo do governo com a Venezuela". NUNCA use só um nome de pessoa, partido ou
  palavra solta ("Lula", "Flávio", "Governo", "Ministério").
- Indique a área temática, escolhendo uma desta lista: {", ".join(AREAS)}.

Regras da providência (o mais importante):
- Proponha UMA providência concreta e pronta para a equipe executar, escolhendo o instrumento desta lista:
  {"; ".join(INSTRUMENTOS)}.
- Em "acao_sugerida", diga exatamente o que fazer: a quem se dirige, o que se pede ou se propõe e por quê. Ex.:
  "RIC ao Ministério da Justiça pedindo o valor cortado do orçamento da PF em 2026 e os efeitos nas operações de
  fronteira" ou "Nota à imprensa do Líder criticando o acordo, com os números de comércio citados na Folha".
- É PROIBIDO sugerir "avaliar", "analisar", "acompanhar", "monitorar" ou "verificar as notícias".
- tipo = "comissao" quando o instrumento é requerimento de convocação, convite, audiência pública ou fiscalização
  (PFC), ou outro pedido votado em comissão; caso contrário, tipo = "outros".

Responda APENAS com JSON puro, sem texto antes ou depois:
{{"temas": [{{"tema": "<igual ao recebido>", "topicos": [{{"topico": "<título com contexto>", "area": "<da lista>",
"resumo": "<1-2 frases com o fato>", "instrumento": "<da lista>", "acao_sugerida": "<providência concreta>",
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

def _noticias_por_tema(cod, dias):
    corte = datetime.now() - timedelta(days=dias)
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
    """Tópicos de seleções anteriores ainda não concluídos (para a IA não repetir)."""
    with engine.connect() as c:
        rows = c.execute(select(selecao_ia.c.tema, selecao_ia.c.topico)
                         .select_from(selecao_ia.outerjoin(solicitacoes, solicitacoes.c.id == selecao_ia.c.solicitacao_id))
                         .where(selecao_ia.c.codigo_deputado == cod,
                                selecao_ia.c.criado_em >= datetime.now() - timedelta(days=DIAS),
                                solicitacoes.c.status.in_(("aberta", "andamento")))).all()
    return [f"{t} — {p}" for t, p in rows]


def montar_mensagem(nome_deputado, temas, abertos):
    partes = [f"Deputado acompanhado: {nome_deputado}", f"Notícias dos últimos {DIAS} dias, por tema:"]
    for tema, itens in temas.items():
        partes.append(f"\n### Tema: {tema} ({len(itens)} notícias)")
        for n in itens[:MAX_POR_TEMA]:
            res = re.sub(r"\s+", " ", n.get("resumo") or "")[:160]
            partes.append(f"[{n['id']}] {n['titulo']}" + (f" — {res}" if res else ""))
    if abertos:
        partes.append("\nTópicos já distribuídos e ainda em aberto (não repita): " + "; ".join(abertos[:30]))
    partes.append("\nResponda APENAS com o JSON.")
    return "\n".join(partes)


MODELO_PADRAO = "claude-sonnet-5-5"      # agrupar por contexto pede um modelo melhor que o da triagem


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


def _topico_ok(p):
    """Tópico com contexto (não um nome solto) e providência concreta; devolve os campos limpos ou None."""
    titulo = re.sub(r"\s+", " ", str(p.get("topico") or "")).strip()[:200]
    acao = re.sub(r"\s+", " ", str(p.get("acao_sugerida") or "")).strip()[:1500]
    inst = _instrumento(p.get("instrumento"))
    if len(titulo.split()) < 3 or not acao or not inst:
        return None
    if GENERICAS.match(acao) or (GENERICAS.search(acao) and len(acao.split()) < 12):
        return None
    tipo = p.get("tipo") if p.get("tipo") in TIPOS else "outros"
    if any(x in norm_nome(inst) for x in ("convocacao", "convite", "audiencia", "fiscalizacao")):
        tipo = "comissao"
    return {"topico": titulo, "area": _area(p.get("area")) or "Outros", "instrumento": inst,
            "resumo": str(p.get("resumo") or "").strip()[:1500] or None, "acao_sugerida": acao, "tipo": tipo}


def _topicos_validos(d, temas):
    return sum(1 for t in d.get("temas") or [] for p in (t.get("topicos") or []) if _topico_ok(p)) >= min(2, 2 * len(temas))


def gerar(cod, autor="Seleção da IA", sessao=None, rnd=random, agora=None):
    """Gera um lote de tópicos para o deputado e cria as solicitações. Devolve {lote, topicos}."""
    import paineis
    import questoes
    import meu
    import radar as rd
    agora = agora or meu.agora_brt()          # lote e datas no horário de Brasília
    marca = datetime.now()
    s = paineis.deputado(cod) or {}
    temas = _noticias_por_tema(cod, DIAS)
    if not temas:
        raise ValueError(f"Não há notícias dos últimos {DIAS} dias para selecionar tópicos.")
    if not questoes.configurado():
        raise ValueError("A Seleção da IA precisa da chave da Anthropic: configure ANTHROPIC_API_KEY no Railway.")
    msg = montar_mensagem(s.get("nome") or str(cod), temas, _abertos(cod))
    dados, erro = None, None
    for tentativa in range(2):                      # uma nova tentativa se a resposta vier quebrada ou genérica
        try:
            texto = questoes.chamar_ia(msg if not tentativa else msg + "\n\nATENÇÃO: a resposta anterior veio inválida "
                                       "ou com providências genéricas. Siga o formato e as regras à risca.", sessao,
                                       sistema=SISTEMA, modelo_=os.environ.get("SELECAO_MODEL") or MODELO_PADRAO,
                                       max_tokens=5000, timeout=150)
            d = rd.extrair_json(texto)
            if isinstance(d.get("temas"), list) and _topicos_validos(d, temas):
                dados = d
                break
            erro = "a resposta não trouxe tópicos com providências concretas"
        except Exception as e:
            erro = str(e)
            log.warning("seleção da IA: %s", e)
    if not dados:
        raise ValueError(f"A IA não devolveu uma seleção utilizável ({erro}). Tente de novo em alguns minutos.")
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
    resps = sortear([t["tipo"] for t in topicos], equipes(), rnd)
    lote = agora.isoformat(timespec="seconds")
    hoje = agora.date()
    prazo = (hoje + timedelta(days=PRAZO_DIAS)).isoformat()
    with engine.begin() as c:
        links = {r[0]: (r[1], r[2]) for r in c.execute(select(noticias.c.id, noticias.c.titulo, noticias.c.link)
                                                         .where(noticias.c.id.in_([i for t in topicos for i in t["noticias"]] or [0])))}
        for t, resp in zip(topicos, resps):
            linhas = [f"Tema acompanhado: {t['tema']} · Área: {t['area']} · {TIPOS[t['tipo']]}", t["resumo"],
                      f"Providência sugerida ({t['instrumento']}): {t['acao_sugerida']}"]
            if t["noticias"]:
                linhas.append("Notícias:\n" + "\n".join(f"- {links[i][0]} — {links[i][1]}" for i in t["noticias"] if i in links))
            sid = c.execute(insert(solicitacoes).values(
                codigo_deputado=cod, titulo=("Seleção da IA: " + t["topico"])[:200],
                descricao="\n\n".join(x for x in linhas if x), solicitante="Seleção da IA (radar)", status="aberta",
                data_solicitacao=hoje.isoformat(), prazo=prazo, responsavel=resp, area=t["area"],
                atribuido_por=autor if resp else None, atribuido_em=marca if resp else None,
                criado_por=autor, criado_em=marca, atualizado_em=marca)).inserted_primary_key[0]
            c.execute(insert(selecao_ia).values(codigo_deputado=cod, lote=lote, tema=t["tema"], topico=t["topico"],
                                                resumo=t["resumo"], tipo=t["tipo"], acao_sugerida=t["acao_sugerida"],
                                                area=t["area"], instrumento=t["instrumento"],
                                                noticias=json.dumps(t["noticias"]), responsavel=resp,
                                                solicitacao_id=sid, criado_em=marca))
            if t["noticias"]:          # as notícias passam a seguir a solicitação (se ainda não tinham uma)
                c.execute(update(noticias).where(noticias.c.id.in_(t["noticias"]), noticias.c.solicitacao_id.is_(None))
                          .values(solicitacao_id=sid))
            t["responsavel"], t["solicitacao_id"] = resp, sid
    log.info("seleção da IA do deputado %s: %d tópico(s)", cod, len(topicos))
    return {"lote": lote, "topicos": topicos}


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

def ultimo_lote(cod):
    """Tópicos da seleção mais recente, por tema, com o andamento da solicitação e as notícias."""
    with engine.connect() as c:
        lote = c.execute(select(selecao_ia.c.lote).where(selecao_ia.c.codigo_deputado == cod)
                         .order_by(selecao_ia.c.lote.desc()).limit(1)).scalar()
        if not lote:
            return None
        rows = [dict(r) for r in c.execute(
            select(selecao_ia, solicitacoes.c.status.label("status"), solicitacoes.c.responsavel.label("resp_atual"))
            .select_from(selecao_ia.outerjoin(solicitacoes, solicitacoes.c.id == selecao_ia.c.solicitacao_id))
            .where(selecao_ia.c.codigo_deputado == cod, selecao_ia.c.lote == lote)
            .order_by(selecao_ia.c.id)).mappings()]
        ids = {i for r in rows for i in json.loads(r["noticias"] or "[]")}
        nots = {r["id"]: dict(r) for r in c.execute(select(noticias.c.id, noticias.c.titulo, noticias.c.link,
                                                           noticias.c.fonte, noticias.c.publicado)
                                                    .where(noticias.c.id.in_(ids or [0]))).mappings()}
    temas = {}
    for r in rows:
        r["noticias_l"] = [nots[i] for i in json.loads(r["noticias"] or "[]") if i in nots]
        r["responsavel"] = r["resp_atual"] or r["responsavel"]
        temas.setdefault(r["tema"], []).append(r)
    return {"lote": lote, "gerado_em": datetime.fromisoformat(lote), "temas": temas, "total": len(rows)}


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
