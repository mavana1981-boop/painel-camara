"""Ex-deputados acompanhados no Meu Parlamentar.

Alguns usuários acompanham um parlamentar que não é da legislatura atual (ex.: a Luísa acompanha o
ex-deputado Subtenente Gonzaga, 55ª e 56ª legislaturas). A coleta normal só olha a 57ª legislatura;
aqui, para cada ex-deputado configurado:
  1) o cadastro dele fica em `deputados` (fora de exercício) e não é apagado pela limpeza;
  2) o perfil (dados, mandatos, comissões que integrou, frentes) vem da API, para o período do mandato;
  3) as proposições de autoria dele dos anos do mandato vêm dos arquivos anuais da Câmara;
  4) a tramitação completa dos projetos dele vira histórico de aprovações (clipping);
  5) os usuários indicados são vinculados uma vez (o administrador pode desfazer depois).
A carga roda em segundo plano no deploy e é refeita a cada DIAS_RECARGA dias (situações mudam:
projeto no Senado, sanção etc.). Nada disso entra nos rankings da legislatura atual.
"""
import json
import logging
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import delete, insert, select, text, update

from db import autorias, deputados, engine, meta, norm_nome, processos, usuario_deputados, usuarios

log = logging.getLogger("ex_deputados")

# código da API de Dados Abertos → configuração
EX_DEPUTADOS = {
    177282: {
        "nome": "Subtenente Gonzaga", "partido": "PSD", "uf": "MG",
        "inicio": "2015-02-01", "fim": "2023-01-31",      # 55ª e 56ª legislaturas
        "usuarios": ["luisa"],                             # login ou primeiro nome (sem acento)
        "foto": "/static/fotos/dep-177282.jpg",            # foto enviada pela equipe (prevalece sobre a da Câmara)
    },
}
PROJETOS = {"PL", "PLP", "PEC", "PDL", "PDC", "PRC", "PLV"}  # PDC: decreto legislativo antes de 2019
DIAS_RECARGA = 7
CHAVE_CARGA = "ex:carga:"          # + código: última carga completa
CHAVE_VINCULO = "ex:vinculo:"      # + código: usuários já vinculados (1x)
LEGISLATURAS = {52: (2003, 2007), 53: (2007, 2011), 54: (2011, 2015), 55: (2015, 2019),
                56: (2019, 2023), 57: (2023, 2027), 58: (2027, 2031)}

_lock = threading.Lock()
estado = {"rodando": False, "cod": None, "fase": None, "feito": 0, "total": 0}


def codigos():
    return set(EX_DEPUTADOS)


def eh_ex(cod):
    return cod in EX_DEPUTADOS


def config(cod):
    return EX_DEPUTADOS.get(cod) or {}


def _meta_get(c, chave):
    return c.execute(select(meta.c.valor).where(meta.c.chave == chave)).scalar()


def _meta_set(c, chave, valor):
    c.execute(delete(meta).where(meta.c.chave == chave))
    c.execute(insert(meta).values(chave=chave, valor=valor))


# ---------------- cadastro e vínculos ----------------

def garantir_cadastro():
    """Coloca no cadastro quem ainda não está (a coleta só traz a legislatura atual)."""
    from collector import _foto
    n = 0
    with engine.begin() as c:
        existentes = {r[0] for r in c.execute(select(deputados.c.codigo).where(deputados.c.codigo.in_(codigos())))}
        for cod, cfg in EX_DEPUTADOS.items():
            if cod in existentes:
                if cfg.get("foto"):
                    c.execute(update(deputados).where(deputados.c.codigo == cod).values(foto=cfg["foto"]))
                continue
            c.execute(insert(deputados).values(
                codigo=cod, nome=cfg["nome"], nome_norm=norm_nome(cfg["nome"]), partido=cfg.get("partido"),
                uf=cfg.get("uf"), foto=cfg.get("foto") or _foto(cod), em_exercicio=False))
            n += 1
    return n


def _casa(u, alvo):
    alvo = norm_nome(alvo)
    login = norm_nome((u["login"] or "").replace(".", " ").replace("_", " "))
    nome = norm_nome(u["nome"] or "")
    return (login.split()[:1] == [alvo]) or (nome.split()[:1] == [alvo])


def vincular_usuarios():
    """Vincula uma vez os usuários indicados. Se o usuário ainda não existe, tenta de novo depois."""
    feitos = 0
    with engine.begin() as c:
        todos = [dict(r) for r in c.execute(select(usuarios.c.id, usuarios.c.login, usuarios.c.nome)
                                            .where(usuarios.c.ativo.is_not(False))).mappings()]
        for cod, cfg in EX_DEPUTADOS.items():
            if _meta_get(c, f"{CHAVE_VINCULO}{cod}"):
                continue
            achados = [u for u in todos if any(_casa(u, a) for a in cfg.get("usuarios") or [])]
            if not achados:
                continue
            ja = {r[0] for r in c.execute(select(usuario_deputados.c.usuario_id)
                                          .where(usuario_deputados.c.codigo_deputado == cod))}
            for u in achados:
                if u["id"] not in ja:
                    c.execute(insert(usuario_deputados).values(usuario_id=u["id"], codigo_deputado=cod))
                    feitos += 1
            _meta_set(c, f"{CHAVE_VINCULO}{cod}", ",".join(u["login"] for u in achados))
            log.info("ex-deputado %s vinculado a: %s", cod, ", ".join(u["login"] for u in achados))
    return feitos


# ---------------- proposições dos anos do mandato ----------------

def anos_do_mandato(cod):
    """Anos de apresentação lidos aqui: do início do mandato até o ano anterior à legislatura atual
    (os anos da legislatura atual já entram pela coleta diária, porque ele está no cadastro)."""
    import collector
    cfg = config(cod)
    ini = int(cfg["inicio"][:4])
    fim = min(int(cfg["fim"][:4]), int(collector.INICIO_LEGISLATURA[:4]) - 1)
    return list(range(ini, fim + 1))


def gravar_ano(conn, cod, ano, linhas_props, linhas_autores, notificar=False):
    """Proposições de um ano (arquivos anuais) em que o ex-deputado é autor ou coautor → processos/autorias.
    Só mexe nas linhas dele: não apaga nada da coleta da legislatura atual."""
    import collector
    autores = defaultdict(list)
    for r in linhas_autores:
        pid = collector._int(r.get("idProposicao"))
        if pid is None or (r.get("proponente") or "1").strip() not in ("1", "true", "True"):
            continue
        autores[pid].append((collector._int(r.get("ordemAssinatura")) or 999, (r.get("nomeAutor") or "").strip(),
                             collector._int(r.get("idDeputadoAutor"))))
    alvo = {pid for pid, lst in autores.items() if any(c == cod for _, _, c in lst)}
    linhas, auts = [], []
    for r in linhas_props:
        pid = collector._int(r.get("id"))
        # autoria/coautoria dele, ou matéria em que ele é o relator registrado (último relator designado)
        if pid is None or (pid not in alvo and collector.id_da_uri(r.get("ultimoStatus_uriRelator")) != cod):
            continue
        p = collector.parse_proposicao(r, ano)
        if p["sigla"] in collector.TIPOS_EXCLUIDOS:
            continue
        lst = sorted(autores[pid])
        p["autoria"] = ", ".join(n for _, n, _ in lst if n)[:2000] or None
        linhas.append(p)
        if pid in alvo:
            dele = min(o for o, _, c in lst if c == cod)
            auts.append({"processo_id": pid, "codigo_deputado": cod, "principal": dele == lst[0][0]})
    ids = [p["id"] for p in linhas]
    antes = {}
    for i in range(0, len(ids), 900):
        for r in conn.execute(select(processos.c.id, processos.c.situacao, processos.c.data_situacao)
                              .where(processos.c.id.in_(ids[i:i + 900]))):
            antes[r[0]] = (r[1], r[2])
        conn.execute(delete(autorias).where(autorias.c.processo_id.in_(ids[i:i + 900]),
                                            autorias.c.codigo_deputado == cod))
        conn.execute(delete(processos).where(processos.c.id.in_(ids[i:i + 900])))
    collector._lote(conn, processos, linhas)
    collector._lote(conn, autorias, auts)
    if notificar and antes:   # mudança de situação de projeto antigo (ex.: sancionado) → notificação
        collector.registrar_notificacoes(conn, [p for p in linhas if p["id"] in antes], auts, antes)
    return len(linhas)


def carregar_proposicoes(cod, notificar=False):
    import collector
    n = 0
    anos = anos_do_mandato(cod)
    estado.update(fase="proposições", feito=0, total=len(anos))
    for i, ano in enumerate(anos, 1):
        a_props = a_aut = None
        try:
            a_aut = collector.baixar(collector.arquivo_anual("proposicoesAutores", ano))
            a_props = collector.baixar(collector.arquivo_anual("proposicoes", ano))
            if a_aut and a_props:
                with engine.begin() as c:
                    k = gravar_ano(c, cod, ano, collector.ler_csv(a_props), collector.ler_csv(a_aut), notificar)
                log.info("ex-deputado %s: %d proposição(ões) de %s", cod, k, ano)
                n += k
        except Exception:
            log.exception("proposições de %s do ex-deputado %s", ano, cod)
        finally:
            collector._apagar(a_props, a_aut)
        estado["feito"] = i
    return n


# ---------------- perfil ----------------

def carregar_perfil(cod, ler=None, ler_paginas=None):
    import collector
    ler = ler or collector.get_json
    ler_paginas = ler_paginas or collector.paginado
    cfg = config(cod)
    det = ler(f"/deputados/{cod}") or {}
    hist = (ler(f"/deputados/{cod}/historico") or {}).get("dados") or []
    org = ler_paginas(f"/deputados/{cod}/orgaos?dataInicio={cfg['inicio']}&dataFim={cfg['fim']}&itens=100")
    fre = (ler(f"/deputados/{cod}/frentes") or {}).get("dados") or []
    det = dict(det)
    det["historico"] = hist
    with engine.begin() as c:
        collector.gravar_perfil(c, cod, det, org, fre)
        st = (det.get("dados") or {}).get("ultimoStatus") or {}
        novos = {"nome": (st.get("nomeEleitoral") or st.get("nome") or "").strip() or None,
                 "partido": collector.norm_partido(st.get("siglaPartido")) or None,
                 "uf": st.get("siglaUf") or None, "foto": st.get("urlFoto") or None,
                 "email": st.get("email") or None}
        novos = {k: v for k, v in novos.items() if v}
        if config(cod).get("foto"):
            novos["foto"] = config(cod)["foto"]
        if novos.get("nome"):
            novos["nome_norm"] = norm_nome(novos["nome"])
        if novos:
            c.execute(update(deputados).where(deputados.c.codigo == cod).values(**novos))


def mandatos(detalhe):
    """Histórico da API → [{legislatura, periodo, partidos, uf, situacoes}] (mais recente primeiro)."""
    por = {}
    for h in (detalhe or {}).get("historico") or []:
        leg = h.get("idLegislatura")
        if not leg:
            continue
        m = por.setdefault(int(leg), {"legislatura": int(leg), "partidos": [], "uf": h.get("siglaUf"),
                                      "situacoes": [], "condicao": None})
        p = (h.get("siglaPartido") or "").strip()
        if p and p not in m["partidos"]:
            m["partidos"].append(p)
        s = (h.get("situacao") or "").strip()
        if s and s not in m["situacoes"]:
            m["situacoes"].append(s)
        m["condicao"] = m["condicao"] or h.get("condicaoEleitoral")
    out = []
    for leg in sorted(por, reverse=True):
        m = por[leg]
        a, b = LEGISLATURAS.get(leg, (None, None))
        m["periodo"] = f"{a}–{b}" if a else ""
        out.append(m)
    return out


# ---------------- aprovações (tramitação completa) ----------------

def carregar_aprovacoes(cod, hoje=None, ler=None):
    import clipping_auto
    hoje = hoje or date.today()
    ini, fim = config(cod)["inicio"], hoje.isoformat()
    ler = ler or clipping_auto._ler_tramitacoes
    with engine.connect() as c:
        props = [dict(r) for r in c.execute(text("""
            SELECT pr.id, pr.sigla, pr.identificacao, pr.ementa, pr.url, a.principal
            FROM processos pr JOIN autorias a ON a.processo_id = pr.id
            WHERE a.codigo_deputado = :c"""), {"c": cod}).mappings()]
    props = [p for p in props if (p["sigla"] or "").upper() in PROJETOS]
    estado.update(fase="tramitação", feito=0, total=len(props))
    n, lote = 0, []
    for i, p in enumerate(props, 1):
        try:
            trams = ler(p["id"], ini, fim)
        except Exception as e:
            log.warning("tramitação de %s: %s", p["identificacao"], e)
            trams = []
        for a in clipping_auto.aprovacoes_na_tramitacao(trams, ini, fim):
            lote.append({"tipo": "autoria", "chave": f"p:{p['id']}", "identificacao": p["identificacao"],
                         "ementa": p["ementa"], "url": p["url"], "principal": bool(p["principal"]),
                         "origem": "tramitacao", **a})
        estado["feito"] = i
        if i % 50 == 0:
            n += clipping_auto.registrar(cod, lote)
            lote = []
    n += clipping_auto.registrar(cod, lote)
    return n


# ---------------- orquestração ----------------

def precisa_carga(cod, agora=None):
    with engine.connect() as c:
        v = _meta_get(c, f"{CHAVE_CARGA}{cod}")
    if not v:
        return True
    try:
        return (agora or datetime.now()) - datetime.fromisoformat(v) > timedelta(days=DIAS_RECARGA)
    except ValueError:
        return True


def ultima_carga(cod):
    with engine.connect() as c:
        return _meta_get(c, f"{CHAVE_CARGA}{cod}")


def carregar(cod):
    primeira = ultima_carga(cod) is None
    estado.update(rodando=True, cod=cod)
    try:
        estado["fase"] = "perfil"
        try:
            carregar_perfil(cod)
        except Exception:
            log.exception("perfil do ex-deputado %s", cod)
        n_props = carregar_proposicoes(cod, notificar=not primeira)
        n_aprov = carregar_aprovacoes(cod)
        with engine.begin() as c:
            _meta_set(c, f"{CHAVE_CARGA}{cod}", datetime.now().isoformat(timespec="seconds"))
        log.info("ex-deputado %s: %d proposição(ões), %d aprovação(ões) nova(s)", cod, n_props, n_aprov)
        try:
            import paineis
            paineis.limpar_cache()
        except Exception:
            pass
    finally:
        estado.update(rodando=False, fase=None)


def atualizar(forcar=False):
    """Cadastro + vínculos sempre; carga completa quando nunca rodou ou passou de DIAS_RECARGA dias."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        from db import init_db
        init_db()
        garantir_cadastro()
        vincular_usuarios()
        for cod in sorted(codigos()):
            if forcar or precisa_carga(cod):
                carregar(cod)
        return True
    except Exception:
        log.exception("ex-deputados")
        return False
    finally:
        _lock.release()


def atualizar_em_segundo_plano(forcar=False):
    threading.Thread(target=atualizar, args=(forcar,), daemon=True).start()


# ---------------- tela ----------------

def _rotulo(sit, norma):
    s = norm_nome(sit or "")
    if norma:
        return "lei", "Virou norma jurídica"
    if "sancao" in s or "promulga" in s:
        return "sancao", "Aprovado — aguardando sanção/promulgação"
    if "senado" in s:
        return "senado", "Aprovado na Câmara — Senado"
    return "comissao", "Aprovado na Câmara"


def _aprovado(p, etapas):
    s = norm_nome(p["situacao"] or "")
    import clipping_auto
    return (bool(p["virou_norma"]) or clipping_auto.eh_aprovacao(p["situacao"] or "")
            or ("senado" in s and "rejeitad" not in s) or bool(etapas))


def projetos(cod, todas=False):
    """Todos os projetos ligados ao ex-deputado — autoria, coautoria e relatoria — com a situação de cada um.
    todas=True: também requerimentos, indicações e demais proposições.
    classe: lei | sancao | senado | comissao (aprovados) | tramita | encerrado."""
    import clipping_auto
    with engine.connect() as c:
        props = {}
        for r in c.execute(text("""
                SELECT pr.id, pr.sigla, pr.identificacao, pr.ementa, pr.situacao, pr.data_situacao, pr.url,
                       pr.virou_norma, pr.tramitando, pr.data_apresentacao, pr.autoria, pr.relator_id, pr.orgao,
                       a.principal, a.codigo_deputado AS autor
                FROM processos pr LEFT JOIN autorias a ON a.processo_id = pr.id AND a.codigo_deputado = :c
                WHERE a.codigo_deputado = :c OR pr.relator_id = :c"""), {"c": cod}).mappings():
            props[r["id"]] = dict(r)
        etapas = defaultdict(list)
        for r in c.execute(text("""SELECT chave, data, situacao, colegiado, identificacao, ementa FROM clipping_aprovacoes
                                   WHERE codigo_deputado = :c AND tipo = 'autoria' ORDER BY data"""),
                           {"c": cod}).mappings():
            if clipping_auto.natureza(dict(r)) != "merito":
                continue
            try:
                pid = int(str(r["chave"]).split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            etapas[pid].append({"data": r["data"], "situacao": r["situacao"], "colegiado": r["colegiado"]})
    out = []
    for pid, p in props.items():
        projeto = (p["sigla"] or "").upper() in PROJETOS
        if not todas and not projeto:
            continue
        autor = p["autor"] is not None
        relator = p["relator_id"] == cod
        papel = ("1º autor" if p["principal"] else "coautor") if autor else "relator"
        if autor and relator:
            papel += " e relator"
        et = etapas.get(pid, [])
        sit = p["situacao"] or ""
        s = norm_nome(sit)
        if projeto and _aprovado(p, et):
            cls, rot = _rotulo(sit, p["virou_norma"])
            if cls == "senado" and not ("aguardando" in s or "remetid" in s or "enviad" in s):
                cls, rot = "comissao", "Aprovado na Câmara"
        elif p["tramitando"] or (p["tramitando"] is None and sit):
            cls, rot = "tramita", ("Em tramitação" if projeto else (sit[:40] or "Em tramitação"))
        else:
            cls, rot = "encerrado", (sit.split(" - ")[0][:40] if sit else "Sem situação")
        ult = max([e["data"] for e in et if e["data"]] + ([p["data_situacao"]] if p["data_situacao"] else []),
                  default="")
        out.append({**p, "classe": cls, "rotulo": rot, "etapas": et, "ultima": ult, "papel": papel,
                    "principal": bool(p["principal"]) and autor, "autor": autor, "relator": relator,
                    "aprovado": cls in ("lei", "sancao", "senado", "comissao"), "projeto": projeto})
    ordem = {"lei": 0, "sancao": 1, "senado": 2, "comissao": 3, "tramita": 4, "encerrado": 5}
    # aprovados primeiro (norma → sanção → Senado → Câmara), depois em tramitação e encerrados; mais recente primeiro
    out.sort(key=lambda x: x["data_apresentacao"] or "", reverse=True)
    out.sort(key=lambda x: x["ultima"] or "", reverse=True)
    out.sort(key=lambda x: (ordem[x["classe"]], not x["principal"]))
    return out


def projetos_aprovados(cod):
    """Só os projetos (autoria/coautoria) aprovados."""
    return [p for p in projetos(cod) if p["aprovado"] and p["autor"]]


FILTROS = [("", "Todos"), ("aprovados", "Aprovados"), ("lei", "Viraram norma"), ("tramita", "Em tramitação"),
           ("encerrado", "Arquivados/encerrados"), ("autor", "1º autor"), ("coautor", "Coautor"),
           ("relator", "Relator")]


def filtrar(lista, f):
    regra = {"aprovados": lambda p: p["aprovado"], "lei": lambda p: p["classe"] == "lei",
             "tramita": lambda p: p["classe"] == "tramita", "encerrado": lambda p: p["classe"] == "encerrado",
             "autor": lambda p: p["principal"], "coautor": lambda p: p["autor"] and not p["principal"],
             "relator": lambda p: p["relator"]}.get(f)
    return [p for p in lista if regra(p)] if regra else list(lista)


def resumo(cod, todas=False):
    """Dados para a visão geral do ex-deputado."""
    import paineis
    with engine.connect() as c:
        r = c.execute(text("SELECT detalhe FROM perfis WHERE codigo = :c"), {"c": cod}).first()
        tot = c.execute(text("""SELECT COUNT(*) n,
                                SUM(CASE WHEN a.principal THEN 1 ELSE 0 END) autor,
                                SUM(CASE WHEN pr.sigla IN ('PL','PLP','PEC','PDL','PDC','PRC','PLV') THEN 1 ELSE 0 END) projetos
                                FROM processos pr JOIN autorias a ON a.processo_id = pr.id
                                WHERE a.codigo_deputado = :c"""), {"c": cod}).mappings().first()
    det = json.loads(r[0]) if r and r[0] else {}
    todos = projetos(cod, todas)
    aprov = [p for p in todos if p["aprovado"] and p["autor"]]
    cfg = config(cod)
    return {
        "perfil": paineis.perfil(cod, historico=True),
        "mandatos": mandatos(det),
        "periodo": (cfg.get("inicio"), cfg.get("fim")),
        "proposicoes": int(tot["n"] or 0), "como_autor": int(tot["autor"] or 0), "projetos": int(tot["projetos"] or 0),
        "aprovados": aprov, "todos": todos,
        "contagem": {k: len(filtrar(todos, k)) for k, _ in FILTROS},
        "n_lei": sum(a["classe"] == "lei" for a in aprov),
        "n_camara": sum(a["classe"] != "lei" for a in aprov),
        "ultima_carga": ultima_carga(cod),
        "carregando": estado["rodando"] and estado["cod"] == cod,
        "estado": dict(estado),
    }
