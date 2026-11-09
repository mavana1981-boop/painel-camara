"""Questões de ordem elaboradas por IA (Gemini, plano gratuito da API do Google) com base no Regimento Interno.

- O administrador envia o texto do Regimento Interno da Câmara dos Deputados (PDF ou TXT) uma vez;
  ele fica guardado na tabela `documentos` (chave "ricd").
- Para cada pedido, o app separa os artigos mais relacionados à situação descrita (busca por
  palavras) e sempre inclui os dispositivos sobre questão de ordem. Só esse trecho vai para a IA,
  o que deixa a resposta mais fiel ao texto e o custo baixo.
- Sem o Regimento cadastrado, a IA responde com o próprio conhecimento e é instruída a avisar que
  os dispositivos citados precisam ser conferidos.

Variáveis de ambiente: GEMINI_API_KEY (obrigatória para gerar; crie em aistudio.google.com) e GEMINI_MODEL
(opcional). `chamar_ia` é a porta única de todas as funções de IA do app (questões de ordem, radar, Seleção da IA e
áreas das solicitações).

Plano gratuito do Gemini: há limite de pedidos por minuto e por dia (por projeto do Google). Por isso as chamadas
passam por uma fila com intervalo mínimo entre elas (GEMINI_INTERVALO, padrão 6 s ≈ 10 por minuto) e, quando o
Google responde 429 (limite por minuto), o app espera o tempo indicado e tenta de novo uma vez. Se o limite do dia
acabar, o erro sobe e o radar deixa o restante para o próximo ciclo, como já fazia quando a IA caía.
"""
import io
import logging
import math
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime

import requests
from sqlalchemy import delete, insert, select, update

from db import documentos, engine, questoes_ordem

log = logging.getLogger("questoes")

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MODELO_PADRAO = "gemini-3.8-flash"            # questões de ordem e Seleção da IA (gratuito)
MODELO_RAPIDO = "gemini-3.5-flash-lite"       # triagem do radar e áreas (gratuito, cotas maiores)
INTERVALO = float(os.environ.get("GEMINI_INTERVALO", "6"))   # segundos entre chamadas (plano gratuito)
FOLGA_RACIOCINIO = 8192       # tokens extras de saída: o "raciocínio" do Gemini conta no limite de saída
ESPERA_MAX_429 = 65           # espera no máximo isso (s) antes de tentar de novo após um 429
LIMITE_TRECHO = 70_000          # caracteres do Regimento enviados por pedido
CHAVE_RICD = "ricd"
# Questão de ordem e reclamação no RICD (arts. 95 e 96): sempre enviados à IA.
ARTIGOS_QO = range(95, 97)

_STOP = set("""a o e é de da do das dos em no na nos nas um uma uns umas para por pelo pela pelos pelas
com sem que se ao aos à às ou como mais menos sua seu suas seus este esta isto esse essa isso aquele
aquela foi ser são está estão sobre entre quando onde qual quais não sim já também muito pode podem
deve devem ter tem têm deputado deputada deputados camara câmara presidente sessão federal artigo art""".split())


def modelo_env(var, padrao):
    """Modelo de uma variável de ambiente; ignora valores antigos da Anthropic (claude-...) ainda no Railway."""
    v = (os.environ.get(var) or "").strip()
    return v if v and not v.lower().startswith("claude") else padrao


def modelo():
    return modelo_env("GEMINI_MODEL", MODELO_PADRAO)


def modelo_rapido():
    return modelo_env("GEMINI_MODEL_RAPIDO", MODELO_RAPIDO)


def chave():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def configurado():
    return bool(chave())


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


_fila = threading.Lock()        # uma chamada por vez, com intervalo mínimo (cota por minuto do plano gratuito)
_ultima_chamada = [0.0]


def _esperar_vez():
    falta = _ultima_chamada[0] + INTERVALO - time.monotonic()
    if falta > 0:
        time.sleep(falta)
    _ultima_chamada[0] = time.monotonic()


def _segundos_retry(dados):
    """Tempo sugerido pelo Google num 429 (RetryInfo.retryDelay, ex.: "17s")."""
    for d in ((dados or {}).get("error") or {}).get("details") or []:
        m = re.match(r"([\d.]+)s", str(d.get("retryDelay") or ""))
        if m:
            return float(m.group(1))
    return None


def chamar_ia(mensagem_usuario, sessao=None, sistema=None, modelo_=None, max_tokens=4000, timeout=180):
    """Envia uma mensagem ao Gemini e devolve o texto. Mesma assinatura de quando era o Claude."""
    k = chave()
    if not k:
        raise RuntimeError("GEMINI_API_KEY não configurada no Railway.")
    mod = modelo_ or modelo()
    corpo = {"systemInstruction": {"parts": [{"text": sistema or SISTEMA}]},
             "contents": [{"role": "user", "parts": [{"text": mensagem_usuario}]}],
             "generationConfig": {"maxOutputTokens": min(max_tokens + FOLGA_RACIOCINIO, 65536)}}
    url = f"{API_BASE}/{mod}:generateContent"
    headers = {"x-goog-api-key": k, "content-type": "application/json"}
    for tentativa in range(2):
        with _fila:
            _esperar_vez()
            r = (sessao or requests).post(url, json=corpo, timeout=timeout, headers=headers)
        if r.status_code == 200:
            break
        try:
            erro = r.json()
        except ValueError:
            erro = {}
        msg = (erro.get("error") or {}).get("message") or (r.text or "")[:300]
        if r.status_code in (429, 503) and tentativa == 0:
            espera = _segundos_retry(erro) or (30 if r.status_code == 429 else 10)
            if espera <= ESPERA_MAX_429:
                log.info("Gemini %s: esperando %.0f s para tentar de novo", r.status_code, espera)
                time.sleep(espera)
                continue
        if r.status_code == 429:
            raise RuntimeError(f"Limite do plano gratuito do Gemini atingido (429): {msg}")
        raise RuntimeError(f"A API do Gemini respondeu {r.status_code}: {msg}")
    try:
        dados = r.json()
    except ValueError:
        raise RuntimeError(f"A API do Gemini devolveu uma resposta que não é JSON: {r.text[:200]!r}")
    cands = dados.get("candidates") or []
    cand = cands[0] if cands else {}
    fim = cand.get("finishReason")
    ULTIMA.stop_reason = "max_tokens" if fim == "MAX_TOKENS" else (fim or "").lower() or None
    ULTIMA.uso = dados.get("usageMetadata")
    partes = (cand.get("content") or {}).get("parts") or []
    texto = "".join(p.get("text", "") for p in partes if not p.get("thought")).strip()
    if not texto:
        bloqueio = (dados.get("promptFeedback") or {}).get("blockReason")
        tipos = "raciocínio" if any(p.get("thought") for p in partes) else "nenhum"
        raise RuntimeError(f"a IA respondeu sem texto (motivo de parada: {ULTIMA.stop_reason or bloqueio}; "
                           f"blocos: {tipos})")
    return texto


ULTIMA = threading.local()      # stop_reason da última chamada desta thread (diagnóstico)


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
