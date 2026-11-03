"""Área temática das solicitações (Saúde, Segurança Pública, Política Externa...).

A Seleção da IA já cria a solicitação com a área. Para as demais (registradas à mão, vindas do radar ou
anteriores a esta função), a área é classificada automaticamente: pela IA, em lote, a partir do título e da
descrição; sem a chave da IA, por palavras-chave de cada área. Quem quiser pode corrigir em "Editar".
Roda na subida do app, a cada ciclo do radar e logo depois de registrar uma solicitação sem área.
"""
import json
import logging
import re
import threading

from sqlalchemy import select, update

from db import engine, norm_nome, solicitacoes

log = logging.getLogger("areas")
_lock = threading.Lock()

PALAVRAS = {
    "Saúde": "saude sus hospital vacina medic doenca epidemi dengue anvisa enfermag ubs remedio farmac",
    "Segurança Pública": "seguranca policia crime faccao presidio penitenc violencia homicid trafico arma pf prf "
                         "guarda municipal milicia",
    "Educação": "educacao escola ensino professor universidade enem mec aluno creche",
    "Economia": "economia inflacao juros pib emprego mercado preco dolar fiscal orcamento gasto divida banco central",
    "Tributação": "imposto tribut taxa iof icms ipi irpf reforma tributaria carga",
    "Previdência": "previdencia inss aposentad pensao beneficio previdenc",
    "Trabalho": "trabalho trabalhador sindicato clt salario minimo emprego greve",
    "Política Externa": "itamaraty embaixada venezuela israel china eua estados unidos russia ucrania onu mercosul "
                        "diplomac externa tarifa exterior",
    "Defesa": "defesa exercito marinha aeronautica militar forcas armadas",
    "Agropecuária": "agro agricult pecuar rural safra mst produtor fazenda invasao de terra",
    "Meio Ambiente": "ambiente ambiental desmat amazonia clima queimada ibama licenciamento",
    "Infraestrutura": "infraestrutura rodovia ferrovia porto aeroporto obra saneamento transporte",
    "Energia": "energia petrobras combustivel gasolina diesel eletric aneel petroleo",
    "Justiça e Direitos": "stf justica judiciario ministro do stf supremo direitos humanos tribunal decisao judicial",
    "Família e Costumes": "familia aborto genero ideologia lgbt crianca adolescente drogas costumes religios",
    "Liberdade de Expressão": "censura liberdade de expressao jornalista imprensa fake news desinformacao",
    "Comunicação e Redes": "redes sociais plataforma big tech internet regulacao das redes",
    "Corrupção e Transparência": "corrupcao desvio fraude licitacao tcu cgu transparencia propina lavagem",
    "Política e Eleições": "eleicao eleitoral tse candidat partido campanha voto urna",
    "Administração Pública": "servidor publico ministerio cargo nomeacao estatal governo federal gestao",
    "Assistência Social": "bolsa familia assistencia social cadunico fome pobreza",
    "Ciência e Tecnologia": "ciencia tecnologia inteligencia artificial pesquisa inovacao",
    "Cultura e Esporte": "cultura esporte lei rouanet futebol olimpi artista",
}

SISTEMA = ("Classifique cada pedido de um gabinete parlamentar em UMA área temática da lista dada. Responda APENAS com "
           "JSON puro no formato {\"areas\": {\"<id>\": \"<área da lista>\"}}.")


def por_palavras(texto):
    n = f" {norm_nome(texto)} "
    pontos = {a: sum(1 for p in ps.split() if f" {p}" in n) for a, ps in PALAVRAS.items()}
    melhor = max(pontos, key=pontos.get)
    return melhor if pontos[melhor] else "Outros"


def classificar(limite=40, sessao=None, esperar=False):
    """Dá área às solicitações que ainda não têm. Devolve quantas foram classificadas."""
    import questoes
    import selecao_ia as si
    if not _lock.acquire(blocking=esperar):
        return 0
    try:
        with engine.connect() as c:
            pend = [dict(r) for r in c.execute(
                select(solicitacoes.c.id, solicitacoes.c.titulo, solicitacoes.c.descricao)
                .where(solicitacoes.c.area.is_(None)).order_by(solicitacoes.c.id.desc()).limit(limite)).mappings()]
        if not pend:
            return 0
        res = {}
        if questoes.configurado():
            linhas = "\n".join("[%s] %s — %s" % (p["id"], p["titulo"], re.sub(r"\s+", " ", p["descricao"] or "")[:300])
                               for p in pend)
            try:
                import radar as rd
                texto = questoes.chamar_ia(f"Áreas possíveis: {', '.join(si.AREAS)}\n\nPedidos:\n{linhas}", sessao,
                                           sistema=SISTEMA, modelo_=rd.modelo(), max_tokens=1500, timeout=60)
                d = rd.extrair_json(texto).get("areas") or {}
                res = {int(k): si._area(v) for k, v in d.items() if str(k).isdigit()}
            except Exception as e:
                log.warning("áreas por IA: %s", e)
        with engine.begin() as c:
            for p in pend:
                area = res.get(p["id"]) or por_palavras(f"{p['titulo']} {p['descricao'] or ''}")
                c.execute(update(solicitacoes).where(solicitacoes.c.id == p["id"], solicitacoes.c.area.is_(None))
                          .values(area=area))
        return len(pend)
    finally:
        _lock.release()


def classificar_em_segundo_plano():
    def _rodar():
        try:
            while classificar():
                pass
        except Exception:
            log.exception("classificação de áreas")
    threading.Thread(target=_rodar, daemon=True).start()
