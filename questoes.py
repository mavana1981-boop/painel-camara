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
acabar, `chamar_ia` passa para a reserva: o modelo rápido do Gemini e depois Groq e Cloudflare, quando configurados
(GROQ_API_KEY, CF_ACCOUNT_ID/CF_API_TOKEN). Sem reserva, o erro sobe e o radar deixa o restante para o próximo ciclo.
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
    """Há alguma IA configurada (Gemini ou uma reserva)."""
    return bool(chave() or _zai_ok() or _groq_ok() or _cf_ok())


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


class _CotaEsgotada(RuntimeError):
    """O limite do modelo/provedor acabou (ex.: cota diária do Gemini): passar para a reserva."""


_esgotado = {}                  # "gemini:<modelo>" / "groq" / "cloudflare" -> instante (monotonic) em que volta
_esgotado_lock = threading.Lock()


def _marcar_esgotado(nome, segundos):
    with _esgotado_lock:
        _esgotado[nome] = time.monotonic() + max(60.0, min(float(segundos or 3600), 24 * 3600))


def _disponivel(nome):
    with _esgotado_lock:
        ate = _esgotado.get(nome)
        if ate and ate > time.monotonic():
            return False
        _esgotado.pop(nome, None)
        return True


def _gemini(mod, mensagem_usuario, sessao, sistema, max_tokens, timeout):
    k = chave()
    if not k:
        raise RuntimeError("GEMINI_API_KEY não configurada no Railway.")
    corpo = {"systemInstruction": {"parts": [{"text": sistema}]},
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
        espera = _segundos_retry(erro)
        if r.status_code in (429, 503) and tentativa == 0:
            espera_ = espera or (30 if r.status_code == 429 else 10)
            if espera_ <= ESPERA_MAX_429:
                log.info("Gemini %s: esperando %.0f s para tentar de novo", r.status_code, espera_)
                time.sleep(espera_)
                continue
        if r.status_code == 429:
            # limite que não volta logo (normalmente a cota do dia): não insistir neste modelo até lá
            _marcar_esgotado(f"gemini:{mod}", espera or 3600)
            raise _CotaEsgotada(f"Limite do plano gratuito do Gemini atingido (429, {mod}): {msg}")
        if r.status_code in (500, 502, 503, 504):
            raise _CotaEsgotada(f"A API do Gemini respondeu {r.status_code} ({mod}): {msg}")
        if r.status_code == 404:
            raise _CotaEsgotada(f"Modelo {mod} não encontrado no Gemini (404): {msg}")
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


# ---------------- reservas: Groq e Cloudflare (mesmas chaves do app Pauta Plenário) ----------------
# Usadas só quando os modelos do Gemini falham por cota/indisponibilidade. Configuração no Railway:
#   GROQ_API_KEY (console.groq.com) e, opcional, GROQ_MODEL;
#   CF_ACCOUNT_ID + CF_API_TOKEN (Cloudflare Workers AI) e, opcional, CF_MODEL.
GROQ_BASE = "https://api.groq.com/openai/v1"
# Só modelos grandes o bastante para o JSON da Seleção; modelos fora da lista (ex.: allam-2-7b) nunca são usados.
_GROQ_PREFERENCIA = ["gpt-oss-120b", "kimi-k2", "llama-4-maverick", "llama-4-scout", "llama-4", "llama-3.3-70b",
                     "qwen3", "qwen", "deepseek", "gpt-oss", "llama-3.1-70b"]
GROQ_MAX_SAIDA = 8192
GROQ_MIN_SAIDA = 3000          # abaixo disso a resposta (JSON da Seleção) não cabe: melhor trocar de modelo
# do mais rápido ao mais lento; CF_MODEL fixa um só
CF_MODELOS = ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/meta/llama-4-scout-17b-16e-instruct",
              "@cf/meta/llama-3.1-70b-instruct"]
CF_MAX_SAIDA = 4096
_groq_cache = {"modelo": None}


def _groq_ok():
    return bool(os.environ.get("GROQ_API_KEY"))


def _cf_ok():
    return bool(os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"))


def _groq_modelo(sessao, k, excluir=()):
    fixo = (os.environ.get("GROQ_MODEL") or "").strip()
    if fixo and fixo not in excluir:
        return fixo
    if _groq_cache["modelo"] and _groq_cache["modelo"] not in excluir:
        return _groq_cache["modelo"]
    r = (sessao or requests).get(f"{GROQ_BASE}/models", headers={"Authorization": f"Bearer {k}"}, timeout=20)
    if r.status_code != 200:
        raise _CotaEsgotada(f"Groq /models respondeu {r.status_code}: {(r.text or '')[:200]}")
    ids = [m.get("id", "") for m in (r.json().get("data") or []) if m.get("active", True)]
    ids = [i for i in ids if i not in excluir and not any(x in i.lower() for x in ("whisper", "guard", "tts", "embed"))]
    for sub in _GROQ_PREFERENCIA:
        for i in ids:
            if sub in i.lower():
                _groq_cache["modelo"] = i
                return i
    raise _CotaEsgotada("Groq: nenhum modelo adequado disponível"
                        + (f" (já tentados: {', '.join(excluir)})" if excluir else ""))


def _parada_openai(fim):
    return "max_tokens" if fim == "length" else (fim or "").lower() or None


def _limite_tpm(corpo):
    """Do 413/429 da Groq: (limite, pedido) em tokens por minuto, ex.: "Limit 8000, Requested 15065"."""
    m = re.search(r"Limit (\d+), Requested (\d+)", corpo or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _groq(mensagem_usuario, sessao, sistema, max_tokens, timeout):
    """Groq no plano gratuito: cada modelo tem um limite de tokens por minuto (TPM) que conta o pedido MAIS a saída
    reservada. Num 413 por TPM, reduz a saída se ainda couber uma resposta útil; senão, troca de modelo."""
    k = os.environ["GROQ_API_KEY"]
    excluir, saida = [], min(max_tokens, GROQ_MAX_SAIDA)
    for _ in range(5):
        mod = _groq_modelo(sessao, k, excluir)
        r = (sessao or requests).post(
            f"{GROQ_BASE}/chat/completions", timeout=timeout,
            headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
            json={"model": mod, "temperature": 0.3, "max_tokens": saida,
                  "messages": [{"role": "system", "content": sistema},
                               {"role": "user", "content": mensagem_usuario}]})
        if r.status_code == 200:
            ch = (r.json().get("choices") or [{}])[0]
            ULTIMA.stop_reason = _parada_openai(ch.get("finish_reason"))
            ULTIMA.uso = r.json().get("usage")
            texto = ((ch.get("message") or {}).get("content") or "").strip()
            if not texto:
                raise RuntimeError(f"a IA (Groq {mod}) respondeu sem texto (parada: {ULTIMA.stop_reason})")
            return texto
        corpo = (r.text or "")[:400]
        baixo = corpo.lower()
        if r.status_code == 404 or (r.status_code == 400 and ("decommission" in baixo or "not found" in baixo)):
            _groq_cache["modelo"] = None
            excluir.append(mod)
            continue
        m = re.search(r"max_tokens`? must be less than or equal to `?(\d+)", corpo)
        if r.status_code == 400 and m:
            teto = int(m.group(1))
            if teto >= GROQ_MIN_SAIDA and teto < saida:
                saida = teto
                continue
            excluir.append(mod)
            _groq_cache["modelo"] = None
            saida = min(max_tokens, GROQ_MAX_SAIDA)
            continue
        limite, pedido = _limite_tpm(corpo)
        if r.status_code == 413 or (limite and "tokens per minute" in baixo and pedido > limite):
            if limite and pedido:
                entrada = pedido - saida
                cabe = limite - entrada - 200
                if cabe >= GROQ_MIN_SAIDA and cabe < saida:
                    log.info("Groq %s: limite de %d TPM, reduzindo a saída para %d tokens", mod, limite, cabe)
                    saida = cabe
                    continue
            log.info("Groq %s: pedido grande demais para o modelo (%s), tentando outro", mod, corpo[:120])
            if _groq_cache["modelo"] == mod:
                _groq_cache["modelo"] = None
            excluir.append(mod)
            saida = min(max_tokens, GROQ_MAX_SAIDA)
            if (os.environ.get("GROQ_MODEL") or "").strip() == mod:
                break
            continue
        if r.status_code == 429:
            _marcar_esgotado("groq", float(r.headers.get("retry-after") or 600))
        raise _CotaEsgotada(f"Groq ({mod}) respondeu {r.status_code}: {corpo[:300]}"
                            + (f" (antes: {', '.join(excluir)})" if excluir else ""))
    raise _CotaEsgotada(f"Groq: nenhum modelo aceitou o pedido (tentados: {', '.join(excluir)})")


# ---------------- Z.ai (GLM) ----------------
# Modelos Flash gratuitos (glm-4.7-flash, glm-4.5-flash), janela de ~200 mil tokens: recebem o pedido COMPLETO.
# Plano gratuito: 1 chamada por vez. Configuração: ZAI_API_KEY (z.ai) e, opcional, ZAI_MODEL.
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
ZAI_MODELOS = ["glm-4.7-flash", "glm-4.5-flash"]
ZAI_MAX_SAIDA = 16000


def _zai_ok():
    return bool(os.environ.get("ZAI_API_KEY"))


def _zai(mensagem_usuario, sessao, sistema, max_tokens, timeout):
    k = os.environ["ZAI_API_KEY"]
    fixo = (os.environ.get("ZAI_MODEL") or "").strip()
    erros = []
    for mod in ([fixo] if fixo else ZAI_MODELOS):
        try:
            r = (sessao or requests).post(
                ZAI_URL, timeout=timeout,
                headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
                json={"model": mod, "temperature": 0.3, "max_tokens": min(max_tokens, ZAI_MAX_SAIDA),
                      "thinking": {"type": "disabled"},          # sem raciocínio longo: mais rápido e sem cortes
                      "messages": [{"role": "system", "content": sistema},
                                   {"role": "user", "content": mensagem_usuario}]})
        except requests.Timeout:
            erros.append(f"{mod}: sem resposta a tempo")
            continue
        if r.status_code == 200:
            dados = r.json()
            ch = (dados.get("choices") or [{}])[0]
            ULTIMA.stop_reason = _parada_openai(ch.get("finish_reason"))
            ULTIMA.uso = dados.get("usage")
            texto = ((ch.get("message") or {}).get("content") or "").strip()
            if texto:
                return texto
            erros.append(f"{mod}: resposta vazia (parada: {ULTIMA.stop_reason})")
            continue
        corpo = (r.text or "")[:300]
        if r.status_code in (401, 403):
            raise _CotaEsgotada(f"Z.ai respondeu {r.status_code} (confira ZAI_API_KEY): {corpo}")
        if r.status_code == 429 and "concurren" not in corpo.lower() and "并发" not in corpo:
            _marcar_esgotado("zai", 1800)
            raise _CotaEsgotada(f"Z.ai ({mod}) respondeu 429: {corpo}")
        erros.append(f"{mod}: {r.status_code} {corpo}")
    raise _CotaEsgotada("Z.ai: " + " | ".join(erros))


def _cf_modelos():
    fixo = (os.environ.get("CF_MODEL") or "").strip()
    return [fixo] if fixo else CF_MODELOS


def _cloudflare(mensagem_usuario, sessao, sistema, max_tokens, timeout):
    """Cloudflare Workers AI. Tenta os modelos em ordem (os rápidos primeiro); num timeout (408), erro do servidor
    ou modelo inexistente, passa para o seguinte."""
    conta, token = os.environ["CF_ACCOUNT_ID"], os.environ["CF_API_TOKEN"]
    erros = []
    for mod in _cf_modelos():
        try:
            r = (sessao or requests).post(
                f"https://api.cloudflare.com/client/v4/accounts/{conta}/ai/run/{mod}", timeout=min(timeout, 120),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"max_tokens": min(max_tokens, CF_MAX_SAIDA), "temperature": 0.3,
                      "messages": [{"role": "system", "content": sistema},
                                   {"role": "user", "content": mensagem_usuario}]})
        except requests.Timeout:
            erros.append(f"{mod}: sem resposta a tempo")
            continue
        if r.status_code != 200:
            corpo = (r.text or "")[:200]
            if r.status_code == 429:
                _marcar_esgotado("cloudflare", 3600)
                raise _CotaEsgotada(f"Cloudflare ({mod}) respondeu 429: {corpo}")
            erros.append(f"{mod}: {r.status_code} {corpo}")
            if r.status_code in (400, 401, 403) and "model" not in corpo.lower():
                break                                   # erro de conta/pedido: outro modelo não resolve
            continue
        res = r.json().get("result") or {}
        texto = res.get("response")
        if texto is None and res.get("choices"):        # formato OpenAI de alguns modelos
            texto = ((res["choices"][0].get("message") or {}).get("content")) or ""
        if not isinstance(texto, str):                  # alguns modelos devolvem já o objeto JSON
            import json
            texto = json.dumps(texto, ensure_ascii=False) if texto is not None else ""
        ULTIMA.stop_reason = None
        ULTIMA.uso = res.get("usage")
        if texto.strip():
            ULTIMA.modelo_reserva = mod
            return texto.strip()
        erros.append(f"{mod}: resposta vazia")
    raise _CotaEsgotada("Cloudflare: " + " | ".join(erros))


def _modelos_reserva_gemini(mod):
    """Outros modelos do Gemini para tentar antes de sair do Google (cada um tem a sua cota diária no plano
    gratuito). Padrão: o modelo rápido (cotas maiores). Nunca "sobe" para o modelo principal quando quem chamou
    já usa o rápido (o radar), para não gastar a cota curta da Seleção e das questões de ordem.
    GEMINI_FALLBACK (lista separada por vírgulas, vazio = nenhum) troca essa lista."""
    env = os.environ.get("GEMINI_FALLBACK")
    if env is not None:
        lista = [m.strip() for m in env.split(",")]
    else:
        lista = [modelo_rapido()] if mod != modelo_rapido() else []
    out = []
    for m in lista:
        if m and m != mod and m not in out and not m.lower().startswith("claude"):
            out.append(m)
    return out


def cadeia(modelo_=None):
    """Ordem de tentativa: [(nome, função)]."""
    mod = modelo_ or modelo()
    passos = []
    if chave():
        for m in [mod] + _modelos_reserva_gemini(mod):
            passos.append((f"gemini:{m}", lambda *a, m=m: _gemini(m, *a)))
    if _zai_ok():
        passos.append(("zai", _zai))
    if _groq_ok():
        passos.append(("groq", _groq))
    if _cf_ok():
        passos.append(("cloudflare", _cloudflare))
    return passos


def chamar_ia(mensagem_usuario, sessao=None, sistema=None, modelo_=None, max_tokens=4000, timeout=180, pular=(),
              mensagem_reserva=None, max_tokens_reserva=None):
    """Envia a mensagem à IA e devolve o texto. Tenta o Gemini (modelo pedido e depois os outros modelos do
    plano gratuito); se a cota acabar ou a API cair, passa para Groq e Cloudflare, quando configurados.
    O provedor que respondeu fica em ULTIMA.provedor. `pular`: provedores a não tentar nesta chamada (ex.: o que
    acabou de devolver uma resposta inutilizável). `mensagem_reserva`/`max_tokens_reserva`: versão enxuta do pedido
    para Groq/Cloudflare, cujos limites de tokens por minuto e tempo de resposta são bem menores."""
    passos = [p for p in cadeia(modelo_) if p[0] not in set(pular)] or cadeia(modelo_)
    if not passos:
        raise RuntimeError("GEMINI_API_KEY não configurada no Railway.")
    sistema = sistema or SISTEMA
    erros, pulados, pular_gemini = [], [], False
    ULTIMA.provedor = None
    for nome, fn in passos:
        if pular_gemini and nome.startswith("gemini:"):
            continue
        if not _disponivel(nome):
            pulados.append(nome)
            continue
        reserva = nome in ("groq", "cloudflare")       # limites pequenos: recebem a versão enxuta
        msg_ = mensagem_reserva if reserva and mensagem_reserva else mensagem_usuario
        mt_ = max_tokens_reserva if reserva and max_tokens_reserva else max_tokens
        try:
            texto = fn(msg_, sessao, sistema, mt_, timeout)
        except _CotaEsgotada as e:
            erros.append(str(e))
            continue
        except requests.RequestException as e:
            erros.append(f"{nome}: falha de conexão ({e.__class__.__name__})")
            continue
        except RuntimeError as e:
            # erro de configuração ou de conteúdo: outro modelo do Gemini não resolve, outro provedor talvez sim
            erros.append(str(e))
            pular_gemini = pular_gemini or nome.startswith("gemini:")
            continue
        ULTIMA.provedor = nome
        if erros or pulados:
            log.warning("IA respondeu pela reserva %s (antes: %s)", nome,
                        "; ".join(erros + [f"{p} sem cota" for p in pulados]))
        return texto
    if not erros:
        erros = [f"todas as IAs estão sem cota no momento ({', '.join(pulados)})"]
    faltam = ([] if _zai_ok() else ["Z.ai (ZAI_API_KEY)"]) + \
             ([] if _groq_ok() else ["Groq (GROQ_API_KEY)"]) + \
             ([] if _cf_ok() else ["Cloudflare (CF_ACCOUNT_ID + CF_API_TOKEN)"])
    if faltam:
        erros.append("reserva não configurada no Railway: " + ", ".join(faltam))
    raise RuntimeError(" | ".join(erros))


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
