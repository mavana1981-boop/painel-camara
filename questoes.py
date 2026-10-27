"""Questões de ordem elaboradas por IA (Claude, via API da Anthropic) com base no Regimento Interno.

- O administrador envia o texto do Regimento Interno da Câmara dos Deputados (PDF ou TXT) uma vez;
  ele fica guardado na tabela `documentos` (chave "ricd").
- Para cada pedido, o app separa os artigos mais relacionados à situação descrita (busca por
  palavras) e sempre inclui os dispositivos sobre questão de ordem. Só esse trecho vai para a IA,
  o que deixa a resposta mais fiel ao texto e o custo baixo.
- Sem o Regimento cadastrado, a IA responde com o próprio conhecimento e é instruída a avisar que
  os dispositivos citados precisam ser conferidos.

Variáveis de ambiente: ANTHROPIC_API_KEY (obrigatória para gerar) e ANTHROPIC_MODEL (opcional).
"""
import io
import logging
import math
import os
import re
import threading
import unicodedata
from collections import Counter
from datetime import datetime

import requests
from sqlalchemy import delete, insert, select, update

from db import documentos, engine, questoes_ordem

log = logging.getLogger("questoes")

API_URL = "https://api.anthropic.com/v1/messages"
MODELO_PADRAO = "claude-sonnet-5-5"
LIMITE_TRECHO = 70_000          # caracteres do Regimento enviados por pedido
CHAVE_RICD = "ricd"
# Questão de ordem e reclamação no RICD (arts. 95 e 96): sempre enviados à IA.
ARTIGOS_QO = range(95, 97)

_STOP = set("""a o e é de da do das dos em no na nos nas um uma uns umas para por pelo pela pelos pelas
com sem que se ao aos à às ou como mais menos sua seu suas seus este esta isto esse essa isso aquele
aquela foi ser são está estão sobre entre quando onde qual quais não sim já também muito pode podem
deve devem ter tem têm deputado deputada deputados camara câmara presidente sessão federal artigo art""".split())


def modelo():
    return os.environ.get("ANTHROPIC_MODEL", MODELO_PADRAO)


def configurado():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------- Regimento ----------------

def extrair_texto(nome_arquivo, conteudo: bytes) -> str:
    nome = (nome_arquivo or "").lower()
    if nome.endswith(".pdf") or conteudo[:4] == b"%PDF":
        from pypdf import PdfReader
        leitor = PdfReader(io.BytesIO(conteudo))
        return "\n".join((p.extract_text() or "") for p in leitor.pages)
    for cod in ("utf-8", "latin-1"):
        try:
            return conteudo.decode(cod)
        except UnicodeDecodeError:
            continue
    return ""


def salvar_regimento(nome_arquivo, conteudo: bytes):
    texto = extrair_texto(nome_arquivo, conteudo)
    texto = re.sub(r"[ \t]+", " ", texto)
    if len(artigos(texto)) < 50:
        raise ValueError("Não encontrei os artigos no arquivo. Envie o texto integral do Regimento "
                         "(PDF com texto selecionável ou TXT).")
    with engine.begin() as c:
        c.execute(delete(documentos).where(documentos.c.chave == CHAVE_RICD))
        c.execute(insert(documentos).values(chave=CHAVE_RICD, nome=nome_arquivo, texto=texto,
                                            atualizado=datetime.now()))
    return len(artigos(texto))


def regimento():
    with engine.connect() as c:
        r = c.execute(select(documentos).where(documentos.c.chave == CHAVE_RICD)).mappings().first()
    return dict(r) if r else None


def artigos(texto):
    """Divide o texto em artigos: [(numero, texto_do_artigo), ...]."""
    marcas = list(re.finditer(r"(?m)^\s*Art\.\s*(\d{1,3})(?:[ºo°]|\b)", texto or ""))
    out = []
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        out.append((int(m.group(1)), texto[m.start():fim].strip()))
    return out


def _tokens(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return [t for t in re.findall(r"[a-z]{4,}", s) if t not in _STOP]


def selecionar_trechos(texto, situacao, limite=LIMITE_TRECHO):
    """Artigos de questão de ordem + os mais parecidos com a situação (TF-IDF simples)."""
    arts = artigos(texto)
    if not arts:
        return []
    docs = [Counter(_tokens(t)) for _, t in arts]
    df = Counter(w for d in docs for w in d)
    n = len(docs)
    consulta = set(_tokens(situacao))
    notas = []
    for i, d in enumerate(docs):
        nota = sum((1 + math.log(d[w])) * math.log(n / df[w]) for w in consulta if w in d)
        notas.append((nota, i))
    escolhidos = {i for i, (num, _) in enumerate(arts) if num in ARTIGOS_QO}
    for nota, i in sorted(notas, reverse=True):
        if nota <= 0 or len(escolhidos) >= 40:
            break
        escolhidos.add(i)
    out, total = [], 0
    for i in sorted(escolhidos, key=lambda i: (arts[i][0] not in ARTIGOS_QO, -notas[i][0])):
        t = arts[i][1][:6000]
        if total + len(t) > limite:
            continue
        out.append(arts[i])
        total += len(t)
    return sorted(out)


# ---------------- IA ----------------

SISTEMA = """Você é assessor(a) técnico(a) legislativo(a) especialista no Regimento Interno da Câmara dos Deputados (RICD) e no processo legislativo brasileiro. Sua tarefa é redigir uma QUESTÃO DE ORDEM para um(a) deputado(a) federal apresentar, a partir da situação descrita pela equipe.

Regras:
1. Fundamente apenas em dispositivos que realmente existem. Quando houver trechos do Regimento abaixo, cite somente artigos que aparecem nesses trechos e transcreva entre aspas o essencial do dispositivo. Se um dispositivo necessário não estiver nos trechos, diga isso expressamente em vez de inventar.
2. No RICD, a questão de ordem trata de dúvida sobre a interpretação do Regimento (na sua prática exclusiva ou relacionada com a Constituição) e deve indicar com precisão os dispositivos regimentais ou constitucionais cuja observância se pretende elucidar. Confira nos trechos enviados as regras dos arts. 95 (questão de ordem) e 96 (reclamação) — momento, prazo para formular, quem decide e recurso — e respeite-as. Se a situação se encaixar melhor numa reclamação (art. 96) do que numa questão de ordem, diga isso.
3. Seja objetivo e use linguagem parlamentar formal.
4. Não invente precedentes, decisões da Presidência ou números de questões de ordem anteriores. Se mencionar a possibilidade de precedentes, recomende que a equipe confirme.

Responda exatamente nestas seções, com estes títulos:
## Cabimento
(1 a 3 frases: se cabe questão de ordem, em que momento e perante quem — Plenário ou comissão.)
## Questão de ordem (texto para leitura)
(O texto que o(a) deputado(a) lerá, começando por "Senhor(a) Presidente, com fundamento no art. ... do Regimento Interno, formulo a seguinte questão de ordem:" e terminando com o pedido concreto à Presidência.)
## Fundamentação
(Dispositivos citados, com o trecho relevante entre aspas e a explicação de como se aplicam.)
## Pedido
(O que se pede à Presidência, em itens curtos.)
## Pontos de atenção
(Riscos, contra-argumentos prováveis e o que conferir antes de apresentar.)"""


def montar_pedido(situacao, deputado_nome=None):
    reg = regimento()
    if reg:
        trechos = selecionar_trechos(reg["texto"], situacao)
        contexto = "\n\n".join(t for _, t in trechos)
        base = (f"Trechos do Regimento Interno da Câmara dos Deputados (texto enviado pela equipe: {reg['nome']}):\n"
                f"<regimento>\n{contexto}\n</regimento>")
    else:
        trechos = []
        base = ("ATENÇÃO: o texto do Regimento não foi cadastrado no sistema. Use seu conhecimento do RICD "
                "e, na seção 'Pontos de atenção', avise que todos os dispositivos citados devem ser "
                "conferidos no texto oficial.")
    quem = f"Deputado(a): {deputado_nome}\n" if deputado_nome else ""
    usuario = f"{base}\n\n{quem}Situação descrita pela equipe:\n<situacao>\n{situacao.strip()}\n</situacao>"
    return usuario, [n for n, _ in trechos]


def chamar_ia(mensagem_usuario, sessao=None, sistema=None, modelo_=None, max_tokens=4000, timeout=180):
    chave = os.environ.get("ANTHROPIC_API_KEY")
    if not chave:
        raise RuntimeError("ANTHROPIC_API_KEY não configurada no Railway.")
    corpo = {"model": modelo_ or modelo(), "max_tokens": max_tokens, "system": sistema or SISTEMA,
             "messages": [{"role": "user", "content": mensagem_usuario}]}
    r = (sessao or requests).post(API_URL, json=corpo, timeout=timeout, headers={
        "x-api-key": chave, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:300])
        except ValueError:
            msg = r.text[:300]
        raise RuntimeError(f"A API da Anthropic respondeu {r.status_code}: {msg}")
    dados = r.json()
    return "".join(b.get("text", "") for b in dados.get("content", []) if b.get("type") == "text").strip()


def criar_pedido(codigo_deputado, autor, situacao):
    with engine.begin() as c:
        res = c.execute(insert(questoes_ordem).values(
            codigo_deputado=codigo_deputado, autor=autor, situacao=situacao.strip(), resposta=None,
            modelo=modelo(), criado_em=datetime.now()))
        return res.inserted_primary_key[0]


def processar(qid, deputado_nome=None, sessao=None):
    """Gera a resposta (roda em segundo plano para não prender a página)."""
    with engine.connect() as c:
        q = c.execute(select(questoes_ordem).where(questoes_ordem.c.id == qid)).mappings().first()
    if not q:
        return
    try:
        msg, arts = montar_pedido(q["situacao"], deputado_nome)
        texto = chamar_ia(msg, sessao)
        if arts:
            texto += ("\n\n---\nArtigos do Regimento enviados à IA: "
                      + ", ".join(str(a) for a in arts) + ".")
    except Exception as e:
        log.exception("questão de ordem %s", qid)
        texto = f"[ERRO] {e}"
    with engine.begin() as c:
        c.execute(update(questoes_ordem).where(questoes_ordem.c.id == qid).values(resposta=texto))


def processar_em_segundo_plano(qid, deputado_nome=None):
    threading.Thread(target=processar, args=(qid, deputado_nome), daemon=True).start()


def historico(codigo_deputado, limite=30):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(select(questoes_ordem).where(
            questoes_ordem.c.codigo_deputado == codigo_deputado)
            .order_by(questoes_ordem.c.criado_em.desc(), questoes_ordem.c.id.desc()).limit(limite)).mappings()]
