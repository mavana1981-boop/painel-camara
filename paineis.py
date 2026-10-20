"""Consultas das abas Perfil, Proposições e Cota (e dos rankings que dependem delas)."""
import json
import os
from collections import defaultdict
from datetime import date

from sqlalchemy import text

import cota
import metrics
from db import engine

SIGLAS_PROJETO = ("PL", "PLP", "PEC", "PDL", "PRC")
INICIO_LEGISLATURA = date.fromisoformat(os.environ.get("INICIO_LEGISLATURA", "2023-02-01"))

# Cache em memória das consultas pesadas; invalida sozinho quando há coleta nova.
_cache = {}


def _versao_dados():
    with engine.connect() as c:
        return c.execute(text("SELECT valor FROM meta WHERE chave = 'ultima_coleta'")).scalar()


def limpar_cache():
    _cache.clear()


def _memo(fn):
    def _w(*a):
        chave = (fn.__name__, a, _versao_dados())
        if chave not in _cache:
            if len(_cache) > 2000:
                _cache.clear()
            _cache[chave] = fn(*a)
        return _cache[chave]
    _w.__name__ = fn.__name__
    return _w


# ---------------- deputados ----------------

def lista_deputados():
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT codigo, nome, partido, uf, foto, em_exercicio FROM deputados ORDER BY nome")).mappings()
        return [dict(r) for r in rows]


def deputado(codigo):
    with engine.connect() as c:
        r = c.execute(text("SELECT * FROM deputados WHERE codigo = :c"), {"c": codigo}).mappings().first()
    return dict(r) if r else None


def escolher_deputado(codigo_pedido, lista):
    """Deputado pedido; senão o primeiro em exercício em ordem alfabética."""
    cods = {s["codigo"] for s in lista}
    if codigo_pedido in cods:
        return codigo_pedido
    em = [s for s in lista if s["em_exercicio"]]
    return (em or lista or [{"codigo": None}])[0]["codigo"]


# ---------------- perfil ----------------

def _atual(x, hoje):
    fim = (x.get("dataFim") or "")[:10]
    return not fim or fim >= hoje.isoformat()


def perfil(codigo):
    with engine.connect() as c:
        r = c.execute(text("SELECT detalhe, orgaos, frentes FROM perfis WHERE codigo = :c"),
                      {"c": codigo}).mappings().first()
    out = {"basicos": {}, "comissoes": [], "cargos": [], "telefones": [], "frentes_lista": [],
           "frentes": 0, "grupos": 0}
    if not r:
        return out
    det = (json.loads(r["detalhe"] or "{}") or {}).get("dados") or {}
    st = det.get("ultimoStatus") or {}
    gab = st.get("gabinete") or {}
    nasc = det.get("dataNascimento")
    idade = None
    if nasc:
        try:
            dn = date.fromisoformat(nasc[:10])
            hoje = date.today()
            idade = hoje.year - dn.year - ((hoje.month, hoje.day) < (dn.month, dn.day))
        except ValueError:
            pass
    partes_gab = []
    if gab.get("sala") or gab.get("nome"):
        partes_gab.append(f"Gabinete {gab.get('sala') or gab.get('nome')}")
    if gab.get("predio"):
        partes_gab.append(f"Anexo {gab['predio']}")
    if gab.get("andar"):
        partes_gab.append(f"{gab['andar']}º andar")
    out["basicos"] = {
        "nome_completo": det.get("nomeCivil"),
        "email": gab.get("email") or st.get("email"),
        "pagina": f"https://www.camara.leg.br/deputados/{codigo}",
        "nascimento": nasc, "idade": idade,
        "naturalidade": det.get("municipioNascimento"), "uf_naturalidade": det.get("ufNascimento"),
        "endereco": ", ".join(partes_gab) or None,
        "escolaridade": det.get("escolaridade"),
        "situacao": st.get("situacao"), "condicao": st.get("condicaoEleitoral"),
        "redes": [x for x in det.get("redeSocial") or [] if isinstance(x, str) and x.startswith("http")],
    }
    if gab.get("telefone"):
        t = str(gab["telefone"]).strip()
        out["telefones"] = [t if t.startswith("(") else f"(61) {t}"]

    hoje = date.today()
    orgs = [x for x in json.loads(r["orgaos"] or "[]") or [] if isinstance(x, dict) and _atual(x, hoje)]
    vistos = set()
    for x in orgs:
        chave = (x.get("siglaOrgao"), x.get("titulo"))
        if chave in vistos:
            continue
        vistos.add(chave)
        nome = x.get("nomePublicacao") or x.get("nomeOrgao")
        titulo = (x.get("titulo") or "").strip()
        out["comissoes"].append({"sigla": x.get("siglaOrgao"), "nome": nome, "casa": "CD",
                                 "participacao": titulo, "desde": (x.get("dataInicio") or "")[:10]})
        if titulo and titulo not in ("Titular", "Suplente"):
            out["cargos"].append({"cargo": titulo, "sigla": x.get("siglaOrgao"), "nome": nome,
                                  "desde": (x.get("dataInicio") or "")[:10]})

    def _peso(n):
        n = (n or "").lower()
        if "frente" in n or "grupo parlamentar" in n:
            return 3
        if "mista" in n or "inquérito" in n or "especial" in n or "externa" in n:
            return 2
        return 0 if "permanente" in n or n.startswith("comissão de") else 1
    out["comissoes"].sort(key=lambda x: (_peso(x["nome"]), x["participacao"] != "Titular", x["sigla"] or ""))
    out["cargos"].sort(key=lambda x: (_peso(x["nome"]), x["sigla"] or ""))

    fr = [x for x in json.loads(r["frentes"] or "[]") or [] if isinstance(x, dict)]
    fr = [x for x in fr if not x.get("idLegislatura") or int(x["idLegislatura"]) == metrics_legislatura()]
    out["frentes_lista"] = sorted({(x.get("titulo") or "").strip() for x in fr} - {""})
    out["frentes"] = len(out["frentes_lista"])
    out["grupos"] = sum(1 for x in out["comissoes"] if "grupo parlamentar" in (x["nome"] or "").lower())
    return out


def metrics_legislatura():
    return int(os.environ.get("LEGISLATURA", "57"))


@_memo
def _indicadores_todos():
    f = metrics.Filtros(opostas=False)
    return {
        "gov": {r["codigo"]: r for r in metrics.alinhamento("Governo", metrics.Filtros(opostas=True)) if r.get("codigo")},
        "opo": {r["codigo"]: r for r in metrics.alinhamento("Oposição", metrics.Filtros(opostas=True)) if r.get("codigo")},
        "part": {r["codigo"]: r for r in metrics.participacao(f) if r.get("codigo")},
        "fid": {r["codigo"]: r for r in metrics.fidelidade(f) if r.get("codigo")},
    }


def indicadores_deputado(codigo):
    """Números de votação do deputado na legislatura (governismo, oposicionismo, participação)."""
    t = _indicadores_todos()
    res = {}
    for chave in ("gov", "opo"):
        linha = t[chave].get(codigo)
        res[chave] = linha["pct"] if linha else None
        res[chave + "_n"] = linha["total"] if linha else 0
    part = t["part"].get(codigo)
    res["participacao"] = part["pct"] if part else None
    res["votou"] = part["votou"] if part else 0
    res["realizadas"] = part["realizadas"] if part else 0
    fid = t["fid"].get(codigo)
    res["fidelidade"] = fid["pct"] if fid else None
    return res


# ---------------- proposições ----------------

def proposicoes(codigo, papel="", sigla="", situacao="", inicio=None, fim=None, busca="", limite=400):
    cond, p = ["a.codigo_deputado = :c"], {"c": codigo}
    if papel == "autor":
        cond.append("a.principal = :t")
        p["t"] = True
    elif papel == "coautor":
        cond.append("a.principal = :f")
        p["f"] = False
    if sigla:
        cond.append("pr.sigla = :sigla")
        p["sigla"] = sigla
    if situacao == "norma":
        cond.append("pr.virou_norma = :vn")
        p["vn"] = True
    elif situacao == "tramitando":
        cond.append("pr.tramitando = :tr")
        p["tr"] = True
    elif situacao == "encerrada":
        cond.append("pr.tramitando = :tr")
        p["tr"] = False
    if inicio:
        cond.append("pr.data_apresentacao >= :ini")
        p["ini"] = inicio.isoformat()
    if fim:
        cond.append("pr.data_apresentacao <= :fim")
        p["fim"] = fim.isoformat()
    if busca:
        cond.append("(LOWER(COALESCE(pr.ementa,'')) LIKE :b OR LOWER(pr.identificacao) LIKE :b)")
        p["b"] = f"%{busca.lower()}%"
    where = " AND ".join(cond)
    base = f"FROM processos pr JOIN autorias a ON a.processo_id = pr.id WHERE {where}"
    with engine.connect() as c:
        tot = c.execute(text(f"""SELECT COUNT(*) n,
                 SUM(CASE WHEN pr.virou_norma THEN 1 ELSE 0 END) normas,
                 SUM(CASE WHEN pr.tramitando THEN 1 ELSE 0 END) tram,
                 SUM(CASE WHEN a.principal THEN 1 ELSE 0 END) principal,
                 SUM(CASE WHEN pr.sigla IN {SIGLAS_PROJETO} THEN 1 ELSE 0 END) projetos
                 {base}"""), p).mappings().first()
        por_tipo = [dict(r) for r in c.execute(text(
            f"SELECT pr.sigla, MAX(pr.tipo_documento) tipo, COUNT(*) n {base} "
            f"GROUP BY pr.sigla ORDER BY n DESC"), p).mappings()]
        por_situacao = [dict(r) for r in c.execute(text(
            f"SELECT COALESCE(pr.situacao,'(sem situação)') situacao, COUNT(*) n {base} "
            f"GROUP BY COALESCE(pr.situacao,'(sem situação)') ORDER BY n DESC"), p).mappings()]
        por_ano = [dict(r) for r in c.execute(text(
            f"SELECT SUBSTR(pr.data_apresentacao,1,4) ano, COUNT(*) n {base} "
            f"GROUP BY SUBSTR(pr.data_apresentacao,1,4) ORDER BY ano DESC"), p).mappings()]
        por_conteudo = [dict(r) for r in c.execute(text(
            f"SELECT COALESCE(pr.tipo_conteudo,'—') conteudo, COUNT(*) n {base} "
            f"GROUP BY COALESCE(pr.tipo_conteudo,'—') ORDER BY n DESC"), p).mappings()]
        p["lim"] = limite
        itens = [dict(r) for r in c.execute(text(
            f"SELECT pr.*, a.principal {base} ORDER BY pr.data_apresentacao DESC, pr.id DESC LIMIT :lim"),
            p).mappings()]
    siglas = sorted({x["sigla"] for x in por_tipo if x["sigla"]})
    return {"total": tot["n"] or 0, "normas": tot["normas"] or 0, "tramitando": tot["tram"] or 0,
            "principal": tot["principal"] or 0, "projetos": tot["projetos"] or 0,
            "por_tipo": por_tipo, "por_situacao": por_situacao, "por_ano": por_ano,
            "por_conteudo": por_conteudo, "itens": itens, "siglas": siglas}


def ranking_proposicoes(inicio=None, fim=None, so_projetos=True, so_autor=True):
    cond, p = ["1=1"], {}
    if so_projetos:
        cond.append(f"pr.sigla IN {SIGLAS_PROJETO}")
    if so_autor:
        cond.append("a.principal = :t")
        p["t"] = True
    if inicio:
        cond.append("pr.data_apresentacao >= :ini")
        p["ini"] = inicio.isoformat()
    if fim:
        cond.append("pr.data_apresentacao <= :fim")
        p["fim"] = fim.isoformat()
    sql = text(f"""SELECT s.codigo, s.nome, s.partido, s.uf, s.foto, s.em_exercicio, COUNT(*) n,
                   SUM(CASE WHEN pr.virou_norma THEN 1 ELSE 0 END) normas
                   FROM autorias a JOIN processos pr ON pr.id = a.processo_id
                   JOIN deputados s ON s.codigo = a.codigo_deputado
                   WHERE {' AND '.join(cond)}
                   GROUP BY s.codigo, s.nome, s.partido, s.uf, s.foto, s.em_exercicio
                   ORDER BY n DESC, s.nome""")
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql, p).mappings()]


# ---------------- cota (CEAP) ----------------

def _expr(campo, parte):
    if engine.dialect.name == "sqlite":
        return f"CAST(strftime('{'%Y' if parte == 'ano' else '%m'}', {campo}) AS INTEGER)"
    return f"CAST(EXTRACT({'YEAR' if parte == 'ano' else 'MONTH'} FROM {campo}) AS INTEGER)"


@_memo
def _atividade():
    """{codigo: {ano: set(meses)}} com votos ou despesas registrados — base para estimar os meses no cargo."""
    ativ = defaultdict(lambda: defaultdict(set))
    with engine.connect() as c:
        for cod, a, m in c.execute(text(
                f"SELECT v.codigo_deputado, {_expr('vt.data', 'ano')}, {_expr('vt.data', 'mes')} "
                f"FROM votos v JOIN votacoes vt ON vt.id = v.votacao_id GROUP BY 1, 2, 3")):
            if a and m:
                ativ[cod][a].add(m)
        for cod, a, m in c.execute(text(
                "SELECT codigo_deputado, ano, mes FROM despesas WHERE mes IS NOT NULL GROUP BY 1, 2, 3")):
            if a and m:
                ativ[cod][a].add(m)
    return {k: {a: set(ms) for a, ms in v.items()} for k, v in ativ.items()}


def _meses_ativos(codigo, ano, em_exercicio):
    """Meses do ano em que o deputado estava no cargo (aproximação pela atividade registrada)."""
    hoje = date.today()
    ultimo_mes_ano = 12 if ano < hoje.year else hoje.month
    meses = _atividade().get(codigo, {}).get(ano, set())
    if not meses:
        return []
    ini = min(meses)
    if ini <= 3:
        ini = 1  # recesso de janeiro não deixa rastro de votação
    if ano == INICIO_LEGISLATURA.year:
        ini = max(ini, INICIO_LEGISLATURA.month)  # janeiro/2023 ainda era a legislatura anterior
    fim = ultimo_mes_ano if em_exercicio else max(meses)
    return list(range(ini, min(fim, ultimo_mes_ano) + 1))


def _resumo_ano(s, ano, gasto):
    meses = _meses_ativos(s["codigo"], ano, bool(s["em_exercicio"]))
    disponivel = cota.teto_periodo(s["uf"], ano, meses) if meses else 0
    return {"ano": ano, "meses": len(meses), "disponivel": disponivel, "gasto": round(gasto, 2),
            "economia": round(disponivel - gasto, 2) if disponivel else None,
            "teto_mes": cota.teto_mensal(s["uf"], ano, max(meses) if meses else 1),
            "pct_uso": round(100 * gasto / disponivel, 1) if disponivel else None}


@_memo
def cota_deputado(codigo, ano_ini=None, ano_fim=None):
    s = deputado(codigo)
    if not s:
        return None
    hoje = date.today()
    ano_ini = ano_ini or INICIO_LEGISLATURA.year
    ano_fim = ano_fim or hoje.year
    with engine.connect() as c:
        p = {"c": codigo, "a1": ano_ini, "a2": ano_fim}
        por_ano_mes = c.execute(text(
            "SELECT ano, mes, SUM(valor) v FROM despesas WHERE codigo_deputado = :c "
            "AND ano BETWEEN :a1 AND :a2 GROUP BY ano, mes"), p).all()
        cat = c.execute(text(
            "SELECT ano, tipo, SUM(valor) v FROM despesas WHERE codigo_deputado = :c "
            "AND ano BETWEEN :a1 AND :a2 GROUP BY ano, tipo"), p).all()
        forn = [dict(r) for r in c.execute(text(
            "SELECT fornecedor, MAX(cnpj_cpf) cnpj, SUM(valor) total, COUNT(*) n FROM despesas "
            "WHERE codigo_deputado = :c AND ano BETWEEN :a1 AND :a2 "
            "GROUP BY fornecedor ORDER BY total DESC LIMIT 25"), p).mappings()]
        recentes = [dict(r) for r in c.execute(text(
            "SELECT data, tipo, fornecedor, cnpj_cpf, documento, valor, url FROM despesas "
            "WHERE codigo_deputado = :c AND ano BETWEEN :a1 AND :a2 "
            "ORDER BY data DESC, id DESC LIMIT 60"), p).mappings()]

    anos = sorted({a for a, _, _ in por_ano_mes}, reverse=True)
    mensal = defaultdict(lambda: [0.0] * 12)
    for a, m, v in por_ano_mes:
        if m:
            mensal[a][m - 1] += v or 0
    resumo_anos = [_resumo_ano(s, a, sum(mensal[a])) for a in sorted(anos)]
    total = {k: round(sum((r[k] or 0) for r in resumo_anos), 2) for k in ("disponivel", "gasto", "economia")}
    total["pct_uso"] = round(100 * total["gasto"] / total["disponivel"], 1) if total["disponivel"] else None

    categorias = defaultdict(lambda: {"total": 0.0})
    for a, t, v in cat:
        d = categorias[t or "(sem tipo)"]
        d[a] = d.get(a, 0) + (v or 0)
        d["total"] += v or 0
    cats = sorted(({"tipo": k, **v} for k, v in categorias.items()), key=lambda x: -x["total"])
    maior_mes = max((max(v) for v in mensal.values()), default=0)
    return {"deputado": s, "anos": anos, "resumo": resumo_anos, "total": total,
            "mensal": {a: mensal[a] for a in anos}, "maior_mes": maior_mes,
            "categorias": cats, "fornecedores": forn, "recentes": recentes,
            "teto_atual": cota.teto_mensal(s["uf"], hoje.year, hoje.month)}


@_memo
def ranking_economia(ano_ini=None, ano_fim=None):
    hoje = date.today()
    a1, a2 = ano_ini or INICIO_LEGISLATURA.year, ano_fim or hoje.year
    with engine.connect() as c:
        gastos = defaultdict(dict)
        for cod, a, v in c.execute(text(
                "SELECT codigo_deputado, ano, SUM(valor) FROM despesas WHERE ano BETWEEN :a1 AND :a2 "
                "GROUP BY codigo_deputado, ano"), {"a1": a1, "a2": a2}):
            gastos[cod][a] = v or 0
    out = []
    for s in lista_deputados():
        if s["codigo"] not in gastos:
            continue
        linhas = [_resumo_ano(s, a, g) for a, g in gastos[s["codigo"]].items()]
        disp = round(sum(r["disponivel"] for r in linhas), 2)
        if not disp:
            continue
        gasto = round(sum(r["gasto"] for r in linhas), 2)
        out.append({**s, "gasto": gasto, "disponivel": disp, "economia": round(disp - gasto, 2),
                    "pct": round(100 * (disp - gasto) / disp, 1)})
    out.sort(key=lambda r: (-r["pct"], r["nome"]))
    return out


def gastos_por_categoria_geral(ano):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT tipo, SUM(valor) total FROM despesas WHERE ano = :a GROUP BY tipo ORDER BY total DESC"),
            {"a": ano}).mappings()]
