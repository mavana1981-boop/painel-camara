"""Clipping automático: toda aprovação do parlamentar fica registrada para sempre.

O clipping olhava só a situação *atual* das proposições e relatorias: quando um projeto aprovado
mudava de situação (ex.: "Aprovado" → "Aguardando Apreciação pelo Senado"), sumia do clipping.
Aqui cada aprovação vira uma linha em clipping_aprovacoes assim que é vista — pela coleta diária,
pelas notificações de mudança de situação ou ao abrir o painel — e não sai mais.
Uma linha por proposição/relatoria e situação de aprovação (etapas diferentes viram registros diferentes).
"""
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timedelta

from sqlalchemy import select, text

from db import clipping_aprovacoes, engine, usuario_deputados

log = logging.getLogger(__name__)

_APROVACAO = re.compile(r"(aprovad|transformad[ao] em norma|remetid[ao] (a|ao) (senado|camara|sancao)|"
                        r"enviad[ao] (a|ao) (senado|sancao)|aguardando (sancao|promulgacao)|"
                        r"aguardando (envio|remessa) ao senado|promulgad|sancionad)")


def _sem_acento(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def eh_aprovacao(situacao):
    s = _sem_acento(situacao)
    return bool(_APROVACAO.search(s)) and "rejeitad" not in s and "nao aprovad" not in s


def _candidatos(c, cod):
    """Aprovações visíveis agora: situação atual das proposições, notificações guardadas e relatorias."""
    p = {"c": cod}
    out = []
    for r in c.execute(text("""
            SELECT pr.id, pr.identificacao, pr.ementa, pr.situacao, pr.data_situacao, pr.url, a.principal
            FROM processos pr JOIN autorias a ON a.processo_id = pr.id
            WHERE a.codigo_deputado = :c AND pr.situacao IS NOT NULL"""), p).mappings():
        if eh_aprovacao(r["situacao"]) and r["data_situacao"]:
            out.append({"tipo": "autoria", "chave": f"p:{r['id']}", "identificacao": r["identificacao"],
                        "ementa": r["ementa"], "situacao": r["situacao"], "data": r["data_situacao"],
                        "url": r["url"], "colegiado": None, "principal": bool(r["principal"]),
                        "origem": "proposicao"})
    # mudanças de situação já notificadas: recupera aprovações cuja situação já mudou de novo
    for r in c.execute(text("""
            SELECT n.processo_id, n.identificacao, n.situacao_nova, n.data_situacao, n.detectado_em,
                   pr.ementa, pr.url, a.principal
            FROM notificacoes n
            LEFT JOIN processos pr ON pr.id = n.processo_id
            LEFT JOIN autorias a ON a.processo_id = n.processo_id AND a.codigo_deputado = n.codigo_deputado
            WHERE n.codigo_deputado = :c AND n.tipo = 'situacao' AND n.situacao_nova IS NOT NULL"""), p).mappings():
        if not eh_aprovacao(r["situacao_nova"]):
            continue
        dt = r["data_situacao"] or (r["detectado_em"].date().isoformat() if isinstance(r["detectado_em"], datetime)
                                    else str(r["detectado_em"] or "")[:10])
        if not dt:
            continue
        out.append({"tipo": "autoria", "chave": f"p:{r['processo_id']}", "identificacao": r["identificacao"],
                    "ementa": r["ementa"], "situacao": r["situacao_nova"], "data": dt, "url": r["url"],
                    "colegiado": None, "principal": bool(r["principal"]), "origem": "notificacao"})
    for r in c.execute(text("""
            SELECT chave, identificacao, ementa, situacao, data_situacao, url, colegiado
            FROM relatorias WHERE codigo_deputado = :c AND situacao IS NOT NULL"""), p).mappings():
        if eh_aprovacao(r["situacao"]) and r["data_situacao"]:
            out.append({"tipo": "relatoria", "chave": r["chave"], "identificacao": r["identificacao"],
                        "ementa": r["ementa"], "situacao": r["situacao"], "data": r["data_situacao"],
                        "url": r["url"], "colegiado": r["colegiado"], "principal": None, "origem": "relatoria"})
    return out


def registrar(cod, extras=()):
    """Grava as aprovações novas do deputado no histórico do clipping. Devolve quantas entraram.
    `extras`: aprovações achadas na tramitação (buscar_tramitacoes)."""
    agora = datetime.now()
    with engine.begin() as c:
        linhas = c.execute(select(clipping_aprovacoes.c.tipo, clipping_aprovacoes.c.chave, clipping_aprovacoes.c.situacao,
                                  clipping_aprovacoes.c.data)
                           .where(clipping_aprovacoes.c.codigo_deputado == cod)).all()
        ja = {(r[0], r[1], r[2]) for r in linhas}
        no_dia = {(r[0], r[1], r[3]) for r in linhas}
        cands = _candidatos(c, cod)
        ordem = {"proposicao": 0, "notificacao": 1, "relatoria": 0, "tramitacao": 2}
        novos = []
        for x in sorted(list(cands) + list(extras), key=lambda x: (x["data"], ordem.get(x["origem"], 3))):
            k = (x["tipo"], x["chave"], (x["situacao"] or "")[:200])
            if k in ja:
                continue
            # da tramitação, só o que ainda não tem registro da mesma proposição no mesmo dia
            if x["origem"] == "tramitacao" and (x["tipo"], x["chave"], x["data"]) in no_dia:
                continue
            ja.add(k)
            no_dia.add((x["tipo"], x["chave"], x["data"]))
            novos.append({**x, "situacao": k[2], "codigo_deputado": cod, "registrado_em": agora,
                          "identificacao": (x["identificacao"] or "")[:60] or None})
        if novos:
            c.execute(clipping_aprovacoes.insert(), novos)
    return len(novos)


# ---------------- tramitação dos últimos dias (API da Câmara) ----------------

DIAS_TRAMITACAO = 7
MAX_PROPOSICOES = 150                  # por deputado e por busca
# despacho de votação que aprova a própria matéria (não um requerimento sobre ela)
_DESPACHO_APROV = re.compile(r"^\s*aprovad[oa]s?\s+(?:(?:o|a|os|as)\s+)?(?:projeto|proposta|parecer|substitutivo|"
                             r"redacao final|emenda|pec|mensagem|texto|materia|subemenda|em (?:primeiro|segundo|turno))")


def aprovacoes_na_tramitacao(trams, inicio: str, fim: str):
    """Tramitações (formato da API v2) → aprovações entre inicio e fim (AAAA-MM-DD).
    Aprovação = situação nova de aprovação ou despacho que aprova a matéria (ex.: "Aprovado o Parecer.")."""
    out, vistos = [], set()
    for t in sorted((t for t in trams or [] if isinstance(t, dict)), key=lambda t: t.get("dataHora") or ""):
        dt = (t.get("dataHora") or "")[:10]
        if not dt or not inicio <= dt <= fim:
            continue
        orgao = (t.get("siglaOrgao") or "").strip() or None
        desp = re.sub(r"\s+", " ", t.get("despacho") or "").strip()
        sit = (t.get("descricaoSituacao") or "").strip()
        if desp and _DESPACHO_APROV.match(_sem_acento(desp)):
            texto = re.split(r"(?<=[.;])\s", desp, maxsplit=1)[0].rstrip(".;")[:160]
            texto = f"{texto} ({orgao})" if orgao else texto
        elif sit and eh_aprovacao(sit):
            texto = sit
        else:
            continue
        k = (dt, orgao)
        if k in vistos:
            continue
        vistos.add(k)
        out.append({"data": dt, "situacao": texto[:200], "colegiado": orgao})
    return out


def _ler_tramitacoes(pid, inicio, fim):
    from collector import get_json
    doc = get_json(f"/proposicoes/{pid}/tramitacoes?dataInicio={inicio}&dataFim={fim}")
    return (doc or {}).get("dados") or []


def buscar_tramitacoes(cod, dias=DIAS_TRAMITACAO, hoje=None, ler=None):
    """Lê na Câmara a tramitação dos últimos `dias` das proposições do deputado que se mexeram no período
    e devolve as aprovações achadas (pega etapas que já foram seguidas de outra no mesmo intervalo)."""
    from datetime import timedelta
    hoje = hoje or date.today()
    ini, fim = (hoje - timedelta(days=dias)).isoformat(), hoje.isoformat()
    ler = ler or _ler_tramitacoes
    with engine.connect() as c:
        props = [dict(r) for r in c.execute(text("""
            SELECT pr.id, pr.identificacao, pr.ementa, pr.url, a.principal
            FROM processos pr JOIN autorias a ON a.processo_id = pr.id
            WHERE a.codigo_deputado = :c AND pr.data_situacao >= :ini
            ORDER BY pr.data_situacao DESC"""), {"c": cod, "ini": ini}).mappings()][:MAX_PROPOSICOES]
    out = []
    for p in props:
        try:
            trams = ler(p["id"], ini, fim)
        except Exception as e:
            log.warning("tramitação de %s: %s", p["identificacao"], e)
            continue
        for a in aprovacoes_na_tramitacao(trams, ini, fim):
            out.append({"tipo": "autoria", "chave": f"p:{p['id']}", "identificacao": p["identificacao"],
                        "ementa": p["ementa"], "url": p["url"], "principal": bool(p["principal"]),
                        "origem": "tramitacao", **a})
    return out


def registrar_todos(com_tramitacao=True, dias=DIAS_TRAMITACAO):
    """Etapa da coleta diária: todos os deputados acompanhados no Meu Parlamentar
    (com a tramitação dos últimos `dias` na Câmara, se com_tramitacao)."""
    with engine.connect() as c:
        cods = sorted({r[0] for r in c.execute(select(usuario_deputados.c.codigo_deputado))})
    n = 0
    for cod in cods:
        try:
            extras = []
            if com_tramitacao and os.environ.get("CLIPPING_TRAMITACAO") != "0":
                try:
                    extras = buscar_tramitacoes(cod, dias)
                except Exception:
                    log.exception("tramitação do deputado %s", cod)
            n += registrar(cod, extras)
        except Exception:
            log.exception("clipping automático do deputado %s", cod)
    log.info("clipping automático: %d aprovação(ões) nova(s) registrada(s)", n)
    return n


def do_periodo(cod, inicio: date, fim: date):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(
            select(clipping_aprovacoes).where(clipping_aprovacoes.c.codigo_deputado == cod,
                                              clipping_aprovacoes.c.data >= inicio.isoformat(),
                                              clipping_aprovacoes.c.data <= fim.isoformat())
            .order_by(clipping_aprovacoes.c.data.desc(), clipping_aprovacoes.c.id)).mappings()]


# ---------------- busca disparada pela tela ----------------

import threading  # noqa: E402

_buscando = set()
_buscando_lock = threading.Lock()


def buscar_em_segundo_plano(cod, dias=DIAS_TRAMITACAO):
    """Botão do clipping: lê a tramitação dos últimos dias deste deputado sem travar a página."""
    with _buscando_lock:
        if cod in _buscando:
            return False
        _buscando.add(cod)

    def rodar():
        try:
            n = registrar(cod, buscar_tramitacoes(cod, dias))
            log.info("clipping do deputado %s: %d aprovação(ões) dos últimos %d dias", cod, n, dias)
        except Exception:
            log.exception("busca de aprovações do deputado %s", cod)
        finally:
            with _buscando_lock:
                _buscando.discard(cod)
    threading.Thread(target=rodar, daemon=True).start()
    return True


def buscando(cod):
    return cod in _buscando


# ---------------- carga da legislatura inteira ----------------
# A coleta diária só enxerga os últimos 7 dias. Esta carga roda uma vez (em segundo plano) e preenche o
# histórico desde o início da legislatura:
#   1) autoria: a tramitação completa (desde fev/2023) de cada proposição do deputado acompanhado;
#   2) relatoria: as pautas de todas as reuniões deliberativas da legislatura, mês a mês — cada item com
#      parecer aprovado vira registro do relator (de todos os deputados, para servir a quem for acompanhado depois).
# É retomável: deputado concluído e mês de pautas lido ficam anotados em meta.

INICIO_LEGISLATURA = os.environ.get("INICIO_LEGISLATURA", "2023-02-01")
CHAVE_LEG_DEP = "clipping:legislatura:dep:"          # + código do deputado
CHAVE_LEG_MES = "clipping:legislatura:pautas:"       # + AAAA-MM
SIGLAS_SEM_VOTACAO = {"RIC", "INC"}                  # requerimento de informação e indicação não são votados
LOTE = 50

_leg_lock = threading.Lock()
_leg_estado = {"rodando": False, "fase": None, "cod": None, "feito": 0, "total": 0, "fila": []}


def _meta_get(chave):
    from db import meta
    with engine.connect() as c:
        return c.execute(select(meta.c.valor).where(meta.c.chave == chave)).scalar()


def _meta_set(chave, valor=None):
    from sqlalchemy import delete, insert
    from db import meta
    with engine.begin() as c:
        c.execute(delete(meta).where(meta.c.chave == chave))
        c.execute(insert(meta).values(chave=chave, valor=valor or datetime.now().isoformat(timespec="seconds")))


def legislatura_concluida(cod):
    return _meta_get(f"{CHAVE_LEG_DEP}{cod}")


def carga_legislatura_deputado(cod, hoje=None, ler=None):
    """Tramitação desde o início da legislatura de todas as proposições de autoria do deputado →
    aprovações no histórico do clipping. Grava em lotes (o que já foi lido não se perde se o servidor reiniciar)."""
    hoje = hoje or date.today()
    ini, fim = INICIO_LEGISLATURA, hoje.isoformat()
    ler = ler or _ler_tramitacoes
    with engine.connect() as c:
        props = [dict(r) for r in c.execute(text("""
            SELECT pr.id, pr.sigla, pr.identificacao, pr.ementa, pr.url, a.principal
            FROM processos pr JOIN autorias a ON a.processo_id = pr.id
            WHERE a.codigo_deputado = :c AND (pr.data_situacao IS NULL OR pr.data_situacao >= :ini)
            ORDER BY pr.data_situacao DESC"""), {"c": cod, "ini": ini}).mappings()]
    props = [p for p in props if (p["sigla"] or "").upper() not in SIGLAS_SEM_VOTACAO]
    _leg_estado.update(fase="autoria", cod=cod, feito=0, total=len(props))
    n, lote = 0, []
    for i, p in enumerate(props, 1):
        try:
            trams = ler(p["id"], ini, fim)
        except Exception as e:
            log.warning("tramitação de %s: %s", p["identificacao"], e)
            trams = []
        for a in aprovacoes_na_tramitacao(trams, ini, fim):
            lote.append({"tipo": "autoria", "chave": f"p:{p['id']}", "identificacao": p["identificacao"],
                         "ementa": p["ementa"], "url": p["url"], "principal": bool(p["principal"]),
                         "origem": "tramitacao", **a})
        _leg_estado["feito"] = i
        if i % LOTE == 0:
            n += registrar(cod, lote)
            lote = []
    n += registrar(cod, lote)
    _meta_set(f"{CHAVE_LEG_DEP}{cod}")
    log.info("clipping da legislatura do deputado %s: %d proposição(ões) lidas, %d aprovação(ões) nova(s)",
             cod, len(props), n)
    return n


def _meses(ini: date, fim: date):
    d = ini.replace(day=1)
    while d <= fim:
        prox = (d + timedelta(days=32)).replace(day=1)
        yield max(d, ini), min(prox - timedelta(days=1), fim)
        d = prox


def relatorias_aprovadas_da_pauta(ev, itens):
    """Itens da pauta com relator e parecer aprovado → [(codigo_deputado, registro do clipping)]."""
    from coleta_meu import relatorias_da_pauta
    out = []
    for cod, r in relatorias_da_pauta(ev, itens):
        if r["situacao"] and r["data_situacao"] and eh_aprovacao(r["situacao"]):
            out.append((cod, {"tipo": "relatoria", "chave": r["chave"], "identificacao": r["identificacao"],
                              "ementa": r["ementa"], "situacao": r["situacao"], "data": r["data_situacao"],
                              "url": r["url"], "colegiado": r["colegiado"], "principal": None,
                              "origem": "relatoria"}))
    return out


def carga_legislatura_pautas(hoje=None, eventos=None, ler_pauta=None):
    """Pautas das reuniões deliberativas desde o início da legislatura (mês a mês, retomável):
    completa a tabela de relatorias e registra cada parecer aprovado no clipping do relator."""
    import coleta_meu
    from db import deputados
    hoje = hoje or date.today()
    eventos = eventos or coleta_meu.eventos_periodo
    ler_pauta = ler_pauta or coleta_meu.pauta
    with engine.connect() as c:
        cad = {r[0] for r in c.execute(select(deputados.c.codigo))}
    meses = [m for m in _meses(date.fromisoformat(INICIO_LEGISLATURA), hoje)
             if not _meta_get(f"{CHAVE_LEG_MES}{m[0]:%Y-%m}")]
    _leg_estado.update(fase="relatoria", cod=None, feito=0, total=len(meses))
    n = 0
    for i, (ini, fim) in enumerate(meses, 1):
        por_dep = {}
        for ev in eventos(ini, fim):
            if not coleta_meu.deliberativo(ev):
                continue
            itens = ler_pauta(ev["id"])
            if not itens:
                continue
            with engine.begin() as c:
                coleta_meu.gravar_relatorias_pauta(c, ev, itens, cad)
            for cod, x in relatorias_aprovadas_da_pauta(ev, itens):
                if cod in cad:
                    por_dep.setdefault(cod, []).append(x)
        for cod, extras in por_dep.items():
            n += registrar(cod, extras)
        if fim < hoje.replace(day=1):                # mês corrente fica para a próxima carga
            _meta_set(f"{CHAVE_LEG_MES}{ini:%Y-%m}")
        _leg_estado["feito"] = i
    log.info("clipping da legislatura (pautas): %d mês(es) lido(s), %d relatoria(s) aprovada(s) nova(s)", len(meses), n)
    return n


def _rodar_fila():
    try:
        while _leg_estado["fila"]:
            item = _leg_estado["fila"].pop(0)
            _leg_estado.update(cod=None if item == "pautas" else item, feito=0, total=0,
                               fase="relatoria" if item == "pautas" else "autoria")
            try:
                if item == "pautas":
                    carga_legislatura_pautas()
                else:
                    carga_legislatura_deputado(item)
            except Exception:
                log.exception("carga da legislatura (%s)", item)
    finally:
        _leg_estado.update(rodando=False, fase=None, cod=None, feito=0, total=0)
        _leg_lock.release()


def iniciar_carga_legislatura(cods=None, pautas=None):
    """Põe na fila (em segundo plano) os deputados ainda sem a legislatura carregada — ou `cods`, à força —
    e, se faltar algum mês, as pautas. Devolve False se já há uma carga rodando."""
    with engine.connect() as c:
        if not c.execute(text("SELECT 1 FROM processos LIMIT 1")).first():
            return False                                 # proposições ainda não carregadas (primeiro deploy)
        if cods is None:
            todos = sorted({r[0] for r in c.execute(select(usuario_deputados.c.codigo_deputado))})
    if cods is None:
        cods = [x for x in todos if not legislatura_concluida(x)]
    if pautas is None:
        fim_ant = date.today().replace(day=1) - timedelta(days=1)    # o mês corrente nunca fica anotado
        pautas = any(not _meta_get(f"{CHAVE_LEG_MES}{m[0]:%Y-%m}")
                     for m in _meses(date.fromisoformat(INICIO_LEGISLATURA), fim_ant))
    fila = list(cods) + (["pautas"] if pautas else [])
    if not fila or not _leg_lock.acquire(blocking=False):
        return False
    _leg_estado.update(rodando=True, fila=fila)
    threading.Thread(target=_rodar_fila, daemon=True).start()
    return True


def estado_carga_legislatura(cod=None):
    e = dict(_leg_estado)
    e["fila"] = list(_leg_estado["fila"])
    e["deste"] = bool(e["rodando"] and cod is not None and (e["cod"] == cod or cod in e["fila"]))
    e["concluida"] = legislatura_concluida(cod) if cod is not None else None
    return e


CHAVE_CARGA_INICIAL = "clipping:tramitacao_inicial"


def carga_inicial_se_preciso():
    """Uma vez, logo após o deploy: aprovações dos últimos 7 dias de todos os deputados acompanhados."""
    from db import init_db, meta
    from sqlalchemy import delete, insert
    try:
        init_db()                          # a tabela nova pode não existir ainda no primeiro deploy
    except Exception:
        log.exception("init_db antes da carga do clipping")
    with engine.connect() as c:
        if c.execute(select(meta.c.valor).where(meta.c.chave == CHAVE_CARGA_INICIAL)).scalar():
            return 0
    n = registrar_todos(com_tramitacao=True)
    with engine.begin() as c:
        c.execute(delete(meta).where(meta.c.chave == CHAVE_CARGA_INICIAL))
        c.execute(insert(meta).values(chave=CHAVE_CARGA_INICIAL, valor=datetime.now().isoformat(timespec="seconds")))
    return n


def carga_legislatura_ao_iniciar(espera=300):
    """Logo após o deploy (com folga para o app subir): retoma/inicia a carga da legislatura."""
    import time
    time.sleep(espera)
    try:
        iniciar_carga_legislatura()
    except Exception:
        log.exception("carga da legislatura do clipping")
