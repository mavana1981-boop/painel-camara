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



# ======================= monitor de situação =======================

def _reler_proposicoes(notificar, hoje=None, trocar=None, extra=None):
    """Relê o arquivo de 2025 das fixtures, com situações trocadas e/ou proposição extra."""
    def props():
        for r in collector.ler_csv(str(FIX / "proposicoes-2025.csv")):
            if trocar and r["id"] in trocar:
                r = {**r, "ultimoStatus_descricaoSituacao": trocar[r["id"]][0],
                     "ultimoStatus_dataHora": trocar[r["id"]][1]}
            yield r
        if extra:
            yield extra[0]

    def autores():
        yield from collector.ler_csv(str(FIX / "proposicoesAutores-2025.csv"))
        if extra:
            yield from extra[1]
    with engine.begin() as c:
        cad = {r[0] for r in c.execute(text("SELECT codigo FROM deputados"))}
        collector.gravar_processos_ano(c, 2025, props(), autores(), cad, notificar, hoje)


def test_monitor_de_situacao_gera_notificacoes():
    from datetime import date, timedelta
    import paineis
    hoje = date.today()
    _reler_proposicoes(True)                                     # nada mudou: nada a notificar
    assert _q("SELECT COUNT(*) FROM notificacoes")[0][0] == 0
    base = dict(next(collector.ler_csv(str(FIX / "proposicoes-2025.csv"))))
    nova = {**base, "id": "2499999", "siglaTipo": "PL", "numero": "4321", "ementa": "Proposição recém-apresentada.",
            "dataApresentacao": f"{hoje - timedelta(days=2)}T10:00", "ultimoStatus_descricaoSituacao": "Aguardando Despacho do Presidente da Câmara dos Deputados",
            "ultimoStatus_dataHora": f"{hoje - timedelta(days=2)}T10:00"}
    autor = {"idProposicao": "2499999", "idDeputadoAutor": "101", "nomeAutor": "Ana Oposição",
             "ordemAssinatura": "1", "proponente": "1", "codTipoAutor": "10000", "tipoAutor": "Deputado(a)"}
    _reler_proposicoes(True, trocar={"2440030": ("Aguardando Parecer do Relator na Comissão de Constituição e Justiça e de Cidadania (CCJC)", f"{hoje}T09:00"),
                                     "2470045": ("Pronta para Pauta no Plenário", f"{hoje}T09:00")},
                       extra=(nova, [autor]))
    n101 = paineis.notificacoes(101)
    tipos = {(i["identificacao"], i["tipo"]) for i in n101["itens"]}
    assert tipos == {("PDL 30/2025", "situacao"), ("PEC 45/2025", "situacao"), ("PL 4321/2025", "nova")}
    pdl = next(i for i in n101["itens"] if i["identificacao"] == "PDL 30/2025")
    assert pdl["situacao_anterior"] == "Aguardando Designação de Relator" and "CCJC" in pdl["situacao_nova"]
    assert pdl["url"].endswith("/2440030") and n101["recentes"] == 3
    # a PEC 45 é do Bruno (1º autor) com a Ana como coautora: os dois são avisados
    assert {i["identificacao"] for i in paineis.notificacoes(102)["itens"]} == {"PEC 45/2025"}
    # proposição antiga que só agora apareceu no arquivo não vira "nova"
    velha = {**nova, "id": "2499998", "numero": "4320", "dataApresentacao": "2025-03-01T10:00"}
    _reler_proposicoes(True, trocar={"2440030": ("Aguardando Parecer do Relator na Comissão de Constituição e Justiça e de Cidadania (CCJC)", f"{hoje}T09:00"),
                                     "2470045": ("Pronta para Pauta no Plenário", f"{hoje}T09:00")},
                       extra=(velha, [{**autor, "idProposicao": "2499998"}]))
    assert len(paineis.notificacoes(101)["itens"]) == 3
    with engine.begin() as c:
        collector.limpar_notificacoes(c, hoje=hoje + timedelta(days=collector.DIAS_NOTIFICACOES + 1))
    assert paineis.notificacoes(101)["itens"] == []
    _reler_proposicoes(False)                                    # volta às fixtures originais
    assert paineis.proposicoes(101)["total"] == 4
    assert collector.anos_legislatura(date(2026, 10, 27)) == [2023, 2024, 2025, 2026]


def test_dias_parado_e_votos_oposicao():
    from datetime import date
    import paineis
    assert paineis.dias_parado({"tramitando": True, "data_situacao": "2025-09-01"}, date(2025, 9, 11)) == 10
    assert paineis.dias_parado({"tramitando": False, "data_situacao": "2025-09-01"}) is None
    itens = paineis.proposicoes(101, situacao="tramitando")["itens"]
    assert itens and all(i["dias_parado"] is not None for i in itens)
    vo = paineis.votos_oposicao(101, metrics.Filtros(opostas=False))
    assert vo["total"] == len(vo["seguiu"]) + len(vo["contra"]) and vo["total"] >= 1
    assert all(v["voto"] in ("SIM", "NÃO") for v in vo["seguiu"])


# ======================= Meu Parlamentar =======================

def _cliente_usuario(login, senha):
    cli = _anonimo()
    tok = re.search(r'name="_csrf" value="([^"]+)"', cli.get("/login").get_data(as_text=True)).group(1)
    r = cli.post("/login", data={"_csrf": tok, "login": login, "senha": senha})
    assert r.status_code == 302, "login falhou"
    return cli


def _tok(cli, url):
    return re.search(r'name="_csrf" value="([^"]+)"', cli.get(url).get_data(as_text=True)).group(1)


def test_meu_parlamentar_acesso_por_vinculo():
    adm = _cliente()
    tok = _tok(adm, "/usuarios")
    adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": "assessora",
                                "nome": "Assessora", "senha": "senha-segura-1"})
    uid = _q("SELECT id FROM usuarios WHERE login='assessora'")[0][0]
    u = _cliente_usuario("assessora", "senha-segura-1")
    assert "Meu parlamentar" not in u.get("/orientacoes").get_data(as_text=True)
    assert u.get("/meu/").status_code == 403 and u.get("/meu/101").status_code == 403
    r = adm.post("/usuarios", data={"_csrf": tok, "acao": "vincular", "id": uid, "deputados": ["101"]})
    assert r.status_code == 302
    assert "1 deputado(s)" in adm.get("/usuarios").get_data(as_text=True)
    assert "Meu parlamentar" in u.get("/orientacoes").get_data(as_text=True)
    r = u.get("/meu/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/meu/101")
    html = u.get("/meu/101").get_data(as_text=True)
    assert "Ana Oposição" in html and "Trocar deputado" not in html
    assert u.get("/meu/102").status_code == 403           # não vinculado
    assert adm.get("/meu/102").status_code == 200          # admin vê todos
    assert adm.get("/meu/999999").status_code == 404
    html = adm.get("/meu/").get_data(as_text=True)
    assert "escolha o deputado" in html and "Bruno Governo" in html
    # excluir o usuário apaga os vínculos
    adm.post("/usuarios", data={"_csrf": tok, "acao": "excluir", "id": uid})
    assert not _q("SELECT * FROM usuario_deputados WHERE usuario_id = :u", u=uid)


def _evento(eid, dia, hora, sigla, tipo, apelido=None, situacao="Convocada"):
    return {"id": eid, "uri": f"https://dadosabertos.camara.leg.br/api/v2/eventos/{eid}",
            "dataHoraInicio": f"{dia}T{hora}", "dataHoraFim": None, "situacao": situacao,
            "descricaoTipo": tipo, "descricao": f"{tipo} da {sigla}", "localExterno": None,
            "orgaos": [{"id": 1, "sigla": sigla, "nome": apelido or sigla, "apelido": apelido or sigla,
                        "codTipoOrgao": 2, "tipoOrgao": "Comissão Permanente", "nomePublicacao": apelido or sigla}],
            "localCamara": {"nome": "Anexo II, Plenário 01", "predio": None, "sala": None, "andar": None},
            "urlRegistro": None}


def _item_pauta(ordem, pid, sigla, num, ano, relator=None, situacao=None, ementa="Dispõe sobre X."):
    return {"ordem": ordem, "topico": "Proposições Sujeitas à Apreciação Conclusiva", "regime": "Ordinário",
            "titulo": f"{sigla} {num}/{ano}",
            "proposicao_": {"id": pid, "siglaTipo": sigla, "codTipo": 139, "numero": num, "ano": ano, "ementa": ementa},
            "relator": ({"id": relator[0], "nome": relator[1], "siglaPartido": relator[2], "siglaUf": relator[3]}
                        if relator else None),
            "textoParecer": None, "proposicaoRelacionada_": None, "uriVotacao": None, "situacaoItem": situacao}


def test_agenda_pautas_e_relatorias():
    from datetime import date, timedelta
    import coleta_meu
    import meu
    hoje = date.today()
    ontem, d1, d2 = (hoje - timedelta(days=1)).isoformat(), (hoje + timedelta(days=1)).isoformat(), (hoje + timedelta(days=2)).isoformat()
    evs = [_evento(9001, ontem, "10:00", "CCJC", "Reunião Deliberativa Ordinária", "Constituição e Justiça", "Encerrada"),
           _evento(9002, d1, "14:30", "CFT", "Reunião Deliberativa Ordinária", "Finanças e Tributação"),
           _evento(9003, d2, "13:55", "PLEN", "Sessão Deliberativa Extraordinária", "Plenário"),
           _evento(9004, d1, "09:00", "CSPCCO", "Audiência Pública", "Segurança Pública")]
    pautas = {9001: [_item_pauta(1, 3001, "PL", 500, 2025, (101, "Ana Oposição", "PL", "SP"), "Aprovado o Parecer"),
                     _item_pauta(2, 3002, "PL", 501, 2025, (102, "Bruno Governo", "PT", "BA"), "Retirado de pauta"),
                     _item_pauta(3, 3003, "REQ", 10, 2025, None, "Aprovado")],
              9003: [_item_pauta(1, 3004, "PEC", 45, 2025, None)]}
    chamadas = []
    orig_pag, orig_get = collector.paginado, collector.get_json

    def fake_paginado(caminho, limite_paginas=200):
        chamadas.append(caminho)
        m = re.search(r"dataInicio=([\d-]+)&dataFim=([\d-]+)", caminho)
        return [e for e in evs if m[1] <= e["dataHoraInicio"][:10] <= m[2]]

    def fake_get(caminho):
        chamadas.append(caminho)
        eid = int(re.search(r"/eventos/(\d+)/pauta", caminho).group(1))
        return {"dados": pautas.get(eid, [])}
    collector.paginado, collector.get_json = fake_paginado, fake_get
    try:
        assert coleta_meu.coletar_agenda(hoje) == 4
        assert coleta_meu.coletar_agenda(hoje) == 4                      # sem duplicar
        assert not any("/eventos/9004/pauta" in c for c in chamadas)       # audiência pública: sem pauta
        assert any("/eventos/9001/pauta" in c for c in chamadas)
        n = len(chamadas)
        coleta_meu.coletar_historico_pautas(hoje, dias=30)
        assert coleta_meu._meta(coleta_meu.CHAVE_HISTORICO)
        n2 = len(chamadas)
        assert n2 > n
        coleta_meu.coletar_historico_pautas(hoje, dias=30)                 # só uma vez
        assert len(chamadas) == n2
    finally:
        collector.paginado, collector.get_json = orig_pag, orig_get

    # Ana (101) é da CCJC e da CSPCCO; a CFT dela já terminou; Plenário vale para todos
    ag = meu.agenda_deputado(101, hoje - timedelta(days=7), hoje + timedelta(days=10))
    itens = [e for d in ag for e in d["itens"]]
    assert {e["colegiado"] for e in itens} == {"CCJC", "PLEN", "CSPCCO"}
    ccjc = next(e for e in itens if e["colegiado"] == "CCJC")
    assert ccjc["citado"] and "PL 500/2025 (relator: Ana Oposição, PL-SP) — Aprovado o Parecer" in ccjc["descricao"]
    assert ccjc["hora"] == "10:00" and ccjc["url"].endswith("/evento-legislativo/9001")
    assert next(e for e in itens if e["colegiado"] == "PLEN")["titulo"] == "Plenário · Sessão Deliberativa Extraordinária"
    todos = [e for d in meu.agenda_deputado(101, hoje - timedelta(days=7), hoje + timedelta(days=10), so_dele=False)
             for e in d["itens"]]
    assert len(todos) == 4
    # relatorias montadas pela pauta: a da Ana aprovada entra no clipping
    rels = {r["identificacao"]: r for r in meu.lista_relatorias(101)}
    assert set(rels) == {"PL 500/2025"} and rels["PL 500/2025"]["colegiado"] == "CCJC"
    assert rels["PL 500/2025"]["situacao"] == "Aprovado o Parecer" and rels["PL 500/2025"]["data_situacao"] == ontem
    assert {r["identificacao"] for r in meu.lista_relatorias(102)} == {"PL 501/2025"}
    clip = meu.clipping(101, hoje - timedelta(days=30), hoje)
    assert ("relatoria", "PL 500/2025") in {(e["tipo"], e["identificacao"]) for d in clip["dias"] for e in d["itens"]}
    assert meu.clipping(102, hoje - timedelta(days=30), hoje)["relatoria"] == 0      # retirado de pauta
    # resultado mais antigo não sobrescreve o mais recente
    with engine.begin() as c:
        ev_velho = _evento(8001, (hoje - timedelta(days=60)).isoformat(), "10:00", "CCJC", "Reunião Deliberativa")
        coleta_meu.gravar_relatorias_pauta(c, ev_velho, [_item_pauta(1, 3001, "PL", 500, 2025,
                                                                   (101, "Ana Oposição", "PL", "SP"), "Adiado")])
    r = meu.lista_relatorias(101)[0]
    assert r["situacao"] == "Aprovado o Parecer" and r["data_designacao"] == (hoje - timedelta(days=60)).isoformat()

    # compromisso da equipe pela tela
    adm = _cliente()
    tok = _tok(adm, "/meu/101?secao=agenda")
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "novo", "data": d1, "hora": "18:30",
                                           "titulo": "Reunião com prefeitos de SP", "local": "Gabinete"})
    html = adm.get("/meu/101?secao=agenda").get_data(as_text=True)
    assert "Reunião com prefeitos de SP" in html and "deputado na pauta" in html and "Finanças e Tributação" not in html
    assert "Reunião com prefeitos de SP" not in adm.get("/meu/102?secao=agenda").get_data(as_text=True)
    aid = _q("SELECT id FROM agenda WHERE origem='equipe'")[0][0]
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "remover", "id": aid})
    assert not _q("SELECT id FROM agenda WHERE origem='equipe'")


def test_solicitacoes_com_anexos():
    import io
    adm = _cliente()
    tok = _tok(adm, "/meu/101?secao=solicitacoes")
    r = adm.post("/meu/101?secao=solicitacoes", content_type="multipart/form-data", data={
        "_csrf": tok, "acao": "nova", "titulo": "Nota técnica sobre a PEC 45/2025",
        "descricao": "Pedido da deputada na reunião de segunda.", "solicitante": "a deputada", "prazo": "2020-01-01",
        "arquivos": [(io.BytesIO(b"%PDF-1.4 conteudo"), "minuta.pdf"), (io.BytesIO(b"a;b\n1;2"), "dados.csv")]})
    assert r.status_code == 302
    html = adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    assert "Nota técnica sobre a PEC 45/2025" in html and "minuta.pdf" in html and "atrasada" in html
    aid = int(re.search(r"/meu/101/anexo/(\d+)", html).group(1))
    sid = int(re.search(r'name="id" value="(\d+)"', html.split("Nota técnica")[1]).group(1))
    baixado = adm.get(f"/meu/101/anexo/{aid}")
    assert baixado.status_code == 200 and baixado.data.startswith(b"%PDF")
    assert "attachment" in baixado.headers["Content-Disposition"]
    assert adm.get(f"/meu/102/anexo/{aid}").status_code == 404          # anexo de outro deputado
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "status", "id": sid, "status": "concluida"})
    assert "Concluída (1)" in adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    adm.post("/meu/101?secao=solicitacoes", content_type="multipart/form-data", data={
        "_csrf": tok, "acao": "nova", "titulo": "Arquivo perigoso", "arquivos": [(io.BytesIO(b"MZ"), "virus.exe")]})
    html = adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    assert "Tipo de arquivo não aceito" in html and "Arquivo perigoso" not in html
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "excluir", "id": sid})
    assert not _q("SELECT id FROM anexos WHERE solicitacao_id = :s", s=sid)


def _regimento_falso():
    linhas = []
    for n in range(1, 283):
        if n == 95:
            corpo = "Considera-se questão de ordem toda dúvida sobre a interpretação deste Regimento."
        elif n == 96:
            corpo = "Em qualquer fase da sessão, poderá ser usada a palavra para reclamação."
        elif n == 120:
            corpo = "As emendas de Plenário serão apresentadas durante a discussão em turno único, e o prazo de emendas na comissão é de cinco sessões."
        else:
            corpo = f"Disposição genérica número {n} sobre funcionamento das sessões."
        linhas.append(f"Art. {n}{'º' if n < 10 else '.'} {corpo}")
    return "\n".join(linhas)


def test_questao_de_ordem_por_ia():
    import io
    import questoes
    texto = _regimento_falso()
    arts = questoes.artigos(texto)
    assert len(arts) == 282 and arts[94][0] == 95
    sel = [n for n, _ in questoes.selecionar_trechos(texto, "prazo de emendas na comissão foi encerrado antes das cinco sessões")]
    assert {95, 96} <= set(sel) and 120 in sel and len(sel) < 60
    adm = _cliente()
    tok = _tok(adm, "/meu/101?secao=questoes")
    os.environ.pop("ANTHROPIC_API_KEY", None)
    assert "ANTHROPIC_API_KEY" in adm.get("/meu/101?secao=questoes").get_data(as_text=True)
    adm.post("/meu/101?secao=questoes", content_type="multipart/form-data", data={
        "_csrf": tok, "acao": "regimento", "arquivo": (io.BytesIO(texto.encode()), "RICD.txt")})
    assert "282 artigos" in adm.get("/meu/101?secao=questoes").get_data(as_text=True)

    enviado = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": "## Cabimento\nCabe, no Plenário.\n## Questão de ordem (texto para leitura)\n"
                                 "Senhor Presidente, com fundamento no art. 95 do Regimento Interno, formulo a seguinte questão de ordem: **<script>x</script>**\n"
                                 "## Pedido\n- Que seja reaberto o prazo de emendas."}]}

    class _Sessao:
        def post(self, url, json=None, timeout=None, headers=None):
            enviado.update(url=url, corpo=json, headers=headers)
            return _Resp()

    orig = questoes.processar_em_segundo_plano
    os.environ["ANTHROPIC_API_KEY"] = "sk-teste"
    questoes.processar_em_segundo_plano = lambda qid, nome=None: questoes.processar(qid, nome, sessao=_Sessao())
    try:
        adm.post("/meu/101?secao=questoes", data={"_csrf": tok, "acao": "gerar",
                 "situacao": "A comissão encerrou o prazo de emendas antes das cinco sessões e já vai votar o parecer hoje."})
    finally:
        questoes.processar_em_segundo_plano = orig
    assert enviado["url"] == questoes.API_URL and enviado["headers"]["x-api-key"] == "sk-teste"
    assert enviado["corpo"]["model"] == questoes.modelo() and "RICD" in enviado["corpo"]["system"]
    msg = enviado["corpo"]["messages"][0]["content"]
    assert "<regimento>" in msg and "Art. 95" in msg and "Art. 120" in msg and "Deputado(a): Ana Oposição" in msg
    assert "Câmara dos Deputados" in msg
    html = adm.get("/meu/101?secao=questoes").get_data(as_text=True)
    assert "<h3>Cabimento</h3>" in html and "Que seja reaberto o prazo de emendas" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html   # HTML da IA é escapado
    assert "Artigos do Regimento enviados à IA" in html

    class _Erro(_Resp):
        status_code = 401
        text = "invalid x-api-key"

        def json(self):
            return {"error": {"message": "invalid x-api-key"}}
    qid = questoes.criar_pedido(101, "admin", "Outra situação qualquer com detalhes suficientes para o teste.")
    questoes.processar(qid, sessao=type("S", (), {"post": lambda self, *a, **k: _Erro()})())
    assert questoes.historico(101)[0]["resposta"].startswith("[ERRO] A API da Anthropic respondeu 401")
    os.environ.pop("ANTHROPIC_API_KEY", None)


def test_radar_clipping_rankings_e_paginas():
    from datetime import date
    import coleta_meu
    import meu
    rss = """<?xml version="1.0"?><rss><channel>
      <item><title>Deputada apresenta PEC sobre segurança - Agência Câmara</title><link>https://exemplo/a</link>
        <pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate><source url="https://www.camara.leg.br">Agência Câmara</source></item>
      <item><title>Outra notícia</title><link>https://exemplo/b</link><pubDate>xx</pubDate></item>
    </channel></rss>"""
    itens = coleta_meu.parse_rss(rss)
    assert itens[0]["titulo"] == "Deputada apresenta PEC sobre segurança" and itens[0]["fonte"] == "Agência Câmara"
    assert itens[1]["publicado"] is None
    with engine.begin() as c:
        assert coleta_meu.gravar_noticias(c, 101, "Ana Oposição", itens) == 2
        assert coleta_meu.gravar_noticias(c, 101, "Ana Oposição", itens) == 0      # sem duplicar
    adm = _cliente()
    tok = _tok(adm, "/meu/101?secao=radar")
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "add", "termo": "segurança pública"})
    assert ("segurança pública", "segurança pública") in coleta_meu.consultas_radar(101)
    assert coleta_meu.consultas_radar(101)[0] == ("Ana Oposição", '"Ana Oposição" deputado')
    assert "news.google.com/rss/search" in coleta_meu.url_noticias("x")
    html = adm.get("/meu/101?secao=radar").get_data(as_text=True)
    assert "Deputada apresenta PEC sobre segurança" in html and "segurança pública" in html
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "del", "termo": "segurança pública"})
    assert len(coleta_meu.consultas_radar(101)) == 1

    clip = meu.clipping(101, date(2023, 1, 1), date.today())
    tipos = {(e["tipo"], e["identificacao"]) for d in clip["dias"] for e in d["itens"]}
    assert ("autoria", "PL 1087/2025") in tipos                            # virou norma
    assert ("autoria", "PDL 30/2025") not in tipos                         # só tramitando
    assert meu.eh_aprovacao("Aprovado o Parecer") and meu.eh_aprovacao("Transformado em Norma Jurídica")
    assert meu.eh_aprovacao("Remetida ao Senado Federal") and meu.eh_aprovacao("Aguardando Sanção")
    assert not meu.eh_aprovacao("Rejeitado o Parecer") and not meu.eh_aprovacao("Aguardando Designação de Relator")
    assert not meu.eh_aprovacao("Parecer não aprovado")

    rk = {r["slug"]: r for r in meu.rankings(101)}
    assert rk["projetos"]["posicao"]["pos"] == 1 and rk["projetos"]["sugestoes"]
    assert "Lidera" in rk["projetos"]["sugestoes"][0] and "deputados" in rk["projetos"]["sugestoes"][0]
    assert "1 aguardando designação de relator" in rk["projetos"]["sugestoes"][1]       # PDL 30 parado
    eco = rk["economia"]
    assert eco["posicao"]["pos"] == 2 and eco["sugestoes"][0].startswith("Gastando R$ ")
    assert "no período, passa Bruno Governo e sobe para 1º." in eco["sugestoes"][0]
    assert meu._passos_para(5, 10, 60) == 3 and meu._passos_para(9, 10, 60) == 0
    for secao in ("visao", "agenda", "notificacoes", "solicitacoes", "questoes", "radar", "rankings", "clipping"):
        r = adm.get(f"/meu/101?secao={secao}")
        assert r.status_code == 200, secao
        assert "Senado" not in r.get_data(as_text=True).replace("ao Senado", ""), secao
    html = adm.get("/meu/101?secao=visao").get_data(as_text=True)
    assert "Meu parlamentar" in html and "Ana Oposição" in html and "Aprovações em 30 dias" in html
    assert "PL 1087/2025" in adm.get(f"/meu/101?secao=clipping&inicio=2025-01-01").get_data(as_text=True)
    assert adm.get("/meu/101?secao=inexistente").status_code == 200
    html = adm.get("/orientacoes").get_data(as_text=True)
    assert "meu-link" in html and "style.css?v=" in html


def test_atualizar_agora_e_etapa_diaria():
    import coleta_meu
    import meu
    feitos = []
    orig = coleta_meu.executar
    coleta_meu.executar = lambda codigos=None, agenda_tambem=True: feitos.append(codigos)
    try:
        adm = _cliente()
        tok = _tok(adm, "/meu/101?secao=radar")
        adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "atualizar"})
        import time
        for _ in range(50):
            if feitos:
                break
            time.sleep(0.05)
        assert feitos == [[101]]
        assert "Atualização iniciada" in adm.get("/meu/101?secao=radar").get_data(as_text=True)
    finally:
        coleta_meu.executar = orig
    for _ in range(50):
        if meu._lock.acquire(blocking=False):
            meu._lock.release()
            break
        time.sleep(0.05)


if __name__ == "__main__":
    for nome, fn in list(globals().items()):
        if nome.startswith("test_"):
            fn()
            print("ok", nome)
