"""Testes do pipeline com fixtures no layout exato dos arquivos em lote da Câmara
(CSV UTF-8 com BOM, separador ';', aspas em todos os campos). Os dados são fictícios.

Rodar:  python -m pytest -q tests   (ou: python tests/test_pipeline.py)
"""
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_tmp}/teste.db")
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "senha-de-teste-123"
os.environ["SECRET_KEY"] = "teste"
os.environ.pop("RAILWAY_ENVIRONMENT", None)
os.environ.pop("CEAP_REAJUSTES", None)
sys.path.insert(0, str(RAIZ))

import collector  # noqa: E402
import metrics  # noqa: E402
from db import engine, init_db  # noqa: E402
from sqlalchemy import text  # noqa: E402

V1, V2, V3, V_CCJ, V_SIMB, V6, V7 = ("2500001-10", "2500001-12", "2500002-3", "2500003-1", "2500004-2",
                                    "2500005-7", "2500006-2")


def _zip(nome_csv):
    caminho = os.path.join(_tmp, nome_csv + ".zip")
    with zipfile.ZipFile(caminho, "w") as z:
        z.write(FIX / nome_csv, nome_csv)
    return caminho


def _carregar():
    init_db()
    docs = (json.loads((FIX / "deputados_legislatura.json").read_text())["dados"],
            json.loads((FIX / "deputados_atual.json").read_text())["dados"])
    ler = lambda n: collector.ler_csv(str(FIX / n))  # noqa: E731
    with engine.begin() as c:
        collector.coletar_deputados(conn=c, docs=docs)
        collector.gravar_votacoes_ano(c, 2025, ler("votacoes-2025.csv"), ler("votacoesVotos-2025.csv"),
                                      ler("votacoesOrientacoes-2025.csv"), ler("votacoesProposicoes-2025.csv"))
        for ano in (2023, 2025):
            collector.gravar_ceaps(c, ano, collector.parse_ceap_csv(collector.ler_csv(_zip(f"Ano-{ano}.csv"))))
        cad = {r[0] for r in c.execute(text("SELECT codigo FROM deputados"))}
        collector.gravar_processos_ano(c, 2025, ler("proposicoes-2025.csv"), ler("proposicoesAutores-2025.csv"), cad)
        collector.gravar_perfil(c, 101, json.loads((FIX / "deputado_101.json").read_text()),
                                json.loads((FIX / "deputado_101_orgaos.json").read_text()),
                                json.loads((FIX / "deputado_101_frentes.json").read_text())["dados"])
        collector.limpar_deputados_sem_atividade(c)
        collector.marcar_coleta(c)


_carregar()


def _anonimo():
    import app as painel
    return painel.app.test_client()


def _cliente():
    cli = _anonimo()
    html = cli.get("/login").get_data(as_text=True)
    tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    r = cli.post("/login", data={"_csrf": tok, "login": "admin", "senha": "senha-de-teste-123"})
    assert r.status_code == 302, "login falhou"
    return cli


def _q(sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).all()


# ---------------- coleta ----------------

def test_cadastro_de_deputados():
    deps = {r[0]: r for r in _q("SELECT codigo, nome, partido, uf, em_exercicio, foto FROM deputados")}
    assert 109 not in deps                       # fora de exercício e sem voto/despesa: removido
    assert deps[108][4] in (0, False)            # suplente que votou: fica, marcado como fora
    assert deps[101][4] in (1, True) and deps[103][2] == "PCdoB"
    assert deps[101][5].endswith("/bandep/101.jpg")
    assert _q("SELECT nome_completo FROM deputados WHERE codigo=101")[0][0] == "ANA MARIA DE TESTE OPOSIÇÃO"


def test_so_votacoes_nominais_do_plenario():
    ids = {r[0] for r in _q("SELECT id FROM votacoes")}
    assert ids == {V1, V2, V3, V6, V7}           # sem a da CCJC e sem a simbólica
    sim, nao, n = _q("SELECT SUM(CASE WHEN voto='SIM' THEN 1 ELSE 0 END), "
                     "SUM(CASE WHEN voto='NÃO' THEN 1 ELSE 0 END), COUNT(*) FROM votos WHERE votacao_id=:v", v=V1)[0]
    assert (sim, nao, n) == (3, 3, 8)            # linha repetida no arquivo não duplica o voto
    v = dict(_q("SELECT id, presidente FROM votacoes")).get(V1)
    assert v == "Elisa Presidente"               # "Artigo 17" = presidente da sessão


def test_proposicao_principal_da_votacao():
    sig, num, ano, disp = _q("SELECT sigla_materia, numero_materia, ano_materia, dispositivo FROM votacoes "
                             "WHERE id=:v", v=V2)[0]
    assert (sig, num, ano) == ("PL", 1087, 2025)  # a votação é de um REQ, mas a matéria é o PL
    assert disp == "REQ 456/2025"
    assert _q("SELECT sigla_materia FROM votacoes WHERE id=:v", v=V7)[0][0] == "MPV"


def test_orientacoes_normalizadas():
    oris = dict(_q("SELECT lideranca, voto FROM orientacoes WHERE votacao_id=:v", v=V2))
    assert oris["Governo"] == "NÃO" and oris["Oposição"] == "SIM"     # arquivo manda "GOVERNO"
    assert oris["Fdr PSOL-REDE"] == "LIVRE"                          # "Liberado"


def test_orientacao_que_vale_para_cada_partido():
    op = {p: (v, f) for p, v, f in _q("SELECT partido, voto, fonte FROM orientacoes_partido WHERE votacao_id=:v", v=V1)}
    assert op["PT"] == ("SIM", "PT")                                  # própria vence a federação
    assert op["PCdoB"] == ("SIM", "Fdr PT-PCdoB-PV")                  # sem orientação própria: federação
    assert op["MDB"] == ("SIM", "Bl MdbPsdRepPode")                   # bloco
    assert op["REPUBLICANOS"] == ("NÃO", "Republican")                # rótulo truncado, mais específico que o bloco
    assert op["PSOL"][1] == "Fdr PSOL-REDE"
    assert "AVANTE" not in op                                         # ninguém orientou pelo Avante aqui
    # bloco de nome truncado ("Bl PdtPsb...") ganha o Avante por dedução
    assert dict(_q("SELECT partido, fonte FROM orientacoes_partido WHERE votacao_id=:v", v=V6))["AVANTE"] == "Bl PdtPsb..."


def test_membros_bancada():
    p = {"PT", "PCdoB", "PV", "REPUBLICANOS", "SOLIDARIEDADE", "PODE", "UNIÃO"}
    assert collector.membros_bancada("Fdr PT-PCdoB-PV", p)[0] == {"PT", "PCdoB", "PV"}
    assert collector.membros_bancada("Solidaried", p)[0] == {"SOLIDARIEDADE"}
    assert collector.membros_bancada("Podemos", p)[0] == {"PODE"}
    assert collector.membros_bancada("Bl UniPpPsd...", p)[0] >= {"UNIÃO", "PP", "PSD"}
    assert collector.membros_bancada("Governo", p)[0] is None


# ---------------- cálculos ----------------

def test_governismo_oposicionismo():
    f = metrics.Filtros(opostas=True)                                 # só V1 e V2
    opo = {r["codigo"]: r for r in metrics.alinhamento("Oposição", f)}
    gov = {r["codigo"]: r for r in metrics.alinhamento("Governo", f)}
    assert (opo[101]["seguiu"], opo[101]["total"], opo[101]["pct"]) == (2, 2, 100.0)
    assert gov[101]["pct"] == 0.0 and gov[102]["pct"] == 100.0
    assert opo[107]["total"] == 1                                     # obstrução fica fora
    assert 105 not in opo                                             # Art. 17 fica fora
    assert gov[106]["total"] == 1                                     # abstenção fica fora
    todos = {r["codigo"]: r for r in metrics.alinhamento("Oposição", metrics.Filtros(opostas=False))}
    assert todos[101]["total"] == 5                                   # V3/V6/V7 entram sem o filtro
    r = metrics.resumo(metrics.Filtros())
    assert (r["votacoes"], r["com_ambas"], r["opostas"], r["nao_opostas"]) == (5, 5, 2, 1)


def test_filtros_partido_uf_e_agrupados():
    f = metrics.Filtros(partido="PL", opostas=False)
    assert {r["codigo"] for r in metrics.alinhamento("Oposição", f)} == {101, 108}
    assert {r["codigo"] for r in metrics.alinhamento("Governo", metrics.Filtros(uf="BA", opostas=False))} == {102}
    por_p = {d["chave"]: d for d in metrics.alinhamento_agrupado("partido", metrics.Filtros())}
    assert por_p["PL"]["opo"] == 100.0 and por_p["PT"]["gov"] == 100.0


def test_fidelidade_e_participacao():
    fid = {r["codigo"]: r for r in metrics.fidelidade(metrics.Filtros(opostas=False))}
    assert (fid[104]["seguiu"], fid[104]["total"]) == (2, 3)          # MDB: contra o bloco na V1
    assert (fid[107]["seguiu"], fid[107]["total"]) == (1, 2)          # Avante pelo bloco deduzido
    assert fid[103]["pct"] == 100.0                                   # PCdoB pela federação
    part = {r["codigo"]: r for r in metrics.participacao(metrics.Filtros())}
    assert part[105]["votou"] == 2                                    # Art. 17 conta presença
    assert part[108]["realizadas"] == 1                               # só votou na V1: denominador 1
    assert part[101]["pct"] == 100.0


def test_aba_votacoes():
    d = metrics.detalhe_votacao(V1)
    assert d["contagem"]["SIM"] == 3 and d["contagem"]["NÃO"] == 3 and d["contagem"]["OBSTRUÇÃO"] == 1
    assert d["gov"] == "SIM" and d["opo"] == "NÃO"
    assert [o["lideranca"] for o in d["orientacoes"][:3]] == ["Governo", "Oposição", "Minoria"]
    mdb = next(p for p in d["placar"] if p["partido"] == "MDB")
    assert mdb["orientacao"] == "SIM" and mdb["NÃO"] == 1
    assert {r["codigo"] for r in d["traidores"]} == {104}
    assert {v["id"] for v in metrics.lista_votacoes(metrics.Filtros(), "PL 1087")} == {V1, V2}
    assert {v["id"] for v in metrics.lista_votacoes(metrics.Filtros(), "retirada")} == {V2}
    assert metrics.lista_votacoes(metrics.Filtros(), "inexistentexyz") == []
    cli = _cliente()
    html = cli.get("/votacoes").get_data(as_text=True)
    assert "MPV 1300/2025" in html and "Votos nominais" in html       # padrão = mais recente
    html = cli.get(f"/votacoes?id={V2}").get_data(as_text=True)
    assert "PL 1087/2025" in html and "REQ 456/2025" in html and "rejeitada" in html
    assert cli.get("/votacoes?busca=1087/2025").status_code == 200
    assert cli.get("/votacoes?id=999-1").status_code == 200


# ---------------- cota ----------------

def test_tetos_ceap():
    import cota
    assert cota.teto_mensal("DF", 2023, 6) == 36582.46
    assert cota.teto_mensal("RR", 2025, 12) == 51406.33
    assert cota.teto_mensal("RR", 2023, 1) == round(51406.33 - 5793.80, 2)       # antes de fev/2023
    assert cota.teto_mensal("RR", 2026, 3) == round(51406.33 * 1.1375, 2)        # ~R$ 58,4 mil
    assert cota.teto_mensal("RR", 2026, 2) == 51406.33


def test_ceap_e_cota():
    import paineis
    linhas = _q("SELECT codigo_deputado, ano, mes, valor FROM despesas ORDER BY ano, mes")
    assert all(cod in (101, 102) for cod, *_ in linhas)                # sem a liderança
    assert (101, 2023, 1, 300.0) not in linhas                         # jan/2023 = legislatura anterior
    d = paineis.cota_deputado(101)
    assert d["total"]["gasto"] == round(200 + 15000 + 1234.56 + 2765.94 - 500, 2)
    r23 = next(r for r in d["resumo"] if r["ano"] == 2023)
    assert r23["meses"] == 11                                          # fev a dez/2023
    r25 = next(r for r in d["resumo"] if r["ano"] == 2025)
    assert r25["meses"] == 12 and r25["disponivel"] == round(12 * 42837.33, 2)
    assert d["categorias"][0]["tipo"] == "DIVULGAÇÃO DA ATIVIDADE PARLAMENTAR"
    rk = {x["codigo"]: x for x in paineis.ranking_economia()}
    assert rk[101]["gasto"] == d["total"]["gasto"] and rk[101]["disponivel"] == d["total"]["disponivel"]
    # plano B: API por deputado
    api = collector.parse_ceap_api(101, "Ana", [{"ano": 2025, "mes": 7, "tipoDespesa": "COMBUSTÍVEIS.",
                                                 "valorLiquido": 99.9, "dataDocumento": "2025-07-01T00:00:00",
                                                 "nomeFornecedor": "POSTO"}])
    assert api[0]["valor"] == 99.9 and api[0]["tipo"] == "COMBUSTÍVEIS"


# ---------------- proposições e perfil ----------------

def test_proposicoes():
    import paineis
    p = paineis.proposicoes(101)
    assert p["total"] == 4 and p["principal"] == 3       # PEC 45 é do Bruno; EMC fica fora; apoio não conta
    assert p["normas"] == 1 and p["tramitando"] == 2
    assert p["projetos"] == 3                            # PL, PEC, PDL (REQ não conta)
    assert paineis.proposicoes(101, papel="coautor")["total"] == 1
    assert paineis.proposicoes(101, situacao="norma")["itens"][0]["identificacao"] == "PL 1087/2025"
    assert paineis.proposicoes(101, busca="decreto")["total"] == 1
    assert paineis.proposicoes(106)["total"] == 1
    assert not _q("SELECT id FROM processos WHERE id IN (2440099, 2440200)")   # emenda e PL do Senado
    rk = paineis.ranking_proposicoes()
    assert rk[0]["codigo"] == 101 and rk[0]["n"] == 2    # PL + PDL como 1º autor
    # o cartão do perfil usa o mesmo critério do ranking
    pr = paineis.proposicoes(101)
    assert pr["projetos_autor"] == rk[0]["n"] and pr["normas_autor"] == (rk[0]["normas"] or 0)
    todas = {r["codigo"]: r["n"] for r in paineis.ranking_proposicoes(so_projetos=False)}
    assert todas[101] == pr["principal"]                 # inclui o REQ


def test_perfil():
    import paineis
    pf = paineis.perfil(101)
    b = pf["basicos"]
    assert b["naturalidade"] == "Campinas" and b["idade"] and b["endereco"] == "Gabinete 512, Anexo 4, 5º andar"
    assert pf["telefones"] == ["(61) 3215-5512"]
    siglas = {c["sigla"] for c in pf["comissoes"]}
    assert "CCJC" in siglas and "CFT" not in siglas                         # encerradas ficam fora
    assert {c["cargo"] for c in pf["cargos"]} == {"Presidente", "1º Vice-Presidente"}
    assert pf["frentes"] == 2                                               # só da 57ª
    ind = paineis.indicadores_deputado(101)
    assert ind["opo"] == 100.0 and ind["participacao"] == 100.0


# ---------------- páginas e segurança ----------------

def test_paginas_renderizam():
    cli = _cliente()
    for url in ["/orientacoes", "/orientacoes?opostas=0", "/orientacoes?partido=PL&opostas=0&opostas=1",
                "/rankings", "/rankings?uf=SP", "/rankings?tipo_prop=todas&uf=SP", "/perfil", "/perfil?deputado=101", "/perfil?deputado=108",
                "/perfil?deputado=999999", "/proposicoes?deputado=101",
                "/proposicoes?deputado=101&papel=autor&situacao=norma", "/cota?deputado=101",
                "/cota?deputado=101&ano=2025", "/cota?deputado=108", "/conta", "/usuarios", "/saude"]:
        r = cli.get(url)
        assert r.status_code == 200, url
    html = cli.get("/orientacoes").get_data(as_text=True)
    assert "Ana Oposição" in html and "Painel da Câmara" in html and "deputados" in html
    html = cli.get("/cota?deputado=101").get_data(as_text=True)
    assert "GRAFICA EXEMPLO LTDA" in html and "R$ 15.000,00" in html
    html = cli.get("/proposicoes?deputado=101").get_data(as_text=True)
    assert "PL 1087/2025" in html and "Transformado em Norma" in html
    html = cli.get("/perfil?deputado=101").get_data(as_text=True)
    assert "Campinas" in html and "camara.leg.br" in html and "Presidente" in html
    html = cli.get("/rankings").get_data(as_text=True)
    assert "Deputado(a)" in html and "Senador" not in html and "Apresentação de projetos" in html
    html = cli.get("/rankings?tipo_prop=todas").get_data(as_text=True)
    assert "Proposições apresentadas" in html and 'name="tipo_prop" value="todas"' in html
    html = cli.get("/perfil?deputado=101").get_data(as_text=True)
    assert "Projetos como 1º autor" in html and "proposições no total" in html
    assert cli.get("/inexistente").status_code == 404
    assert _anonimo().post("/api/coletar").status_code == 403


def test_login_obrigatorio_e_seguranca():
    anon = _anonimo()
    for url in ["/", "/orientacoes", "/perfil", "/cota", "/usuarios"]:
        r = anon.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"], url
    assert anon.get("/saude").status_code == 200
    for est in ("style.css", "favicon.svg", "plenario.svg"):
        assert anon.get(f"/static/{est}").status_code == 200
    assert anon.get("/static/app.py").status_code == 404
    r = anon.post("/login", data={"login": "admin", "senha": "senha-de-teste-123"})
    assert r.status_code == 200 and "novamente" in r.get_data(as_text=True)   # sem CSRF não entra
    html = anon.get("/login").get_data(as_text=True)
    assert "plenario.svg" in html and "Painel da Câmara" in html
    tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    r = anon.post("/login", data={"_csrf": tok, "login": "admin", "senha": "errada"})
    assert "inválidos" in r.get_data(as_text=True)
    r = anon.post("/login", data={"_csrf": tok, "login": "admin", "senha": "senha-de-teste-123",
                                  "proximo": "https://site-malicioso.com"})
    assert r.headers["Location"].endswith("/") or "orientacoes" in r.headers["Location"]


def test_gestao_de_usuarios():
    cli = _cliente()
    html = cli.get("/usuarios").get_data(as_text=True)
    tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    cli.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": "Assessor",
                                "nome": "Assessor", "senha": "outra-senha-1"})
    assert "assessor" in cli.get("/usuarios").get_data(as_text=True)
    c2 = _anonimo()
    t2 = re.search(r'name="_csrf" value="([^"]+)"', c2.get("/login").get_data(as_text=True)).group(1)
    assert c2.post("/login", data={"_csrf": t2, "login": "assessor", "senha": "outra-senha-1"}).status_code == 302
    assert c2.get("/orientacoes").status_code == 200
    assert c2.get("/usuarios").status_code == 403
    uid = _q("SELECT id FROM usuarios WHERE login='assessor'")[0][0]
    cli.post("/usuarios", data={"_csrf": tok, "acao": "alternar", "id": uid})
    assert c2.get("/orientacoes").status_code == 302            # desativado perde a sessão na hora


# ---------------- robustez da coleta ----------------

def test_coleta_recupera_anos_que_falharam():
    """Se o servidor da Câmara cair num ano, os demais seguem e o ano fica pendente para a próxima coleta."""
    falhar = {2024}
    original = collector.baixar

    def fake(url):
        ano = int(re.search(r"-(\d{4})\.csv", url).group(1))
        if ano in falhar:
            raise RuntimeError("HTTP 503")
        if ano == 2026:
            return None                                          # arquivo ainda não publicado
        nome = url.rsplit("/", 1)[1]
        destino = os.path.join(_tmp, "baixado-" + nome)
        Path(destino).write_bytes((FIX / nome).read_bytes())
        return destino
    collector.baixar = fake
    try:
        collector.coletar_votacoes([2024, 2025, 2026])
        assert collector._anos_pendentes("votacoes_anos_pendentes") == {2024}
        assert len(_q("SELECT id FROM votacoes")) == 5           # 2025 recarregado sem duplicar
        falhar.clear()
        orig_fix = FIX / "votacoes-2024.csv"
        try:   # na coleta seguinte 2024 volta (aqui: arquivo vazio de cabeçalho)
            orig_fix.write_text((FIX / "votacoes-2025.csv").read_text(encoding="utf-8-sig").splitlines()[0] + "\n",
                                encoding="utf-8-sig")
            for n in ("votacoesVotos", "votacoesOrientacoes", "votacoesProposicoes"):
                (FIX / f"{n}-2024.csv").write_text((FIX / f"{n}-2025.csv").read_text(encoding="utf-8-sig")
                                                   .splitlines()[0] + "\n", encoding="utf-8-sig")
            collector.coletar_votacoes([2025])
            assert collector._anos_pendentes("votacoes_anos_pendentes") == set()
        finally:
            for n in ("votacoes", "votacoesVotos", "votacoesOrientacoes", "votacoesProposicoes"):
                (FIX / f"{n}-2024.csv").unlink(missing_ok=True)
    finally:
        collector.baixar = original
    assert len(_q("SELECT id FROM votacoes")) == 5


def test_anos_da_rotina():
    from datetime import date as d
    assert collector.anos_rotina(d(2026, 10, 20)) == [2026]
    assert collector.anos_rotina(d(2026, 1, 10)) == [2025, 2026]
    assert collector.anos_ceap_rotina(d(2026, 3, 5)) == [2025, 2026]
    assert collector.anos_ceap_rotina(d(2026, 4, 1)) == [2026]


def test_agendador_diario():
    from datetime import datetime, timedelta, timezone
    import app as painel
    brt = timezone(timedelta(hours=-3))
    manha = datetime(2026, 10, 21, 7, 0, tzinfo=brt)
    assert painel.precisa_atualizar(manha, "2026-10-20T09:00:00")
    assert not painel.precisa_atualizar(manha, "2026-10-21T09:30:00")
    assert not painel.precisa_atualizar(datetime(2026, 10, 21, 4, 0, tzinfo=brt), "2026-10-19T09:00:00")


if __name__ == "__main__":
    for nome, fn in list(globals().items()):
        if nome.startswith("test_"):
            fn()
            print("ok", nome)
