"""Cálculos do painel: governismo, oposicionismo, participação e fidelidade partidária.

Regras (iguais às do painel do Senado e do Power BI da Câmara):
- Alinhamento com uma liderança (Governo/Oposição) = votos do deputado iguais à orientação
  ÷ votos SIM/NÃO do deputado nas votações em que essa liderança orientou SIM ou NÃO.
  Orientação LIBERADO, abstenção, obstrução e Art. 17 ficam fora da conta.
- "Orientações opostas": votações em que Governo e Oposição orientaram em sentidos
  contrários (um SIM, outro NÃO). É o recorte que mais diferencia governistas de opositores.
- Participação = votações em que o deputado registrou voto (inclui obstrução e Art. 17)
  ÷ votações nominais realizadas entre o primeiro e o último voto dele no período.
- Fidelidade = votos iguais à orientação que valeu para o partido do deputado na data do voto
  (do próprio partido; se ele não orientou, da federação; senão, do bloco).
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import DateTime, bindparam, select
from sqlalchemy import text as _text

from db import INSTITUCIONAIS, deputados, engine, meta


@dataclass
class Filtros:
    inicio: date | None = None
    fim: date | None = None
    partido: str = ""
    uf: str = ""
    sigla: str = ""          # tipo de matéria: PL, PEC, MPV...
    opostas: bool = True     # só votações com orientações opostas Governo x Oposição

    @classmethod
    def from_args(cls, a):
        def _d(s):
            try:
                return date.fromisoformat(s) if s else None
            except ValueError:
                return None
        return cls(inicio=_d(a.get("inicio")), fim=_d(a.get("fim")),
                   partido=(a.get("partido") or "").strip(), uf=(a.get("uf") or "").upper(),
                   sigla=(a.get("sigla") or "").upper(),
                   opostas=cls._opostas(a))

    @staticmethod
    def _opostas(a):
        # o formulário manda um campo oculto "0" e, se marcado, também "1"
        vals = a.getlist("opostas") if hasattr(a, "getlist") else [a.get("opostas")]
        vals = [v for v in vals if v is not None]
        return ("1" in vals) if vals else True

    def where_votacoes(self, alias="vt"):
        cond, p = ["1=1"], {}
        if self.inicio:
            cond.append(f"{alias}.data >= :inicio")
            p["inicio"] = datetime.combine(self.inicio, datetime.min.time())
        if self.fim:
            cond.append(f"{alias}.data < :fim")
            p["fim"] = datetime.combine(self.fim + timedelta(days=1), datetime.min.time())
        if self.sigla:
            cond.append(f"{alias}.sigla_materia = :sigla")
            p["sigla"] = self.sigla
        if self.opostas:
            cond.append(f"""EXISTS (SELECT 1 FROM orientacoes g JOIN orientacoes o
                              ON o.votacao_id = g.votacao_id
                            WHERE g.votacao_id = {alias}.id AND g.lideranca = 'Governo'
                              AND o.lideranca = 'Oposição'
                              AND g.voto IN ('SIM','NÃO') AND o.voto IN ('SIM','NÃO')
                              AND g.voto <> o.voto)""")
        return " AND ".join(cond), p


def text(sql):
    """text() com tipos de data explícitos (funciona igual em SQLite e PostgreSQL)."""
    q = _text(sql)
    tipos = [bindparam(k, type_=DateTime) for k in ("inicio", "fim") if f":{k}" in sql]
    return q.bindparams(*tipos) if tipos else q


def _cadastro():
    with engine.connect() as c:
        rows = c.execute(select(deputados)).mappings().all()
    return {r["codigo"]: dict(r) for r in rows}


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def _enriquecer(linhas, cad, f: Filtros):
    """Junta cadastro (foto, partido atual, UF) e aplica filtros de partido/UF."""
    saida = []
    for r in linhas:
        s = cad.get(r["codigo"]) if r.get("codigo") else None
        item = dict(r)
        item["nome"] = (s or {}).get("nome") or r["nome"]
        item["partido"] = (s or {}).get("partido") or r.get("partido_voto") or ""
        item["uf"] = (s or {}).get("uf") or r.get("uf") or ""
        item["foto"] = (s or {}).get("foto")
        item["em_exercicio"] = bool(s.get("em_exercicio")) if s else None  # None = fora do cadastro
        if f.partido and item["partido"] != f.partido:
            continue
        if f.uf and item["uf"] != f.uf:
            continue
        saida.append(item)
    return saida


_POR_DEPUTADO = """v.codigo_deputado AS codigo, MAX(v.nome) AS nome,
               MAX(v.uf) AS uf, MAX(v.partido) AS partido_voto"""


def alinhamento(lideranca: str, f: Filtros, minimo: int = 1):
    where, p = f.where_votacoes()
    p["lid"] = lideranca
    sql = text(f"""
        SELECT {_POR_DEPUTADO},
               SUM(CASE WHEN v.voto = o.voto THEN 1 ELSE 0 END) AS seguiu,
               SUM(CASE WHEN v.voto <> o.voto THEN 1 ELSE 0 END) AS contra
        FROM votos v
        JOIN orientacoes o ON o.votacao_id = v.votacao_id AND o.lideranca = :lid
        JOIN votacoes vt ON vt.id = v.votacao_id
        WHERE o.voto IN ('SIM','NÃO') AND v.voto IN ('SIM','NÃO') AND {where}
        GROUP BY v.codigo_deputado""")
    with engine.connect() as c:
        linhas = [dict(r) for r in c.execute(sql, p).mappings()]
    for r in linhas:
        r["total"] = r["seguiu"] + r["contra"]
        r["pct"] = _pct(r["seguiu"], r["total"])
    linhas = [r for r in linhas if r["total"] >= minimo]
    out = _enriquecer(linhas, _cadastro(), f)
    out.sort(key=lambda r: (-(r["pct"] or 0), -r["total"], r["nome"]))
    return out


def alinhamento_agrupado(campo: str, f: Filtros):
    """% de votos seguindo Governo e Oposição, agregado por 'partido' (na data do voto) ou 'uf'."""
    assert campo in ("partido", "uf")
    where, p = f.where_votacoes()
    sql = text(f"""
        SELECT v.{campo} AS chave, o.lideranca AS lid,
               SUM(CASE WHEN v.voto = o.voto THEN 1 ELSE 0 END) AS seguiu,
               COUNT(*) AS total
        FROM votos v
        JOIN orientacoes o ON o.votacao_id = v.votacao_id
             AND o.lideranca IN ('Governo','Oposição')
        JOIN votacoes vt ON vt.id = v.votacao_id
        WHERE o.voto IN ('SIM','NÃO') AND v.voto IN ('SIM','NÃO') AND {where}
        GROUP BY v.{campo}, o.lideranca""")
    agg = {}
    with engine.connect() as c:
        for r in c.execute(sql, p).mappings():
            if not r["chave"]:
                continue
            d = agg.setdefault(r["chave"], {"chave": r["chave"]})
            k = "gov" if r["lid"] == "Governo" else "opo"
            d[k] = _pct(r["seguiu"], r["total"])
            d[k + "_n"] = r["total"]
    out = list(agg.values())
    out.sort(key=lambda d: (-(d.get("opo") or 0), d["chave"]))
    return out


def resumo(f: Filtros):
    base = Filtros(f.inicio, f.fim, "", "", f.sigla, opostas=False)
    where, p = base.where_votacoes()
    sql = text(f"""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN g.voto IN ('SIM','NÃO') AND o.voto IN ('SIM','NÃO') AND g.voto <> o.voto
                   THEN 1 ELSE 0 END) AS opostas,
          SUM(CASE WHEN g.voto IN ('SIM','NÃO') AND o.voto IN ('SIM','NÃO') AND g.voto = o.voto
                   THEN 1 ELSE 0 END) AS iguais
        FROM votacoes vt
        JOIN orientacoes g ON g.votacao_id = vt.id AND g.lideranca = 'Governo'
        JOIN orientacoes o ON o.votacao_id = vt.id AND o.lideranca = 'Oposição'
        WHERE {where}""")
    sql_tot = text(f"SELECT COUNT(*) AS n, SUM(CASE WHEN vt.aprovacao THEN 1 ELSE 0 END) AS aprov "
                   f"FROM votacoes vt WHERE {where}")
    with engine.connect() as c:
        r = c.execute(sql, p).mappings().first()
        t = c.execute(sql_tot, p).mappings().first()
    return {"votacoes": t["n"] or 0, "aprovadas": t["aprov"] or 0,
            "com_ambas": r["total"] or 0, "opostas": r["opostas"] or 0,
            "nao_opostas": r["iguais"] or 0}


def votacoes_orientadas(f: Filtros, limite: int = 200):
    where, p = f.where_votacoes()
    p["lim"] = limite
    sql = text(f"""
        SELECT vt.id, vt.data, vt.sigla_materia, vt.numero_materia, vt.ano_materia,
               vt.descricao_materia, vt.descricao_votacao, vt.dispositivo, vt.qtd_sim, vt.qtd_nao,
               g.voto AS gov, o.voto AS opo
        FROM votacoes vt
        LEFT JOIN orientacoes g ON g.votacao_id = vt.id AND g.lideranca = 'Governo'
        LEFT JOIN orientacoes o ON o.votacao_id = vt.id AND o.lideranca = 'Oposição'
        WHERE (g.voto IS NOT NULL OR o.voto IS NOT NULL) AND {where}
        ORDER BY vt.data DESC LIMIT :lim""")
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql, p).mappings()]


def participacao(f: Filtros):
    """Ignora o filtro de orientações opostas: participação vale para todas as votações."""
    base = Filtros(f.inicio, f.fim, f.partido, f.uf, f.sigla, opostas=False)
    where, p = base.where_votacoes()
    sql = text(f"""
        SELECT {_POR_DEPUTADO},
               MIN(vt.data) AS primeiro, MAX(vt.data) AS ultimo,
               SUM(CASE WHEN v.voto IN ('SIM','NÃO','ABSTENÇÃO','OBSTRUÇÃO','ART. 17','SECRETO')
                        THEN 1 ELSE 0 END) AS votou
        FROM votos v JOIN votacoes vt ON vt.id = v.votacao_id
        WHERE {where}
        GROUP BY v.codigo_deputado""")
    sql_datas = text(f"SELECT vt.data FROM votacoes vt WHERE {where} ORDER BY vt.data")
    with engine.connect() as c:
        linhas = [dict(r) for r in c.execute(sql, p).mappings()]
        datas = [r[0] for r in c.execute(sql_datas, p)]
    import bisect
    for r in linhas:
        i = bisect.bisect_left(datas, r["primeiro"])
        j = bisect.bisect_right(datas, r["ultimo"])
        r["realizadas"] = max(j - i, r["votou"])
        r["pct"] = _pct(r["votou"], r["realizadas"])
    out = _enriquecer(linhas, _cadastro(), f)
    out.sort(key=lambda r: (-(r["pct"] or 0), -r["votou"], r["nome"]))
    return out


def fidelidade(f: Filtros):
    """% de votos iguais à orientação que valeu para o partido do deputado (na data do voto)."""
    where, p = f.where_votacoes()
    sql = text(f"""
        SELECT {_POR_DEPUTADO},
               SUM(CASE WHEN v.voto = o.voto THEN 1 ELSE 0 END) AS seguiu,
               COUNT(*) AS total
        FROM votos v
        JOIN orientacoes_partido o ON o.votacao_id = v.votacao_id AND o.partido = v.partido
        JOIN votacoes vt ON vt.id = v.votacao_id
        WHERE o.voto IN ('SIM','NÃO') AND v.voto IN ('SIM','NÃO') AND {where}
        GROUP BY v.codigo_deputado""")
    with engine.connect() as c:
        linhas = [dict(r) for r in c.execute(sql, p).mappings()]
    for r in linhas:
        r["pct"] = _pct(r["seguiu"], r["total"])
    out = _enriquecer(linhas, _cadastro(), f)
    out.sort(key=lambda r: (-(r["pct"] or 0), -r["total"], r["nome"]))
    return out


def opcoes_filtro():
    with engine.connect() as c:
        partidos = [r[0] for r in c.execute(text(
            "SELECT DISTINCT partido FROM deputados WHERE partido IS NOT NULL ORDER BY partido"))]
        ufs = [r[0] for r in c.execute(text(
            "SELECT DISTINCT uf FROM deputados WHERE uf IS NOT NULL ORDER BY uf"))]
        siglas = [r[0] for r in c.execute(text(
            "SELECT sigla_materia, COUNT(*) n FROM votacoes WHERE sigla_materia IS NOT NULL "
            "GROUP BY sigla_materia ORDER BY n DESC"))]
        ult = c.execute(select(meta.c.valor).where(meta.c.chave == "ultima_coleta")).scalar()
    return {"partidos": partidos, "ufs": ufs, "siglas": siglas, "ultima_coleta": ult}


# ---------------- Aba Votações ----------------

def lista_votacoes(f: Filtros, busca: str = "", limite: int = 300):
    """Votações do período para o seletor (mais recentes primeiro)."""
    base = Filtros(f.inicio, f.fim, "", "", f.sigla, opostas=False)
    where, p = base.where_votacoes()
    p["lim"] = limite
    cond_busca = ""
    if busca:
        # aceita "PL 1087", "1087/2025", ou trecho da ementa / do que foi votado
        termos = busca.replace("/", " ").split()
        partes = []
        for i, t in enumerate(termos):
            k = f"b{i}"
            p[k] = f"%{t.lower()}%"
            partes.append(f"(LOWER(COALESCE(vt.descricao_materia,'')) LIKE :{k} "
                          f"OR LOWER(COALESCE(vt.descricao_votacao,'')) LIKE :{k} "
                          f"OR LOWER(COALESCE(vt.sigla_materia,'')) LIKE :{k} "
                          f"OR CAST(vt.numero_materia AS VARCHAR(12)) LIKE :{k} "
                          f"OR CAST(vt.ano_materia AS VARCHAR(6)) LIKE :{k})")
        cond_busca = " AND " + " AND ".join(partes)
    sql = text(f"""
        SELECT vt.id, vt.data, vt.sigla_materia, vt.numero_materia, vt.ano_materia,
               vt.descricao_materia, vt.descricao_votacao, vt.dispositivo, vt.secreta,
               vt.qtd_sim, vt.qtd_nao, vt.aprovacao
        FROM votacoes vt WHERE {where}{cond_busca}
        ORDER BY vt.data DESC LIMIT :lim""")
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql, p).mappings()]


def detalhe_votacao(vid: str):
    with engine.connect() as c:
        v = c.execute(text("SELECT * FROM votacoes WHERE id = :id"), {"id": vid}).mappings().first()
        if not v:
            return None
        v = dict(v)
        oris = [dict(r) for r in c.execute(text(
            "SELECT lideranca, voto FROM orientacoes WHERE votacao_id = :id"), {"id": vid}).mappings()]
        ori_por_partido = dict(c.execute(text(
            "SELECT partido, voto FROM orientacoes_partido WHERE votacao_id = :id"), {"id": vid}).all())
        vts = [dict(r) for r in c.execute(text(
            "SELECT nome, codigo_deputado AS codigo, partido AS partido_voto, uf, voto "
            "FROM votos WHERE votacao_id = :id"), {"id": vid}).mappings()]

    # lideranças institucionais primeiro, depois partidos, federações e blocos
    ordem = {nome: i for i, nome in enumerate(INSTITUCIONAIS)}
    oris.sort(key=lambda o: (ordem.get(o["lideranca"], 9), o["lideranca"]))
    por_lid = {o["lideranca"]: o["voto"] for o in oris}
    gov = por_lid.get("Governo")
    opo = por_lid.get("Oposição")

    cad = _cadastro()
    for r in vts:
        s = cad.get(r["codigo"]) if r["codigo"] else None
        r["nome"] = (s or {}).get("nome") or r["nome"]
        r["foto"] = (s or {}).get("foto")
        r["em_exercicio"] = bool(s.get("em_exercicio")) if s else None
        r["partido"] = r["partido_voto"]          # nesta aba vale o partido na data do voto
        op = ori_por_partido.get(r["partido_voto"])
        r["ori_partido"] = op
        r["seguiu_partido"] = (r["voto"] == op) if op in ("SIM", "NÃO") and r["voto"] in ("SIM", "NÃO") else None
        r["com_governo"] = (r["voto"] == gov) if gov in ("SIM", "NÃO") and r["voto"] in ("SIM", "NÃO") else None
        r["com_oposicao"] = (r["voto"] == opo) if opo in ("SIM", "NÃO") and r["voto"] in ("SIM", "NÃO") else None
    vts.sort(key=lambda r: (r["voto"], r["partido"] or "", r["nome"]))

    # placar por partido (na data do voto)
    por_partido = {}
    for r in vts:
        d = por_partido.setdefault(r["partido_voto"] or "—",
                                   {"partido": r["partido_voto"] or "—", "SIM": 0, "NÃO": 0, "outros": 0})
        if r["voto"] in ("SIM", "NÃO"):
            d[r["voto"]] += 1
        else:
            d["outros"] += 1
    for d in por_partido.values():
        d["orientacao"] = ori_por_partido.get(d["partido"])
        d["total"] = d["SIM"] + d["NÃO"] + d["outros"]
    placar = sorted(por_partido.values(), key=lambda d: (-d["total"], d["partido"]))

    # contagem a partir dos votos individuais (o arquivo às vezes traz os totais nulos)
    contagem = {"SIM": 0, "NÃO": 0, "ABSTENÇÃO": 0, "OBSTRUÇÃO": 0, "OUTROS": 0}
    for r in vts:
        contagem[r["voto"] if r["voto"] in contagem else "OUTROS"] += 1
    contagem["SIM"] = contagem["SIM"] or (v["qtd_sim"] or 0)
    contagem["NÃO"] = contagem["NÃO"] or (v["qtd_nao"] or 0)

    traidores = [r for r in vts if r["seguiu_partido"] is False]  # contra a orientação do próprio partido

    return {"votacao": v, "orientacoes": oris, "votos": vts, "placar": placar,
            "contagem": contagem, "gov": gov, "opo": opo, "traidores": traidores}
