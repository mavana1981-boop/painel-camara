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

SISTEMA = """Você assessora a Liderança da Oposição na Câmara dos Deputados. Recebe as manchetes recentes que o radar
de notícias coletou para um deputado, separadas por tema acompanhado. Para CADA tema, identifique pelo contexto os
assuntos (tópicos) que mais aparecem nas notícias — agrupe manchetes que tratam do mesmo fato ou discussão — e
escolha pelo menos 2 (no máximo 3) tópicos por tema, priorizando os mais recorrentes e com mais potencial de ação
parlamentar da Oposição. Para cada tópico indique o tipo:
- "comissao": quando a providência natural é em comissão (requerimento de audiência pública, convocação ou convite
  de ministro/autoridade, fiscalização e controle, requerimento de informação a ser votado na comissão);
- "outros": demais providências (discurso, projeto, nota, requerimento de informação direto, articulação, imprensa).
Responda APENAS com JSON puro, sem texto antes ou depois, no formato:
{"temas": [{"tema": "<igual ao recebido>", "topicos": [{"topico": "<título curto>", "resumo": "<1-2 frases>",
"tipo": "comissao|outros", "acao_sugerida": "<ação concreta>", "noticias": [<ids das notícias do tópico>]}]}]}"""


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


def _fallback(temas):
    """Sem resposta utilizável da IA: as palavras mais frequentes das manchetes de cada tema (aviso na tela)."""
    pare = set("a o as os de da do das dos e em no na nos nas para por com que se um uma ao aos sobre diz apos após "
               "contra mais como sem ser foi é tem vai pelo pela entre ate até ja já não nao deve após".split())
    out = []
    for tema, itens in temas.items():
        cont = {}
        for n in itens:
            for t in set(norm_nome(n["titulo"]).split()):
                if len(t) > 3 and t not in pare and t not in norm_nome(tema).split():
                    cont[t] = cont.get(t, 0) + 1
        top = sorted(cont, key=lambda t: -cont[t])[:MIN_TOPICOS]
        tops = []
        for t in top:
            ids = [n["id"] for n in itens if t in norm_nome(n["titulo"]).split()][:8]
            tops.append({"topico": t.capitalize(), "resumo": f"{len(ids)} notícia(s) citam “{t}”.", "tipo": "outros",
                         "acao_sugerida": "Avaliar as notícias e propor a providência cabível.", "noticias": ids})
        out.append({"tema": tema, "topicos": tops})
    return {"temas": out}


def gerar(cod, autor="Seleção da IA", sessao=None, rnd=random, agora=None):
    """Gera um lote de tópicos para o deputado e cria as solicitações. Devolve {lote, topicos, sem_ia}."""
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
    sem_ia = False
    dados = None
    if questoes.configurado():
        try:
            texto = questoes.chamar_ia(montar_mensagem(s.get("nome") or str(cod), temas, _abertos(cod)), sessao,
                                       sistema=SISTEMA, modelo_=os.environ.get("SELECAO_MODEL") or rd.modelo(),
                                       max_tokens=4000, timeout=120)
            dados = rd.extrair_json(texto)
        except Exception as e:
            log.warning("seleção da IA: %s", e)
    if not dados or not isinstance(dados.get("temas"), list):
        dados, sem_ia = _fallback(temas), True
    validos = {n["id"]: (tema, n) for tema, itens in temas.items() for n in itens}
    topicos = []
    for t in dados["temas"]:
        tema = str(t.get("tema") or "").strip()
        if tema not in temas:      # a IA pode reescrever o nome do tema: casa pelo normalizado
            tema = next((x for x in temas if norm_nome(x) == norm_nome(tema)), None)
        if not tema:
            continue
        for p in (t.get("topicos") or [])[:3]:
            ids = [int(i) for i in (p.get("noticias") or []) if str(i).isdigit() and int(i) in validos][:12]
            titulo = str(p.get("topico") or "").strip()[:200]
            if not titulo:
                continue
            topicos.append({"tema": tema, "topico": titulo, "resumo": str(p.get("resumo") or "").strip()[:1500] or None,
                            "tipo": p.get("tipo") if p.get("tipo") in TIPOS else "outros",
                            "acao_sugerida": str(p.get("acao_sugerida") or "").strip()[:1500] or None, "noticias": ids})
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
            linhas = [f"Tema: {t['tema']} · {TIPOS[t['tipo']]}", t["resumo"],
                      f"Ação sugerida pela IA: {t['acao_sugerida']}" if t["acao_sugerida"] else None]
            if t["noticias"]:
                linhas.append("Notícias:\n" + "\n".join(f"- {links[i][0]} — {links[i][1]}" for i in t["noticias"] if i in links))
            sid = c.execute(insert(solicitacoes).values(
                codigo_deputado=cod, titulo=("Seleção da IA: " + t["topico"])[:200],
                descricao="\n\n".join(x for x in linhas if x), solicitante="Seleção da IA (radar)", status="aberta",
                data_solicitacao=hoje.isoformat(), prazo=prazo, responsavel=resp,
                atribuido_por=autor if resp else None, atribuido_em=marca if resp else None,
                criado_por=autor, criado_em=marca, atualizado_em=marca)).inserted_primary_key[0]
            c.execute(insert(selecao_ia).values(codigo_deputado=cod, lote=lote, tema=t["tema"], topico=t["topico"],
                                                resumo=t["resumo"], tipo=t["tipo"], acao_sugerida=t["acao_sugerida"],
                                                noticias=json.dumps(t["noticias"]), responsavel=resp,
                                                solicitacao_id=sid, criado_em=marca))
            if t["noticias"]:          # as notícias passam a seguir a solicitação (se ainda não tinham uma)
                c.execute(update(noticias).where(noticias.c.id.in_(t["noticias"]), noticias.c.solicitacao_id.is_(None))
                          .values(solicitacao_id=sid))
            t["responsavel"], t["solicitacao_id"] = resp, sid
    log.info("seleção da IA do deputado %s: %d tópico(s)%s", cod, len(topicos), " (sem IA)" if sem_ia else "")
    return {"lote": lote, "topicos": topicos, "sem_ia": sem_ia}


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
