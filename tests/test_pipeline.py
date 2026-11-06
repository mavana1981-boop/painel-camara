import pytest
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
os.environ["AGENDA_AO_ACESSAR"] = "0"     # a tela da agenda não chama a API da Câmara nos testes
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
    # compromisso que já passou vai para o histórico
    ontem = (meu.agora_brt().date() - timedelta(days=1)).isoformat()
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "novo", "data": ontem, "hora": "09:00",
                                           "titulo": "Café com a bancada"})
    html = adm.get("/meu/101?secao=agenda").get_data(as_text=True)
    hist = html[html.index('class="ag-hist"'):]
    assert "Café com a bancada" in hist and "Reunião com prefeitos de SP" not in hist
    assert "Café com a bancada" not in \
        adm.get("/meu/101?secao=visao").get_data(as_text=True)
    for (aid,) in _q("SELECT id FROM agenda WHERE origem='equipe'"):
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


def _png(cor=(200, 30, 30)):
    import io as _io
    from PIL import Image
    b = _io.BytesIO()
    Image.new("RGB", (600, 400), cor).save(b, "PNG")
    return b.getvalue()


def test_solicitacao_com_datas_responsavel_e_aviso_na_tela_inicial():
    import base64
    import io
    import sqlite3
    from datetime import date, timedelta
    import fotos
    adm = _cliente()
    tok = _tok(adm, "/usuarios")
    for login, nome in (("joana.lima", "Joana Lima"), ("pedro", "Pedro Alves")):
        adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": login, "nome": nome, "senha": "senha-segura-1"})
    joana = _q("SELECT id FROM usuarios WHERE login='joana.lima'")[0][0]
    pedro = _q("SELECT id FROM usuarios WHERE login='pedro'")[0][0]

    # fotos do Minha Pauta: casa pelo login (joana.lima) e pelo nome exibido (Pedro Alves)
    caminho = os.path.join(_tmp, "minha_pauta_fotos.db")
    db = sqlite3.connect(caminho)
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, nome_display TEXT, foto TEXT)")
    uri = "data:image/png;base64," + base64.b64encode(_png()).decode()
    db.execute("INSERT INTO users (username, nome_display, foto) VALUES ('Joana.Lima', 'Joana', ?)", (uri,))
    db.execute("INSERT INTO users (username, nome_display, foto) VALUES ('pedroa', 'Pedro Alves', ?)", (uri,))
    db.execute("INSERT INTO users (username, nome_display, foto) VALUES ('outro', 'Sem Conta', 'data:image/png;base64,!!')")
    db.commit()
    from sqlalchemy import create_engine
    with create_engine(f"sqlite:///{caminho}").connect() as cf:
        assert fotos.sincronizar(cf) == 2
        assert fotos.sincronizar(cf) == 0                                  # nada mudou
    img = adm.get(f"/foto/{joana}")
    assert img.status_code == 200 and img.headers["Content-Type"] == "image/jpeg" and img.data[:2] == b"\xff\xd8"
    from PIL import Image
    assert max(Image.open(io.BytesIO(img.data)).size) <= 256                # reduzida
    assert adm.get("/foto/999999").status_code == 404
    assert _anonimo().get(f"/foto/{joana}").status_code == 302              # exige login
    html = adm.get("/usuarios").get_data(as_text=True)
    assert f'src="/foto/{joana}?v=' in html and f'src="/foto/{pedro}?v=' in html

    # nova solicitação com datas e responsável
    hoje = date.today()
    tok = _tok(adm, "/meu/101?secao=solicitacoes")
    form = adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    assert 'name="data_solicitacao"' in form and 'name="responsavel"' in form and "Joana Lima (joana.lima)" in form
    r = adm.post("/meu/101?secao=solicitacoes", data={
        "_csrf": tok, "acao": "nova", "titulo": "Minuta de PDL sobre a portaria", "solicitante": "o deputado",
        "data_solicitacao": (hoje - timedelta(days=1)).isoformat(), "prazo": (hoje + timedelta(days=3)).isoformat(),
        "responsavel": "joana.lima"})
    assert r.status_code == 302
    sid, ds, resp, por = _q("SELECT id, data_solicitacao, responsavel, atribuido_por FROM solicitacoes "
                            "WHERE titulo = 'Minuta de PDL sobre a portaria'")[0]
    assert ds == (hoje - timedelta(days=1)).isoformat() and resp == "joana.lima" and por == "admin"
    r = adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "nova", "titulo": "X", "responsavel": "ninguem"})
    assert "usuário ativo" in adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "nova", "titulo": "Datas trocadas",
                                                  "data_solicitacao": "2026-05-10", "prazo": "2026-05-01"})
    assert "não pode ser anterior" in adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    assert not _q("SELECT id FROM solicitacoes WHERE titulo IN ('X', 'Datas trocadas')")

    # Joana não tem vínculo com o deputado, mas recebeu a tarefa
    u = _cliente_usuario("joana.lima", "senha-segura-1")
    r = u.get("/")
    inicio = u.get(r.headers["Location"]).get_data(as_text=True)
    assert 'id="tarefas"' in inicio and "Nova tarefa atribuída a você" in inicio
    assert "Minuta de PDL sobre a portaria" in inicio and "faltam 3 dias" in inicio
    assert (hoje + timedelta(days=3)).strftime("%d/%m/%Y") in inicio and "atribuída por" in inicio
    assert f'src="/foto/{joana}?v=' in inicio                                # foto do usuário
    assert f"/meu/101?secao=solicitacoes&amp;abrir={sid}" in inicio
    assert 'class="sino"' in u.get("/rankings").get_data(as_text=True)       # aviso no topo em toda página
    assert 'id="tarefas"' not in u.get("/rankings").get_data(as_text=True)   # o quadro só na tela inicial
    assert 'id="tarefas"' not in adm.get("/orientacoes").get_data(as_text=True)  # admin não tem tarefa
    # acesso limitado às próprias tarefas
    assert u.get("/meu/101").headers["Location"].endswith("/meu/101?secao=solicitacoes")
    assert u.get("/meu/101?secao=agenda").status_code == 302
    assert u.get("/meu/102?secao=solicitacoes").status_code == 403
    html = u.get(f"/meu/101?secao=solicitacoes&abrir={sid}").get_data(as_text=True)
    assert "Minuta de PDL sobre a portaria" in html and "Nota técnica sobre" not in html and "Nova solicitação" not in html
    assert f'id="s{sid}" open' in html and "Suas tarefas" in html
    assert _q("SELECT visto_em FROM solicitacoes WHERE id = :s", s=sid)[0][0] is not None     # deixou de ser nova
    assert "Nova tarefa" not in u.get("/orientacoes").get_data(as_text=True)
    tj = _tok(u, "/meu/101?secao=solicitacoes")
    assert u.post("/meu/101?secao=solicitacoes", data={"_csrf": tj, "acao": "excluir", "id": sid}).status_code == 403
    assert u.post("/meu/101?secao=solicitacoes", data={"_csrf": tj, "acao": "nova", "titulo": "y"}).status_code == 403
    # tenta reatribuir: ignorado; conclui pela troca de status -> data da solução = hoje
    u.post("/meu/101?secao=solicitacoes", data={"_csrf": tj, "acao": "editar", "id": sid, "responsavel": "pedro",
                                                "data_solicitacao": ds, "prazo": (hoje + timedelta(days=3)).isoformat()})
    assert _q("SELECT responsavel FROM solicitacoes WHERE id = :s", s=sid)[0][0] == "joana.lima"
    r = u.post("/meu/101?secao=solicitacoes", data={"_csrf": tj, "acao": "status", "id": sid, "status": "concluida"})
    assert r.status_code == 302
    st, sol = _q("SELECT status, data_solucao FROM solicitacoes WHERE id = :s", s=sid)[0]
    assert st == "concluida" and sol == hoje.isoformat()
    assert 'id="tarefas"' not in u.get("/orientacoes").get_data(as_text=True)     # sem pendências
    html = adm.get(f"/meu/101?secao=solicitacoes&abrir={sid}").get_data(as_text=True)
    assert "solucionada " + hoje.strftime("%d/%m/%Y") in html and "Joana Lima" in html

    # admin reabre (limpa a solução) e reatribui a Pedro; informar a solução conclui
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "status", "id": sid, "status": "andamento"})
    assert _q("SELECT data_solucao FROM solicitacoes WHERE id = :s", s=sid)[0][0] is None
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "editar", "id": sid, "responsavel": "pedro",
                                                  "data_solicitacao": (hoje - timedelta(days=10)).isoformat(),
                                                  "prazo": (hoje - timedelta(days=2)).isoformat()})
    resp, visto = _q("SELECT responsavel, visto_em FROM solicitacoes WHERE id = :s", s=sid)[0]
    assert resp == "pedro" and visto is None
    p = _cliente_usuario("pedro", "senha-segura-1")
    inicio = p.get("/orientacoes").get_data(as_text=True)
    assert "atrasada há 2 dias" in inicio and f'src="/foto/{pedro}?v=' in inicio
    assert u.get("/meu/101?secao=solicitacoes").status_code == 403           # Joana perdeu a tarefa
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "editar", "id": sid, "responsavel": "pedro",
                                                  "data_solicitacao": ds, "prazo": (hoje + timedelta(days=1)).isoformat(),
                                                  "data_solucao": hoje.isoformat()})
    assert _q("SELECT status FROM solicitacoes WHERE id = :s", s=sid)[0][0] == "concluida"

    # foto enviada pelo admin prevalece e pode ser removida
    tu = _tok(adm, "/usuarios")
    r = adm.post("/usuarios", content_type="multipart/form-data", data={
        "_csrf": tu, "acao": "foto", "id": pedro, "foto": (io.BytesIO(_png((0, 90, 200))), "eu.png")})
    assert r.status_code == 302
    assert _q("SELECT origem FROM fotos_usuarios WHERE usuario_id = :u", u=pedro)[0][0] == "painel"
    with create_engine(f"sqlite:///{caminho}").connect() as cf:
        fotos.sincronizar(cf)
    assert _q("SELECT origem FROM fotos_usuarios WHERE usuario_id = :u", u=pedro)[0][0] == "painel"
    adm.post("/usuarios", content_type="multipart/form-data", data={
        "_csrf": tu, "acao": "foto", "id": pedro, "foto": (io.BytesIO(b"MZ"), "x.exe")})
    assert "JPG, PNG" in adm.get("/usuarios").get_data(as_text=True)
    adm.post("/usuarios", data={"_csrf": tu, "acao": "foto_remover", "id": pedro})
    assert not _q("SELECT 1 FROM fotos_usuarios WHERE usuario_id = :u", u=pedro)
    assert 'class="av av-ini' in adm.get("/usuarios").get_data(as_text=True)      # iniciais sem foto


def test_agenda_oculta_solenes_e_notificacoes_agrupadas():
    from datetime import date, datetime, timedelta
    import meu
    from db import agenda, notificacoes, processos
    from sqlalchemy import insert as ins, delete as dele
    hoje = date.today()
    with engine.begin() as c:
        c.execute(ins(agenda).values(origem="camara", chave="SOL1", data=(hoje + timedelta(days=1)).isoformat(), hora="10:00",
                                     titulo="Plenário · Sessão Não Deliberativa Solene", colegiado="PLEN",
                                     descricao="Homenagem aos 50 anos de X"))
        c.execute(ins(agenda).values(origem="camara", chave="DEL1", data=(hoje + timedelta(days=1)).isoformat(), hora="14:00",
                                     titulo="Plenário · Sessão Deliberativa Extraordinária", colegiado="PLEN"))
        c.execute(ins(agenda).values(origem="equipe", codigo_deputado=101, data=(hoje + timedelta(days=2)).isoformat(),
                                     titulo="Reunião sobre a sessão solene", criado_por="admin"))
        c.execute(dele(notificacoes))
        for pid, ident, sigla, tipo in ((9001, "REQ 10/2026", "REQ", "Requerimento de Urgência (Art. 155 do RICD)"),
                                        (9002, "REQ 11/2026", "REQ", "Requerimento de Urgência (Art. 155 do RICD)"),
                                        (9003, "RIC 5/2026", "RIC", "Requerimento de Informação"),
                                        (9004, "PL 77/2026", "PL", None)):
            c.execute(ins(processos).values(id=pid, identificacao=ident, sigla=sigla, tipo_documento=tipo,
                                            ementa=f"Ementa de {ident}", url=f"https://camara.leg.br/{pid}"))
        agora = datetime.now()
        for pid, ident, ant, nova, dias in ((9001, "REQ 10/2026", "Aguardando Despacho", "Aguardando Deliberação", 1),
                                            (9001, "REQ 10/2026", "Aguardando Deliberação", "Aprovado", 0),
                                            (9002, "REQ 11/2026", "Aguardando Despacho", "Arquivado", 40),
                                            (9003, "RIC 5/2026", "Aguardando Despacho", "Encaminhado ao Ministério", 3),
                                            (9004, "PL 77/2026", None, "Aguardando Designação de Relator", 2)):
            c.execute(ins(notificacoes).values(codigo_deputado=101, processo_id=pid, identificacao=ident,
                                               tipo="situacao" if ant else "nova", situacao_anterior=ant, situacao_nova=nova,
                                               data_situacao=(hoje - timedelta(days=dias)).isoformat(),
                                               detectado_em=agora - timedelta(days=dias)))
    adm = _cliente()
    html = adm.get("/meu/101?secao=agenda").get_data(as_text=True)
    assert "Sessão Não Deliberativa Solene" in html and "Ocultar sessões solenes" in html and "seu padrão" in html
    html = adm.get("/meu/101?secao=agenda&solenes=ocultar").get_data(as_text=True)
    assert "Sessão Não Deliberativa Solene" not in html and "Sessão Deliberativa Extraordinária" in html
    assert "Reunião sobre a sessão solene" in html                      # compromisso da equipe fica
    assert "1 sessão solene oculta" in html and "Salvar como padrão" in html
    tok = _tok(adm, "/meu/101?secao=agenda")
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "pref_solenes", "valor": "ocultar"})
    html = adm.get("/meu/101?secao=agenda").get_data(as_text=True)          # padrão salvo vale sem parâmetro
    assert "Padrão salvo" in html and "Sessão Não Deliberativa Solene" not in html and "seu padrão" in html
    assert "Sessão Não Deliberativa Solene" not in adm.get("/meu/101?secao=agenda&todos=1").get_data(as_text=True)
    assert "Sessão Não Deliberativa Solene" in adm.get("/meu/101?secao=agenda&solenes=mostrar").get_data(as_text=True)
    assert meu.eh_solene({"titulo": "Sessão Solene"}) and not meu.eh_solene({"titulo": "Solenidade de posse"})
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "pref_solenes", "valor": "mostrar"})
    assert "Sessão Não Deliberativa Solene" in adm.get("/meu/101?secao=agenda").get_data(as_text=True)

    g = meu.notificacoes_agrupadas(101)
    nomes = [t["nome"] for t in g["tipos"]]
    urg = next(t for t in g["tipos"] if t["nome"].startswith("Requerimento de Urgência"))
    assert urg["n_props"] == 2 and urg["mudancas"] == 3 and urg["sigla"] == "REQ"
    req10 = urg["props"][0]
    assert req10["identificacao"] == "REQ 10/2026" and req10["atual"] == "Aprovado" and len(req10["eventos"]) == 2
    assert "Requerimento de Informação" in nomes and "Projeto de Lei" in nomes      # sem descrição: nome pela sigla
    assert meu.notificacoes_agrupadas(101, 7)["total"] == 4                          # sem a de 40 dias
    assert [d["data"] for d in g["datas"]][0] == hoje.isoformat()
    html = adm.get("/meu/101?secao=notificacoes").get_data(as_text=True)
    assert "Por tipo e proposição" in html and 'class="nt-tipo"' in html
    assert "Requerimento de Urgência (Art. 155 do RICD)" in html and "2 proposições · 3 mudanças" in html
    assert "REQ 10/2026" in html and "2 mudanças" in html
    html = adm.get("/meu/101?secao=notificacoes&modo=data&periodo=7").get_data(as_text=True)
    assert "Salvar como padrão" in html and "REQ 11/2026" not in html and "RIC 5/2026" in html
    tok = _tok(adm, "/meu/101?secao=notificacoes")
    adm.post("/meu/101?secao=notificacoes", data={"_csrf": tok, "acao": "pref_notif", "valor": "data"})
    html = adm.get("/meu/101?secao=notificacoes").get_data(as_text=True)
    assert 'class="sel" href="/meu/101?secao=notificacoes&amp;modo=data"' in html


def test_solicitacao_concluida_vai_para_clipping_e_relatorio():
    import io
    from datetime import date, timedelta
    from pypdf import PdfReader
    import meu
    adm = _cliente()
    hoje = date.today()
    tok = _tok(adm, "/meu/101?secao=solicitacoes")
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "nova", "titulo": "PDL para sustar a portaria 2272",
                                                  "solicitante": "o líder", "data_solicitacao": (hoje - timedelta(days=2)).isoformat()})
    sid = _q("SELECT id FROM solicitacoes WHERE titulo = 'PDL para sustar a portaria 2272'")[0][0]
    html = adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    assert f'id="conc-{sid}" hidden' in html and "O que foi feito" in html
    r = adm.post("/meu/101?secao=solicitacoes", content_type="multipart/form-data", data={
        "_csrf": tok, "acao": "concluir", "id": sid, "no_clipping": "1",
        "resumo": "Minuta do PDL elaborada e protocolada; nota enviada à imprensa.",
        "arquivos": [(io.BytesIO(b"PK conteudo"), "04_PDL_Susta_Portaria_2272.docx")]})
    assert r.status_code == 302
    st, sol, resumo, clip = _q("SELECT status, data_solucao, resumo_realizado, no_clipping FROM solicitacoes WHERE id = :s", s=sid)[0]
    assert st == "concluida" and sol == hoje.isoformat() and resumo.startswith("Minuta do PDL") and clip
    html = adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    assert "registrada no clipping" in html and "No clipping do parlamentar" in html

    html = adm.get("/meu/101?secao=clipping").get_data(as_text=True)
    assert "Solicitação atendida" in html and "PDL para sustar a portaria 2272" in html
    assert "Minuta do PDL elaborada e protocolada" in html and "04_PDL_Susta_Portaria_2272.docx" in html
    assert "Relatório de realizações" in html and re.search(r"\d+ solicitação\(ões\) atendida\(s\)", html)
    assert meu.clipping(101, hoje - timedelta(days=1), hoje)["realizacao"] >= 1

    pdf = adm.get(f"/meu/101/realizacoes.pdf?ano={hoje.year}")
    assert pdf.status_code == 200 and pdf.mimetype == "application/pdf"
    texto = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.data)).pages)
    assert "PDL para sustar a portaria 2272" in texto and "Minuta do PDL elaborada" in texto
    assert "04_PDL_Susta_Portaria_2272.docx" in texto and f"Ano de {hoje.year}" in texto
    pdf0 = adm.get(f"/meu/101/realizacoes.pdf?ano={hoje.year}&aprovacoes=0")
    texto0 = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf0.data)).pages)
    assert "PDL para sustar" in texto0 and "Proposição aprovada".upper() not in texto0.upper()
    vazio = adm.get("/meu/101/realizacoes.pdf?ano=2023")
    assert "Nenhuma realização" in " ".join(p.extract_text() for p in PdfReader(io.BytesIO(vazio.data)).pages)

    # tirar do clipping pela edição
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "editar", "id": sid,
                                                  "data_solicitacao": (hoje - timedelta(days=2)).isoformat(),
                                                  "data_solucao": hoje.isoformat(), "resumo_realizado": "Novo resumo"})
    assert _q("SELECT resumo_realizado, no_clipping FROM solicitacoes WHERE id = :s", s=sid)[0] == ("Novo resumo", False)
    assert "PDL para sustar a portaria 2272" not in adm.get("/meu/101?secao=clipping").get_data(as_text=True)
    assert _anonimo().get(f"/meu/101/realizacoes.pdf").status_code == 302


def test_agenda_avisa_deputado_na_pauta_e_termos_sensiveis():
    import io
    from datetime import date, timedelta
    from pypdf import PdfReader
    import alertas
    import coleta_meu
    from db import agenda, autorias, processos
    from sqlalchemy import insert as ins, delete as dele
    dia = (date.today() + timedelta(days=3)).isoformat()
    with engine.begin() as c:
        c.execute(ins(processos).values(id=7700001, identificacao="PL 900/2026", sigla="PL"))
        c.execute(ins(autorias).values(processo_id=7700001, codigo_deputado=101, principal=True))
        c.execute(ins(processos).values(id=7700006, identificacao="PL 901/2026", sigla="PL"))
        c.execute(ins(autorias).values(processo_id=7700006, codigo_deputado=101, principal=False))
    ev = coleta_meu.parse_evento(_evento(880001, dia, "10:00", "PLEN", "Sessão Deliberativa Extraordinária"), [
        _item_pauta(1, 7700002, "PL", 321, 2026, relator=(101, "Ana Oposição", "PL", "SP"),
                    ementa="Altera o Código de Trânsito."),
        _item_pauta(2, 7700001, "PL", 900, 2026, ementa="Institui o programa de hortas comunitárias."),
        _item_pauta(3, 7700003, "PL", 55, 2026, ementa="Dispõe sobre a igualdade de GÊNEROS nas escolas e combate à misoginia."),
        _item_pauta(5, 7700006, "PL", 901, 2026, ementa="Cria o selo empresa amiga da escola."),
    ])
    req = _item_pauta(4, 7700004, "REQ", 12, 2026, ementa="Requer urgência para o PL 77/2026.")
    req["proposicaoRelacionada_"] = {"id": 7700005, "siglaTipo": "PL", "numero": 77, "ano": 2026,
                                     "ementa": "Descriminaliza o porte de maconha para uso pessoal."}
    ev2 = coleta_meu.parse_evento(_evento(880002, dia, "14:00", "CCJC", "Reunião Deliberativa"), [req])
    with engine.begin() as c:
        c.execute(dele(agenda).where(agenda.c.chave.in_(["E880001", "E880002"])))
        for e in (ev, ev2):
            c.execute(ins(agenda).values(**e, codigo_deputado=None))
    assert len(json.loads(ev["itens_pauta"])) == 4

    adm = _cliente()
    html = adm.get("/meu/101?secao=agenda&todos=1&solenes=mostrar").get_data(as_text=True)
    assert "Deputado é relator: PL 321/2026" in html and "Deputado é autor: PL 900/2026" in html
    assert "Deputado é coautor: PL 901/2026" in html
    assert 'class="ag-tag autor"' in html and 'class="ag-tag coautor"' in html
    assert "Institui o programa de hortas comunitárias." in html
    assert "⚠ gênero, misoginia</b> — PL 55/2026" in html
    assert "⚠ maconha</b> — REQ 12/2026 (sobre o PL 77/2026)" in html and "Descriminaliza o porte" in html
    assert "Deputado na pauta em <b>" in html and "Temas sensíveis em <b>" in html
    import meu as _meu
    from app import app as _app
    with _app.test_request_context("/"):
        from flask import g as _g
        _g.usuario = {"id": 1, "login": "admin", "admin": True}
        evs = {e["chave"]: e for d in _meu.agenda_deputado(101, date.today(), date.today() + timedelta(days=5), False)
               for e in d["itens"]}
    assert evs["E880002"]["sem_proposicao"] and not evs["E880001"]["sem_proposicao"]   # CCJC: nada dele
    assert "SEM PROPOSIÇÃO DO DEPUTADO" in html
    # lista de termos editável
    tok = _tok(adm, "/meu/101?secao=agenda")
    assert "Termos sensíveis" in html and "ideologia de gênero" in html
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "termo_add", "termo": "Código de Trânsito"})
    assert "Código de Trânsito" in alertas.termos()
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "termo_add", "termo": "codigo de transito"})
    assert "já está na lista" in adm.get("/meu/101?secao=agenda").get_data(as_text=True)
    html = adm.get("/meu/101?secao=agenda&todos=1").get_data(as_text=True)
    assert "⚠ Código de Trânsito</b> — PL 321/2026" in html
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "termo_del", "termo": "Código de Trânsito"})
    adm.post("/meu/101?secao=agenda", data={"_csrf": tok, "acao": "termo_del", "termo": "maconha"})
    assert "maconha" not in alertas.termos()
    regras = alertas.compilar(["misogin", "drogas"])
    assert alertas.achar("Combate ao discurso misógino", regras) == ["misogin"]
    assert alertas.achar("Fiscalização de drogarias", alertas.compilar(["drogas"])) == []
    r8 = alertas.compilar(["8 de janeiro"])
    assert alertas.achar("Anistia aos condenados pelos atos de 8 de janeiro de 2023", r8) == ["8 de janeiro"]
    assert alertas.achar("Homenagem ao Rio de Janeiro", r8) == []
    assert alertas.achar("Sessão de 18 de janeiro", r8) == [] and alertas.achar("Prazo até janeiro", r8) == []
    # PDF traz os avisos
    pdf = adm.get(f"/meu/101/agenda.pdf?periodo=data&de={dia}&todos=1")
    texto = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.data)).pages)
    assert "Deputado é relator" in texto and "PL 321/2026" in texto and "Tema sensível" in texto
    assert "Deputado é coautor: PL 901/2026" in texto and "COAUTOR" in texto


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



RSS_PORTAL = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>G1</title>
<item><title>Ana Oposição cobra explicações do ministério sobre obras paradas</title><link>https://g1.exemplo/1</link>
  <description>&lt;p&gt;A deputada quer saber por que 40 obras estão paralisadas.&lt;/p&gt;</description><pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Governo anuncia novo plano</title><link>https://g1.exemplo/2</link>
  <description>Plano prevê ações de segurança pública nas fronteiras.</description><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Campeonato de futebol começa</title><link>https://g1.exemplo/3</link><description>Esporte.</description></item>
<item><title>Ana Oposiçãozinha é personagem de novela</title><link>https://g1.exemplo/4</link><description>Ficção.</description></item>
</channel></rss>"""
ATOM_PORTAL = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Poder360</title>
<entry><title>Oposição: Ana Oposição apresenta PDL contra decreto</title><link href="https://poder.exemplo/9"/>
  <summary>O projeto susta o decreto.</summary><updated>2026-10-25T09:00:00Z</updated></entry></feed>"""
RSS_GOOGLE = """<?xml version="1.0"?><rss><channel>
<item><title>Deputada Ana Oposição visita hospital em SP - Folha de S.Paulo</title><link>https://news.google/a</link>
  <description>&lt;a href="x"&gt;Deputada Ana Oposição visita hospital em SP&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font&gt;Folha de S.Paulo&lt;/font&gt;</description>
  <pubDate>Tue, 27 Oct 2026 08:00:00 GMT</pubDate><source url="https://folha.uol.com.br">Folha de S.Paulo</source></item>
<item><title>Ana Oposição cobra explicações do ministério sobre obras paradas - G1</title><link>https://g1.exemplo/1</link>
  <pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate><source url="https://g1.globo.com">G1</source></item>
</channel></rss>"""


def _buscar_fake(url):
    if url == "https://g1.globo.com/rss/g1/politica/":
        return RSS_PORTAL.encode()
    if url == "https://www.poder360.com.br/feed/":
        return ATOM_PORTAL.encode()
    if "news.google.com" in url:
        return RSS_GOOGLE.encode() if "Ana" in url else b"<rss><channel></channel></rss>"
    return None


class _IAFake:
    """Responde como a IA do Monitor: relevante para cobranças, irrelevante para o resto."""
    def __init__(self):
        self.pedidos = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.pedidos.append(json)
        msg = json["messages"][0]["content"]
        if "obras paradas" in msg:
            txt = ('```json\n{"relevante": true, "categoria": "Infraestrutura e serviços públicos", "tipo_acao": '
                   '"Requerimento de informações", "acao_sugerida": "Apresentar RIC ao Ministério sobre as 40 obras.", '
                   '"justificativa": "Omissão do Executivo."}\n```')
        elif "hospital" in msg:
            txt = "resposta quebrada sem json"
        else:
            txt = '{"relevante": false, "categoria": null, "tipo_acao": null, "acao_sugerida": null, "justificativa": "Sem relação com fiscalização."}'

        class R:
            status_code = 200

            def json(self_):
                return {"content": [{"type": "text", "text": txt}]}
        return R()


def test_radar_no_modelo_do_monitor():
    from datetime import date, datetime, timedelta
    import app as painel
    import radar
    with engine.begin() as c:
        c.execute(text("DELETE FROM noticias"))
        c.execute(text("DELETE FROM temas_radar"))
    radar.garantir_fontes()
    fontes = radar.lista_fontes()
    assert len(fontes) == len(radar.FONTES_PADRAO) == 18
    assert {f["nome"] for f in fontes} >= {"G1 Política", "Poder360", "Folha Poder"}
    veja = next(f for f in fontes if f["nome"] == "Veja Brasil")
    adm = _cliente()
    tok = _tok(adm, "/meu/101?secao=radar")
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "fonte_del", "fonte": veja["id"]})
    radar.garantir_fontes()                                    # exclusão do admin é respeitada
    assert len(radar.lista_fontes()) == 17
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "add", "termo": "segurança pública"})
    assert radar.casa_termo("Ana Oposiçãozinha na novela", radar._termos_norm(101)) is None      # palavra inteira
    assert radar.casa_termo("A deputada ANA OPOSICAO falou", radar._termos_norm(101)) == "Ana Oposição"

    ia = _IAFake()
    os.environ["ANTHROPIC_API_KEY"] = "sk-teste"
    try:
        res = radar.ciclo([101], buscar=_buscar_fake, sessao=ia)
        assert res == {"portais": 3, "google": 1, "analisadas": 4}
        assert radar.ciclo([101], buscar=_buscar_fake, sessao=ia)["portais"] == 0      # já vistas
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    itens = {n["link"]: n for n in radar.lista(101)}
    assert set(itens) == {"https://g1.exemplo/1", "https://g1.exemplo/2", "https://poder.exemplo/9", "https://news.google/a"}
    g1 = itens["https://g1.exemplo/1"]
    assert g1["origem"] == "portal" and g1["fonte"] == "G1 Política" and g1["termo"] == "Ana Oposição"
    assert g1["resumo"] == "A deputada quer saber por que 40 obras estão paralisadas."
    assert g1["relevante"] is True and g1["tipo_acao"] == "Requerimento de informações" and g1["status"] == "Pendente"
    assert itens["https://g1.exemplo/2"]["termo"] == "segurança pública" and itens["https://g1.exemplo/2"]["relevante"] is False
    assert itens["https://poder.exemplo/9"]["fonte"] == "Poder360"
    gg = itens["https://news.google/a"]
    assert gg["origem"] == "google" and gg["fonte"] == "Folha de S.Paulo" and gg["titulo"] == "Deputada Ana Oposição visita hospital em SP"
    assert gg["relevante"] is None and "não pôde ser lida" in gg["justificativa"]
    pedido = ia.pedidos[0]
    assert pedido["model"] == radar.modelo() and "Liderança da Oposição" in pedido["system"]
    assert "deputado Ana Oposição (PL-SP)" in pedido["messages"][0]["content"]
    assert [n["link"] for n in radar.lista(101, filtro="relevantes")] == ["https://g1.exemplo/1"]
    assert len(radar.lista(101, filtro="portal")) == 3 and len(radar.lista(101, termo="segurança pública")) == 1

    # tela: filtro, análise e acompanhamento pela equipe
    html = adm.get("/meu/101?secao=radar&filtro=relevantes").get_data(as_text=True)
    assert "Apresentar RIC ao Ministério sobre as 40 obras." in html and "Omissão do Executivo." in html
    assert "Campeonato" not in html and "Portais acompanhados" in html and "G1 Política" in html
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": g1["id"], "status": "Ação efetuada",
                                          "assessor": "Marcelo", "acao_efetuada": "RIC 1234/2026 protocolado"})
    html = adm.get("/meu/101?secao=radar&filtro=Ação efetuada").get_data(as_text=True)
    assert "RIC 1234/2026 protocolado" in html and "Marcelo" in html
    assert radar.lista(102) == []
    adm.post("/meu/102?secao=radar", data={"_csrf": tok, "acao": "item", "id": g1["id"], "status": "Pendente"})
    assert radar.lista(101, filtro="Ação efetuada")[0]["id"] == g1["id"]          # item de outro deputado: intocado
    html = adm.get("/meu/101?secao=visao").get_data(as_text=True)
    assert "Requerimento de informações" in html                               # selo da IA na visão geral

    # portais: admin acrescenta; quem não é admin não mexe
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "fonte_add", "nome": "Agência Câmara",
                                          "url": "https://www.camara.leg.br/noticias/rss"})
    assert "Agência Câmara" in {f["nome"] for f in radar.lista_fontes()}
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "fonte_add", "nome": "Ruim", "url": "ftp://x"})
    assert "endereço do feed RSS" in adm.get("/meu/101?secao=radar").get_data(as_text=True)
    tok_u = _tok(adm, "/usuarios")
    adm.post("/usuarios", data={"_csrf": tok_u, "acao": "criar", "login": "radar.leitor", "nome": "Leitor", "senha": "senha-segura-2"})
    uid = _q("SELECT id FROM usuarios WHERE login='radar.leitor'")[0][0]
    adm.post("/usuarios", data={"_csrf": tok_u, "acao": "vincular", "id": uid, "deputados": ["101"]})
    u = _cliente_usuario("radar.leitor", "senha-segura-2")
    tu = _tok(u, "/meu/101?secao=radar")
    assert u.post("/meu/101?secao=radar", data={"_csrf": tu, "acao": "fonte_del", "fonte": veja["id"]}).status_code == 403
    assert "Adicionar portal" not in u.get("/meu/101?secao=radar").get_data(as_text=True)

    # limpeza: notícia antiga sem acompanhamento sai; a trabalhada fica
    with engine.begin() as c:
        c.execute(text("UPDATE noticias SET coletado_em = :d"), {"d": datetime.now() - timedelta(days=radar.DIAS_NOTICIAS + 5)})
        radar.gravar_noticias(c, 101, "x", [])
    assert [n["link"] for n in radar.lista(101)] == ["https://g1.exemplo/1"]

    # agendamento a cada 2 horas
    radar.ciclo(None, buscar=lambda u: None)
    assert not radar.precisa_ciclo() and radar.precisa_ciclo(datetime.now() + timedelta(hours=radar.HORAS_CICLO, minutes=1))
    os.environ["AUTO_RADAR"] = "0"
    try:
        assert painel.radar_se_preciso() is False
    finally:
        os.environ.pop("AUTO_RADAR")
    assert radar.extrair_json('lixo {"relevante": false} lixo') == {"relevante": False}


def test_banco_antigo_ganha_colunas_novas():
    """Quem já tinha a tabela de notícias antes do radar novo recebe as colunas no boot."""
    from sqlalchemy import inspect
    with engine.begin() as c:
        c.execute(text("ALTER TABLE noticias DROP COLUMN acao_efetuada"))
        c.execute(text("ALTER TABLE noticias DROP COLUMN relevante"))
    assert "relevante" not in {x["name"] for x in inspect(engine).get_columns("noticias")}
    init_db()
    cols = {x["name"] for x in inspect(engine).get_columns("noticias")}
    assert {"relevante", "acao_efetuada", "origem", "resumo"} <= cols



def _minha_pauta_falsa(caminho):
    """Banco no formato do Minha Pauta (plenario-assessoria), com duas sessões."""
    import sqlite3
    db = sqlite3.connect(caminho)
    db.executescript("""
    CREATE TABLE notas (item_key TEXT PRIMARY KEY, evento_id INTEGER, ordem TEXT, resumo_materia TEXT, orientacao TEXT,
                        resumo_parecer TEXT, saved_by TEXT, saved_at TEXT, responsavel_username TEXT);
    CREATE TABLE pauta_cache_db (evento_id INTEGER PRIMARY KEY, json_pauta TEXT, last_updated TEXT, last_saved_by TEXT,
                                 fetched_at TEXT, reordered_at TEXT, reordered_by TEXT);
    CREATE TABLE extra_pauta (id INTEGER PRIMARY KEY AUTOINCREMENT, evento_id INTEGER NOT NULL, id_proposicao TEXT NOT NULL,
                              projeto TEXT, ementa TEXT, autor TEXT, relator TEXT, created_by TEXT, created_at TEXT);
    CREATE TABLE orientacoes_grupo (id INTEGER PRIMARY KEY AUTOINCREMENT, evento_id INTEGER, id_principal TEXT, grupo TEXT,
                                    orientacao TEXT, comentario TEXT, saved_by TEXT, saved_at TEXT);
    """)
    itens_novos = [
        {"ordem": "1", "id_principal": "2480001", "projeto": "PL 1087/2025", "projeto_original": "PL 1087/2025",
         "ementa": "Altera a Lei X.", "autor": "Ana Oposição", "relator": "Dep. Fulano", "situacao": "Aguardando",
         "secao": "Matéria sobre a mesa", "resumo_materia": "", "orientacao": "", "resumo_parecer": ""},
        {"ordem": "2", "id_principal": "2470045", "projeto": "REQ 10/2025 ao PEC 45/2025", "projeto_original": "REQ 10/2025 ao PEC 45/2025",
         "ementa": "Requer urgência.", "autor": "Bruno Governo", "relator": "Não atribuído", "situacao": "N/D", "secao": "N/D",
         "resumo_materia": "<p>cópia antiga no cache</p>", "orientacao": "SIM"},
        {"ordem": "3", "id_principal": "2440030", "projeto": "PDL 30/2025", "ementa": "Susta decreto.",
         "autor": "Ana Oposição", "relator": "Não atribuído", "situacao": "N/D", "secao": "N/D"},
    ]
    db.execute("INSERT INTO pauta_cache_db VALUES (?,?,?,?,?,?,?)", (9100, json.dumps(itens_novos), "2026-10-27 09:10:00",
                                                                    "Marcelo", "2026-10-27 08:00:00", None, None))
    db.execute("INSERT INTO pauta_cache_db VALUES (?,?,?,?,?,?,?)", (9050, json.dumps(itens_novos[:1]), "2026-10-20 10:00:00",
                                                                    "Luisa", None, None, None))
    db.execute("INSERT INTO notas VALUES (?,?,?,?,?,?,?,?,?)", ("PROP_2480001", 9050, "1",
        '<p><strong>Nota técnica.</strong> Vota SIM.</p><script>alert(1)</script><ul><li>ponto 1</li></ul>'
        '<a href="javascript:x()">ruim</a><a href="https://www.camara.leg.br/x">texto</a><img src=x onerror=alert(2)>',
        "NÃO", "Parecer favorável com substitutivo.", "Luisa", "2026-10-20 18:00:00", "luisa"))
    db.execute("INSERT INTO notas VALUES (?,?,?,?,?,?,?,?,?)", ("PROP_2470045", 9100, "2", "<p>nota nova</p>", "OBSTRUÇÃO",
                                                                "", "Marcelo", "2026-10-27 09:00:00", None))
    db.execute("INSERT INTO extra_pauta (evento_id, id_proposicao, projeto, ementa, autor, relator, created_by, created_at) "
               "VALUES (9100, '2490456', 'REQ 456/2025', 'Requer retirada.', 'Ana Oposição', '', 'Marcelo', '2026-10-27 09:05')")
    for g, o in (("PL", "NÃO"), ("NOVO", "NÃO"), ("oposicao", "OBSTRUÇÃO"), ("minoria", "")):
        db.execute("INSERT INTO orientacoes_grupo (evento_id, id_principal, grupo, orientacao, comentario) VALUES (9100,'2480001',?,?,?)",
                   (g, o, "acordo" if g == "PL" else ""))
    db.commit()
    db.close()


def test_plenario_com_pauta_e_notas_do_minha_pauta():
    from datetime import datetime, timedelta
    import app as painel
    import plenario
    adm = _cliente()
    os.environ.pop("MINHA_PAUTA_DATABASE_URL", None)
    html = adm.get("/plenario").get_data(as_text=True)
    assert "MINHA_PAUTA_DATABASE_URL" in html and 'class="ativa">Plenário' in html.replace("\n", "")
    assert plenario.sincronizar() is None and not plenario.precisa_sincronizar()
    caminho = os.path.join(_tmp, "minha_pauta.db")
    _minha_pauta_falsa(caminho)
    os.environ["MINHA_PAUTA_DATABASE_URL"] = f"sqlite:///{caminho}"
    orig = collector.get_json
    collector.get_json = lambda c: {"dados": {"dataHoraInicio": "2026-10-28T13:55", "descricaoTipo": "Sessão Deliberativa",
                                              "situacao": "Convocada", "localCamara": {"nome": "Plenário Principal - CD"}}}
    try:
        assert plenario.precisa_sincronizar()
        assert plenario.sincronizar() == 2
        assert not plenario.precisa_sincronizar()
        assert plenario.precisa_sincronizar(datetime.now() + timedelta(minutes=plenario.MINUTOS + 1))
        p = plenario.pauta()
        assert p["evento_id"] == 9100 and p["data"] == "2026-10-28" and p["hora"] == "13:55"
        assert p["total"] == 4 and p["com_nota"] == 2 and p["atualizado_por"] == "Marcelo"
        itens = {i["id_principal"]: i for i in p["itens"]}
        pl = itens["2480001"]
        assert pl["nota"]["orientacao"] == "NÃO" and pl["nota"]["saved_by"] == "Luisa"      # nota de sessão anterior vale
        assert pl["orientacoes_grupo"]["oposicao"]["orientacao"] == "OBSTRUÇÃO" and pl["url"].endswith("/2480001")
        assert itens["2470045"]["nota"]["resumo_materia"] == "<p>nota nova</p>"          # nota prevalece sobre o cache
        assert itens["2470045"]["projeto_original"] == "REQ 10/2025"
        assert not itens["2440030"]["tem_nota"]
        assert itens["2490456"]["eh_extra_pauta"] and itens["2490456"]["incluido_por"] == "Marcelo"
        assert [x["evento_id"] for x in plenario.pautas_recentes()] == [9100, 9050]
        assert [i["id_principal"] for i in plenario.pauta(filtro="com_nota")["itens"]] == ["2480001", "2470045"]

        html = adm.get("/plenario").get_data(as_text=True)
        assert "PL 1087/2025" in html and "Nota técnica." in html and "Parecer favorável com substitutivo." in html
        assert "<li>ponto 1</li>" in html and "<script>alert" not in html and "alert(" not in html and "javascript:" not in html
        assert 'href="https://www.camara.leg.br/x"' in html and "onerror" not in html
        assert "Oposição:" in html and "OBSTRUÇÃO" in html and "extrapauta" in html and "Sem nota da equipe" in html
        assert "28/10/2026" in html and "Salva no Minha Pauta por Marcelo" in html
        html = adm.get("/plenario?evento=9050").get_data(as_text=True)
        assert "Altera a Lei X." in html and "Susta decreto." not in html
        assert "Susta decreto." in adm.get("/plenario?filtro=sem_nota").get_data(as_text=True)
        tok = _tok(adm, "/plenario")
        r = adm.post("/plenario/atualizar", data={"_csrf": tok})
        assert r.status_code == 302 and "Pauta atualizada" in adm.get("/plenario").get_data(as_text=True)
        assert painel.plenario_se_preciso() is False                                   # acabou de copiar
        assert _anonimo().get("/plenario").status_code == 302                           # exige login
        html = adm.get("/orientacoes?opostas=0").get_data(as_text=True)
        assert 'href="/plenario"' in html                                               # aba sem os filtros de votação
        assert plenario.html_seguro("linha 1\nlinha <2>") == "linha 1<br>linha &lt;2&gt;"
        erro = Exception('connection to server at "10.0.0.1", port 5432 failed: FATAL:  password authentication failed for user "postgres"')
        assert "DATABASE_PUBLIC_URL" in plenario.explicar_erro(erro)
    finally:
        collector.get_json = orig
        os.environ.pop("MINHA_PAUTA_DATABASE_URL", None)


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




def test_agenda_historico():
    """Compromissos que já passaram saem dos próximos e vão para o histórico (mais recente primeiro)."""
    from datetime import datetime
    import meu
    agora = datetime(2026, 11, 3, 15, 0)
    dias = [
        {"data": "2026-11-01", "itens": [{"id": 1, "data": "2026-11-01", "hora": "10:00", "origem": "equipe"}]},
        {"data": "2026-11-03", "itens": [
            {"id": 2, "data": "2026-11-03", "hora": "09:00", "origem": "equipe"},           # passou
            {"id": 3, "data": "2026-11-03", "hora": "13:00", "origem": "camara"},           # tolerância de 3 h
            {"id": 4, "data": "2026-11-03", "hora": "10:00", "origem": "camara"},           # passou (+3 h)
            {"id": 5, "data": "2026-11-03", "hora": "14:00", "origem": "camara", "situacao": "Encerrada"},
            {"id": 6, "data": "2026-11-03", "hora": "09:00", "origem": "camara", "situacao": "Em Andamento"},
            {"id": 7, "data": "2026-11-03", "hora": None, "origem": "equipe"},              # sem hora: fica até o fim do dia
            {"id": 8, "data": "2026-11-03", "hora": "16:00", "origem": "equipe"}]},
        {"data": "2026-11-05", "itens": [{"id": 9, "data": "2026-11-05", "hora": "09:00", "origem": "camara"}]},
    ]
    prox, hist = meu.separar_agenda(dias, agora)
    assert [e["id"] for d in prox for e in d["itens"]] == [3, 6, 7, 8, 9]
    assert [d["data"] for d in hist] == ["2026-11-03", "2026-11-01"]
    assert sorted(e["id"] for e in hist[0]["itens"]) == [2, 4, 5]


def test_agenda_pdf():
    """Impressão da agenda: período escolhido, PDF válido e período inválido volta com aviso."""
    from datetime import date
    import agenda_pdf as ap
    ap.foto_redonda = lambda url, lado=360: None          # sem rede nos testes
    hoje = date(2026, 11, 4)                               # quarta-feira
    assert ap.periodo({"periodo": "semana"}, hoje)[:2] == (date(2026, 11, 2), date(2026, 11, 8))
    assert ap.periodo({"periodo": "proxima_semana"}, hoje)[:2] == (date(2026, 11, 9), date(2026, 11, 15))
    assert ap.periodo({"periodo": "mes"}, hoje)[:2] == (date(2026, 11, 1), date(2026, 11, 30))
    assert ap.periodo({"periodo": "data", "de": "2026-12-01"}, hoje)[:2] == (date(2026, 12, 1),) * 2
    assert ap.periodo({"periodo": "intervalo", "de": "2026-11-20", "ate": "2026-11-10"}, hoje)[:2] == \
        (date(2026, 11, 10), date(2026, 11, 20))
    for ruim in ({"periodo": "data"}, {"periodo": "intervalo", "de": "2026-01-01", "ate": "2026-12-31"}):
        try:
            ap.periodo(ruim, hoje)
            assert False, ruim
        except ValueError:
            pass
    adm = _cliente()
    r = adm.get("/meu/101/agenda.pdf?periodo=semana&descricao=1")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:5] == b"%PDF-"
    r = adm.get("/meu/101/agenda.pdf?periodo=intervalo&de=2026-01-01")
    assert r.status_code == 302
    assert "Imprimir agenda" in adm.get("/meu/101?secao=agenda").get_data(as_text=True)


def test_importa_usuarios_do_minha_pauta():
    import base64
    import sqlite3
    import bcrypt
    import usuarios_mp
    from sqlalchemy import create_engine
    caminho = os.path.join(_tmp, "minha_pauta_usuarios.db")
    db = sqlite3.connect(caminho)
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, role TEXT, "
               "categoria TEXT, foto TEXT, nome_display TEXT, responsavel_pauta INTEGER)")
    h = lambda s: bcrypt.hashpw(s.encode(), bcrypt.gensalt(4)).decode()
    foto = "data:image/png;base64," + base64.b64encode(_png((10, 80, 160))).decode()
    db.executemany("INSERT INTO users (username, password, role, categoria, foto, nome_display) VALUES (?,?,?,?,?,?)", [
        ("admin", h("senha-do-mp"), "Admin", "geral", None, "Admin"),
        ("marcelo.uvara", h("marcelo123"), "Admin", "oposicao", foto, "Marcelo Uvara"),
        ("Luisa.Marreco", h("luisa123"), "Assessor", "minoria", foto, "Luisa Marreco"),
        ("PL", h("123"), "Assessor", "restrito", None, "Orientação"),
    ])
    db.commit()
    adm = _cliente()
    with create_engine(f"sqlite:///{caminho}").connect() as cf:
        r = usuarios_mp.importar(cf)
    assert {"marcelo.uvara", "luisa.marreco", "pl"} <= set(r["criados"]) and r["admins"] == ["marcelo.uvara"]
    assert "admin" in r["atualizados"]
    adms = {x[0] for x in _q("SELECT login FROM usuarios WHERE admin")}
    assert adms == {"marcelo.uvara"}                                       # só Marcelo é administrador
    nome, = _q("SELECT nome FROM usuarios WHERE login='luisa.marreco'")[0]
    assert nome == "Luisa Marreco"
    eq = dict(_q("SELECT login, equipe FROM usuarios WHERE login IN ('luisa.marreco','pl','admin')"))
    assert eq == {"luisa.marreco": "minoria", "pl": "restrito", "admin": "geral"}
    lid = _q("SELECT id FROM usuarios WHERE login='luisa.marreco'")[0][0]
    assert _q("SELECT origem FROM fotos_usuarios WHERE usuario_id = :u", u=lid)[0][0] == "minha_pauta"
    # login com a senha do Minha Pauta (bcrypt), que vira o hash do painel no 1º acesso
    u = _cliente_usuario("Luisa.Marreco", "luisa123")
    assert u.get("/orientacoes").status_code == 200
    assert _q("SELECT senha_hash FROM usuarios WHERE id = :u", u=lid)[0][0].startswith(("scrypt:", "pbkdf2:"))
    assert u.get("/orientacoes").status_code == 200                         # sessão continua após o rehash
    assert _cliente_usuario("luisa.marreco", "luisa123").get("/usuarios").status_code == 403  # não é admin
    m = _cliente_usuario("marcelo.uvara", "marcelo123")
    html = m.get("/usuarios").get_data(as_text=True)
    assert "Minoria (1)" in html and 'eq-sel eq-minoria' in html
    assert "<b>luisa.marreco</b>" in m.get("/usuarios?equipe=minoria").get_data(as_text=True)
    assert "<b>luisa.marreco</b>" not in m.get("/usuarios?equipe=oposicao").get_data(as_text=True)
    mid = _q("SELECT id FROM usuarios WHERE login='marcelo.uvara'")[0][0]
    m.post("/usuarios", data={"_csrf": _tok(m, "/usuarios"), "acao": "equipe", "id": mid, "equipe": "oposicao"})
    assert _q("SELECT equipe FROM usuarios WHERE id = :u", u=mid)[0][0] == "oposicao"
    assert "Importar do Minha Pauta" in html and "Só marcelo.uvara fica como administrador" in html
    c = _anonimo()
    tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
    assert c.post("/login", data={"_csrf": tok, "login": "luisa.marreco", "senha": "errada"}).status_code == 200
    # reimportar não troca a senha que já foi convertida, a menos que mude no Minha Pauta
    with create_engine(f"sqlite:///{caminho}").connect() as cf:
        r2 = usuarios_mp.importar(cf)
    assert "luisa.marreco" not in r2["atualizados"]
    assert _cliente_usuario("luisa.marreco", "luisa123")
    db.execute("UPDATE users SET password = ? WHERE username = 'Luisa.Marreco'", (h("nova-senha-1"),))
    db.commit()
    with create_engine(f"sqlite:///{caminho}").connect() as cf:
        assert "luisa.marreco" in usuarios_mp.importar(cf)["atualizados"]
    assert _cliente_usuario("luisa.marreco", "nova-senha-1")
    assert usuarios_mp.ja_importou() and usuarios_mp.importar_se_preciso() is None
    # sem nenhum dos administradores previstos, ninguém perde o acesso
    os.environ["ADMINS_PAINEL"] = "fulano.inexistente"
    try:
        with create_engine(f"sqlite:///{caminho}").connect() as cf:
            assert usuarios_mp.importar(cf)["admins"] == []
        assert {x[0] for x in _q("SELECT login FROM usuarios WHERE admin")} == {"marcelo.uvara"}
    finally:
        os.environ.pop("ADMINS_PAINEL", None)
    # devolve o admin dos testes
    with engine.begin() as cx:
        cx.execute(text("UPDATE usuarios SET admin = :t WHERE login = 'admin'"), {"t": True})
        from werkzeug.security import generate_password_hash
        cx.execute(text("UPDATE usuarios SET senha_hash = :h WHERE login = 'admin'"),
                   {"h": generate_password_hash("senha-de-teste-123")})


def test_radar_qualquer_noticia_vira_solicitacao_com_responsavel():
    from datetime import date, datetime, timedelta
    from db import noticias
    from sqlalchemy import insert as ins
    adm = _cliente()
    tok = _tok(adm, "/usuarios")
    adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": "rita", "nome": "Rita Souza", "senha": "senha-segura-1"})
    with engine.begin() as c:
        nid = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Ministério muda regra do Pix",
                                             link="https://exemplo.com/pix", fonte="Folha", publicado=datetime.now(),
                                             origem="google", relevante=False, justificativa="Sem pertinência",
                                             analisado_em=datetime.now())).inserted_primary_key[0]
        nid2 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Notícia ainda não analisada",
                                              link="https://exemplo.com/x", origem="portal", fonte="Agência",
                                              publicado=datetime.now())).inserted_primary_key[0]
    html = adm.get("/meu/101?secao=radar").get_data(as_text=True)
    bloco = html.split(f'id="n{nid}"')[1].split("</article>")[0]
    assert 'name="responsavel"' in bloco and "Atribuir responsável / gerar ação" in bloco and "Rita Souza" in bloco
    assert 'name="responsavel"' in html.split(f'id="n{nid2}"')[1].split("</article>")[0]     # mesmo sem análise da IA
    tok = _tok(adm, "/meu/101?secao=radar")
    prazo = (date.today() + timedelta(days=2)).isoformat()
    r = adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": nid, "responsavel": "rita",
                                                "status": "Pendente", "prazo": prazo, "tarefa": "Avaliar RIC ao BC"})
    assert r.status_code == 302
    sid, = _q("SELECT solicitacao_id FROM noticias WHERE id = :n", n=nid)[0]
    tit, resp, pz, st, desc, sol = _q("SELECT titulo, responsavel, prazo, status, descricao, solicitante FROM solicitacoes "
                                      "WHERE id = :s", s=sid)[0]
    assert tit == "Radar: Ministério muda regra do Pix" and resp == "rita" and pz == prazo and st == "aberta"
    assert "https://exemplo.com/pix" in desc and "Avaliar RIC ao BC" in desc and sol == "Radar de notícias"
    assert _q("SELECT assessor FROM noticias WHERE id = :n", n=nid)[0][0] == "Rita Souza"
    # aviso de tarefa na tela inicial da Rita
    rita = _cliente_usuario("rita", "senha-segura-1")
    inicio = rita.get("/orientacoes").get_data(as_text=True)
    assert "Radar: Ministério muda regra do Pix" in inicio and "Nova tarefa atribuída a você" in inicio
    # salvar de novo não duplica; "Em análise" vira "em andamento"
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": nid, "responsavel": "rita", "status": "Em análise"})
    assert _q("SELECT COUNT(*) FROM solicitacoes WHERE titulo LIKE 'Radar: Ministério%'")[0][0] == 1
    assert _q("SELECT status FROM solicitacoes WHERE id = :s", s=sid)[0][0] == "andamento"
    html = adm.get("/meu/101?secao=radar&filtro=Em análise").get_data(as_text=True)
    assert "Ministério muda regra do Pix" in html and "ver solicitação" in html                     # filtro vale p/ qualquer notícia
    # ação efetuada conclui a solicitação e leva ao clipping
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": nid, "responsavel": "rita",
                                            "status": "Ação efetuada", "acao_efetuada": "RIC 900/2026 protocolado ao BC"})
    st, res, ds, clip = _q("SELECT status, resumo_realizado, data_solucao, no_clipping FROM solicitacoes WHERE id = :s", s=sid)[0]
    assert st == "concluida" and res == "RIC 900/2026 protocolado ao BC" and ds == date.today().isoformat() and clip
    assert "RIC 900/2026 protocolado ao BC" in adm.get("/meu/101?secao=clipping").get_data(as_text=True)
    assert "Radar: Ministério" not in rita.get("/orientacoes").get_data(as_text=True)              # saiu das pendências
    # e o caminho inverso: reabrir a solicitação devolve a notícia para "Pendente"
    t2 = _tok(adm, "/meu/101?secao=solicitacoes")
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": t2, "acao": "status", "id": sid, "status": "aberta"})
    assert _q("SELECT status FROM noticias WHERE id = :n", n=nid)[0][0] == "Pendente"
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": t2, "acao": "concluir", "id": sid, "no_clipping": "1",
                                                  "resumo": "Resposta do BC recebida"})
    assert _q("SELECT status, acao_efetuada FROM noticias WHERE id = :n", n=nid)[0] == ("Ação efetuada", "Resposta do BC recebida")
    # sem responsável: só acompanha a notícia, sem solicitação
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": nid2, "status": "Em análise"})
    assert _q("SELECT status, solicitacao_id FROM noticias WHERE id = :n", n=nid2)[0] == ("Em análise", None)
    # excluir a solicitação solta a notícia
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": t2, "acao": "excluir", "id": sid})
    assert _q("SELECT solicitacao_id FROM noticias WHERE id = :n", n=nid)[0][0] is None


def test_tutorial_e_convite_no_primeiro_acesso():
    adm = _cliente()
    tok = _tok(adm, "/usuarios")
    adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": "novato", "nome": "Novato", "senha": "senha-segura-1"})
    u = _cliente_usuario("novato", "senha-segura-1")
    html = u.get("/orientacoes").get_data(as_text=True)
    assert "Primeira vez por aqui?" in html and 'href="/ajuda"' in html
    t = u.get("/ajuda").get_data(as_text=True)
    assert "Tutorial rápido" in t and "SEM PROPOSIÇÃO DO DEPUTADO" in t and "Relatório de realizações" in t
    assert "Administração" not in t                                   # só para administradores
    assert "Primeira vez por aqui?" not in u.get("/orientacoes").get_data(as_text=True)   # abrir o tutorial dispensa
    assert "Administração" in adm.get("/ajuda").get_data(as_text=True)
    n2 = "novato2"
    adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": n2, "nome": "N2", "senha": "senha-segura-1"})
    u2 = _cliente_usuario(n2, "senha-segura-1")
    tk = _tok(u2, "/orientacoes")
    u2.post("/ajuda/dispensar", data={"_csrf": tk})
    assert "Primeira vez por aqui?" not in u2.get("/orientacoes").get_data(as_text=True)
    assert _anonimo().get("/ajuda").status_code == 302


def test_selecao_da_ia_distribui_topicos_para_a_oposicao():
    import random
    from datetime import datetime, timedelta
    import questoes
    import selecao_ia as si
    from db import noticias, temas_radar
    from sqlalchemy import insert as ins, delete as dele
    adm = _cliente()
    tok = _tok(adm, "/usuarios")
    for login, nome in (("lianna.souza", "Lianna Souza"), ("marcelo.uvara2", "Marcelo Uvara"), ("marcelo.oliveira", "Marcelo Oliveira"),
                        ("elyesley.silva", "Elyesley Silva"), ("vinicius.melo", "Vinicius Melo"), ("luiz.garibaldi", "Luiz Garibaldi"),
                        ("andre.alencar", "André Alencar")):
        adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": login, "nome": nome, "senha": "senha-segura-1"})
    with engine.begin() as c:
        c.execute(text("UPDATE usuarios SET equipe = 'oposicao' WHERE login IN ('lianna.souza','marcelo.uvara2','elyesley.silva','vinicius.melo','andre.alencar')"))
        c.execute(text("UPDATE usuarios SET equipe = 'minoria' WHERE login IN ('marcelo.oliveira','luiz.garibaldi')"))
        c.execute(text("UPDATE usuarios SET ativo = :f WHERE login = 'marcelo.uvara'"), {"f": False})   # do teste anterior
    eq = si.equipes()
    assert eq["comissao"] == ["lianna.souza"]
    assert eq["outros"] == ["marcelo.uvara2", "elyesley.silva", "vinicius.melo", "luiz.garibaldi", "andre.alencar"]
    assert si.nao_encontrados() == []
    # notícias de dois temas
    agora = datetime.now()
    with engine.begin() as c:
        c.execute(dele(noticias).where(noticias.c.codigo_deputado == 102))
        c.execute(ins(temas_radar).values(codigo_deputado=102, termo="segurança pública"))
        ids = {}
        for i, (termo, tit) in enumerate([("Bruno Governo", "Bruno Governo critica ministro da Justiça"),
                                          ("Bruno Governo", "Bruno Governo cobra ministro sobre presídios"),
                                          ("Bruno Governo", "Bruno Governo apresenta projeto sobre Pix"),
                                          ("segurança pública", "Facções avançam no Nordeste"),
                                          ("segurança pública", "Governo corta verba da PF"),
                                          ("segurança pública", "Facções dominam presídios")]):
            ids[i] = c.execute(ins(noticias).values(codigo_deputado=102, termo=termo, titulo=tit, link=f"https://n/{i}",
                                                    fonte="G1", origem="google", publicado=agora - timedelta(days=1))).inserted_primary_key[0]
        c.execute(ins(noticias).values(codigo_deputado=102, termo="Bruno Governo", titulo="Notícia antiga", link="https://n/old",
                                       origem="google", publicado=agora - timedelta(days=30)))
    resposta = {"temas": [
        {"tema": "Bruno Governo", "topicos": [
            {"topico": "Cobranças ao ministro da Justiça sobre presídios", "area": "segurança pública", "resumo": "Duas notícias.",
             "instrumento": "Requerimento de convocação de ministro", "tipo": "outros",
             "acao_sugerida": "Requerimento de convocação do ministro da Justiça na CSPCCO para explicar a crise nos presídios federais",
             "noticias": [ids[0], ids[1], 999999]},
            {"topico": "Projeto do deputado sobre o Pix", "area": "Economia", "resumo": "Uma notícia.", "instrumento": "Nota à imprensa",
             "tipo": "outros", "acao_sugerida": "Nota à imprensa do Líder defendendo o projeto e rebatendo a taxação do Pix",
             "noticias": [ids[2]]},
            {"topico": "Lula", "area": "Política", "instrumento": "Nota à imprensa", "acao_sugerida": "Nota criticando o presidente pela fala",
             "noticias": [ids[0]]},                                                   # título sem contexto: descartado
            {"topico": "Fala do ministro em entrevista", "area": "Política", "instrumento": "Discurso em Plenário",
             "acao_sugerida": "Avaliar as notícias", "noticias": [ids[1]]}]},       # providência genérica: descartada
        {"tema": "Segurança Pública", "topicos": [
            {"topico": "Avanço das facções no Nordeste", "area": "Segurança Pública", "instrumento": "RIC",
             "acao_sugerida": "RIC ao Ministério da Justiça pedindo dados de presos ligados a facções por estado do Nordeste",
             "tipo": "outros", "noticias": [ids[3], ids[5]]},
            {"topico": "Corte de verbas da Polícia Federal", "area": "Inventada", "instrumento": "Projeto de Lei (PL)",
             "acao_sugerida": "PL que proíbe contingenciamento das verbas de custeio da Polícia Federal", "tipo": "xyz",
             "noticias": [ids[4]]}]}]}
    pedidos = []
    orig_cfg, orig_ia = questoes.configurado, questoes.chamar_ia
    questoes.configurado = lambda: True
    questoes.chamar_ia = lambda msg, *a, **k: (pedidos.append(msg), "Aqui está:\n" + json.dumps(resposta))[1]
    try:
        tok = _tok(adm, "/meu/102?secao=radar&filtro=selecao")
        html = adm.get("/meu/102?secao=radar&filtro=selecao").get_data(as_text=True)
        assert "Seleção da IA" in html and "Nenhuma seleção ainda" in html and "Distribuição por sorteio" in html
        antes_sols = _q("SELECT COUNT(*) FROM solicitacoes")[0][0]
        random.seed(3)
        r = adm.post("/meu/102?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_gerar"})
        assert r.status_code == 302 and "#lote-" in r.headers["Location"]
    finally:
        questoes.configurado, questoes.chamar_ia = orig_cfg, orig_ia
    assert "Notícia antiga" not in pedidos[0] and "### Tema: segurança pública" in pedidos[0]
    assert "últimas 72 horas" in pedidos[0] and "(G1)" in pedidos[0] and "1 veículos" in pedidos[0]
    assert "MAIS COMENTADOS" in si.SISTEMA and "MAIOR NÚMERO POSSÍVEL" in si.SISTEMA
    # nada vira solicitação sem confirmação humana
    assert _q("SELECT COUNT(*) FROM solicitacoes")[0][0] == antes_sols
    sel = si.ultimo_lote(102)
    assert sel["total"] == 4 and sel["pendente"] == 4 and set(sel["temas"]) == {"Bruno Governo", "segurança pública"}
    tops = {t["topico"]: t for ts in sel["temas"].values() for t in ts}
    assert "Lula" not in tops and "Fala do ministro em entrevista" not in tops
    assert all(t["status"] == "pendente" and not t["responsavel"] and not t["solicitacao_id"] for t in tops.values())
    cob = tops["Cobranças ao ministro da Justiça sobre presídios"]
    assert cob["tipo"] == "comissao" and cob["area"] == "Segurança Pública" and len(cob["noticias_l"]) == 2
    pf = tops["Corte de verbas da Polícia Federal"]
    assert pf["tipo"] == "outros" and pf["area"] == "Outros" and pf["instrumento"] == "Projeto de Lei (PL)"
    assert tops["Avanço das facções no Nordeste"]["instrumento"] == "Requerimento de Informação (RIC)"
    assert si.n_pendentes(102) == 4
    html = adm.get("/meu/102?secao=radar&filtro=selecao").get_data(as_text=True)
    assert "Seleção da IA — pendentes de abertura" in html and "4 tópicos aguardando" in html
    assert f"Lote</span> <b>{sel['gerado_em'].strftime('%d/%m/%Y')} às {sel['gerado_em'].strftime('%H:%M')}</b>" in html
    assert "Cobranças ao ministro da Justiça sobre presídios" in html and "CSPCCO para explicar" in html
    assert 'class="sel-inst">Requerimento de convocação de ministro' in html and 'class="area-chip">Economia' in html
    assert "Abrir solicitação" in html and "Abrir solicitações selecionadas" in html and "ver solicitação" not in html
    assert "✨ Seleção da IA (4)" in adm.get("/meu/102?secao=radar").get_data(as_text=True)
    li = _cliente_usuario("lianna.souza", "senha-segura-1")
    assert "Cobranças ao ministro da Justiça" not in li.get("/orientacoes").get_data(as_text=True)   # sem tarefa ainda
    # abrir um só, com sorteio → comissão → Lianna
    r = adm.post("/meu/102?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_decidir", "um": cob["id"],
                                                              f"resp_{cob['id']}": si.SORTEAR})
    assert r.status_code == 302 and f"#t{cob['id']}" in r.headers["Location"]
    sol = _q("SELECT responsavel, solicitante, prazo, area, descricao, criado_por, atribuido_por FROM solicitacoes "
             "WHERE titulo = 'Seleção da IA: Cobranças ao ministro da Justiça sobre presídios'")
    assert len(sol) == 1 and sol[0][0] == "lianna.souza" and sol[0][1] == "Seleção da IA (radar)" and sol[0][2]
    assert sol[0][3] == "Segurança Pública" and "1. Requerimento de convocação de ministro: Requerimento de convocação do ministro" in sol[0][4]
    assert "Seleção da IA de " in sol[0][4] and sol[0][5] == "admin" and sol[0][6] == "admin"
    assert _q("SELECT solicitacao_id IS NOT NULL FROM noticias WHERE id = :n", n=ids[0])[0][0]
    assert "Seleção da IA: Cobranças ao ministro da Justiça sobre presídios" in li.get("/orientacoes").get_data(as_text=True)
    # abrir de novo o mesmo: não duplica
    assert si.abrir(102, [cob["id"]], "admin") == []
    # vários de uma vez: um com responsável escolhido, os outros sorteados sem repetir
    pix, fac, pfid = (tops[k]["id"] for k in ("Projeto do deputado sobre o Pix", "Avanço das facções no Nordeste",
                                               "Corte de verbas da Polícia Federal"))
    r = adm.post("/meu/102?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_decidir", "modo": "abrir",
                                                              "ids": [pix, fac], f"resp_{pix}": "andre.alencar",
                                                              f"resp_{fac}": si.SORTEAR, f"resp_{pfid}": "vinicius.melo"})
    resp = dict(_q("SELECT topico, responsavel FROM selecao_ia WHERE id IN (:a, :b)", a=pix, b=fac))
    assert resp["Projeto do deputado sobre o Pix"] == "andre.alencar" and resp["Avanço das facções no Nordeste"] in eq["outros"]
    assert si.n_pendentes(102) == 1                                                         # PF continua pendente
    # descartar e voltar
    adm.post("/meu/102?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_decidir", "descartar_um": pfid})
    assert si.n_pendentes(102) == 0
    html = adm.get("/meu/102?secao=radar&filtro=selecao").get_data(as_text=True)
    assert "1 descartado neste lote" in html and "aberta por" in html and "ver solicitação" in html
    adm.post("/meu/102?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_decidir", "restaurar_um": pfid})
    assert si.n_pendentes(102) == 1
    adm.post("/meu/102?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_decidir", "modo": "abrir"})
    assert "Marque pelo menos um tópico" in adm.get("/meu/102?secao=radar&filtro=selecao").get_data(as_text=True)
    html = adm.get("/meu/102?secao=solicitacoes").get_data(as_text=True)
    assert '<span class="area-chip">Segurança Pública</span>' in html
    # equipes editáveis
    adm.post("/meu/102?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_equipes",
                                                          "comissao": ["lianna.souza", "vinicius.melo"], "outros": ["andre.alencar"]})
    assert si.equipes() == {"comissao": ["lianna.souza", "vinicius.melo"], "outros": ["andre.alencar"]}
    assert not si.precisa_gerar(102, datetime.now().replace(hour=12))                        # já gerou hoje
    assert not si.precisa_gerar(102, datetime.now().replace(hour=6) + timedelta(days=1))     # antes das 7h
    assert si.precisa_gerar(102, datetime.now().replace(hour=8) + timedelta(days=1))
    # sem IA: não gera nada (nada de palavras soltas)
    antes = _q("SELECT COUNT(*) FROM solicitacoes")[0][0]
    try:
        si.gerar(102)
        assert False, "deveria exigir a chave"
    except ValueError as e:
        assert "ANTHROPIC_API_KEY" in str(e)
    # IA respondendo só coisas genéricas duas vezes: erro, sem criar solicitações
    questoes.configurado = lambda: True
    questoes.chamar_ia = lambda *a, **k: json.dumps({"temas": [{"tema": "Bruno Governo", "topicos": [
        {"topico": "Lula", "instrumento": "Nota", "acao_sugerida": "Avaliar notícias"}]}]})
    try:
        si.gerar(102)
        assert False
    except ValueError as e:
        assert "não devolveu uma seleção utilizável" in str(e)
    finally:
        questoes.configurado, questoes.chamar_ia = orig_cfg, orig_ia
    assert _q("SELECT COUNT(*) FROM solicitacoes")[0][0] == antes
    # limpeza do que a versão por palavras gerou
    from db import selecao_ia as tab, solicitacoes as tsol
    with engine.begin() as c:
        sid_lixo = c.execute(ins(tsol).values(codigo_deputado=102, titulo="Seleção da IA: Flavio", status="aberta")).inserted_primary_key[0]
        c.execute(ins(tab).values(codigo_deputado=102, lote="2026-01-01T08:00:00", tema="x", topico="Flavio",
                                  acao_sugerida="Avaliar as notícias e propor a providência cabível.", solicitacao_id=sid_lixo))
    assert si.limpar_selecao_por_palavras() == 1
    assert not _q("SELECT id FROM solicitacoes WHERE id = :s", s=sid_lixo)
    with engine.begin() as c:
        c.execute(text("UPDATE usuarios SET ativo = :t WHERE login = 'marcelo.uvara'"), {"t": True})


def test_area_automatica_e_hora_de_criacao():
    import areas
    adm = _cliente()
    tok = _tok(adm, "/meu/101?secao=solicitacoes")
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "nova", "titulo": "RIC sobre falta de vacinas nas UBS",
                                                  "descricao": "Pedido sobre o SUS"})
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": tok, "acao": "nova", "titulo": "Nota sobre o acordo com a Venezuela"})
    with engine.begin() as c:
        c.execute(text("UPDATE solicitacoes SET area = NULL WHERE titulo LIKE 'RIC sobre falta%' OR titulo LIKE 'Nota sobre o acordo%'"))
    while areas.classificar(esperar=True):
        pass
    a = dict(_q("SELECT titulo, area FROM solicitacoes WHERE titulo LIKE 'RIC sobre falta%' OR titulo LIKE 'Nota sobre o acordo%'"))
    assert a == {"RIC sobre falta de vacinas nas UBS": "Saúde", "Nota sobre o acordo com a Venezuela": "Política Externa"}
    assert not _q("SELECT id FROM solicitacoes WHERE area IS NULL")                   # todas ficam com área
    html = adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    assert '<span class="area-chip">Saúde</span>' in html and "criada em " in html
    assert re.search(r"criada em \d{2}/\d{2}/\d{4} às \d{2}:\d{2}", html)
    import meu
    from datetime import datetime, timezone
    assert meu.data_hora(datetime(2026, 11, 3, 21, 55, tzinfo=timezone.utc)) == "03/11/2026 às 18:55"


def test_trocar_responsavel_e_criar_pelo_radar_com_sorteio():
    from datetime import datetime
    import selecao_ia as si
    from db import noticias
    from sqlalchemy import insert as ins
    adm = _cliente()
    tok = _tok(adm, "/usuarios")
    for login, nome in (("lia.com", "Lia Comissão"), ("out.um", "Outro Um"), ("out.dois", "Outro Dois")):
        adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": login, "nome": nome, "senha": "senha-segura-1"})
    si.salvar_equipes(["lia.com"], ["out.um", "out.dois"])
    assert si.tipo_do_texto("Requerimento de convocação do ministro") == "comissao"
    assert si.tipo_do_texto("Nota à imprensa sobre o Pix") == "outros"
    with engine.begin() as c:
        n1 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Ministro some de audiência",
                                            link="https://n/a1", origem="google", publicado=datetime.now(), relevante=True,
                                            tipo_acao="Requerimento de convocação", acao_sugerida="Convocar o ministro na comissão")
                       ).inserted_primary_key[0]
        n2 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Preço do arroz dispara",
                                            link="https://n/a2", origem="google", publicado=datetime.now())).inserted_primary_key[0]
        n3 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Outra alta de preços",
                                            link="https://n/a3", origem="google", publicado=datetime.now())).inserted_primary_key[0]
    tok = _tok(adm, "/meu/101?secao=radar")
    html = adm.get("/meu/101?secao=radar").get_data(as_text=True)
    assert "＋ Criar solicitação" in html and "sortear pelos critérios" in html
    # sem responsável + "Criar solicitação": sorteia pelo tipo
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": n1, "status": "Pendente", "criar": "1"})
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": n2, "status": "Pendente", "criar": "1",
                                            "tarefa": "Nota à imprensa sobre a inflação dos alimentos"})
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": n3, "status": "Pendente", "criar": "1"})
    r1, r2, r3 = (_q("SELECT s.responsavel FROM noticias n JOIN solicitacoes s ON s.id = n.solicitacao_id WHERE n.id = :n", n=x)[0][0]
                  for x in (n1, n2, n3))
    assert r1 == "lia.com"                                                         # convocação → comissão
    assert {r2, r3} == {"out.um", "out.dois"}                                       # equilibra a carga entre os de outros
    assert "sorteado pelos critérios" in adm.get("/meu/101?secao=radar").get_data(as_text=True)
    # trocar o responsável direto na solicitação (e a notícia acompanha)
    sid = _q("SELECT solicitacao_id FROM noticias WHERE id = :n", n=n2)[0][0]
    t2 = _tok(adm, "/meu/101?secao=solicitacoes")
    html = adm.get(f"/meu/101?secao=solicitacoes&abrir={sid}").get_data(as_text=True)
    assert "Trocar responsável…" in html
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": t2, "acao": "responsavel", "id": sid, "responsavel": "lia.com"})
    assert _q("SELECT responsavel, visto_em FROM solicitacoes WHERE id = :s", s=sid)[0] == ("lia.com", None)
    assert _q("SELECT assessor FROM noticias WHERE id = :n", n=n2)[0][0] == "Lia Comissão"
    assert "Responsável: Lia Comissão." in adm.get("/meu/101?secao=solicitacoes").get_data(as_text=True)
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": t2, "acao": "responsavel", "id": sid, "responsavel": "__sortear__"})
    assert _q("SELECT responsavel FROM solicitacoes WHERE id = :s", s=sid)[0][0] in ("out.um", "out.dois")
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": t2, "acao": "responsavel", "id": sid, "responsavel": ""})
    assert _q("SELECT responsavel FROM solicitacoes WHERE id = :s", s=sid)[0][0] is None
    # nova solicitação manual com sorteio
    adm.post("/meu/101?secao=solicitacoes", data={"_csrf": t2, "acao": "nova", "titulo": "Audiência pública sobre o Pix",
                                                  "responsavel": "__sortear__"})
    assert _q("SELECT responsavel FROM solicitacoes WHERE titulo = 'Audiência pública sobre o Pix'")[0][0] == "lia.com"


if __name__ == "__main__":
    for nome, fn in list(globals().items()):
        if nome.startswith("test_"):
            fn()
            print("ok", nome)


PAGINA_NOTICIA = """<html><head><title>Ignorado</title>
<meta property="og:title" content="Hospital do DF fecha UTI por falta de m&eacute;dicos - Correio Braziliense">
<meta property="og:site_name" content="Correio Braziliense">
<meta name="description" content="Pacientes foram transferidos às pressas.">
<meta property="article:published_time" content="2026-11-02T14:30:00-03:00">
</head><body>...</body></html>"""


def test_radar_pesquisa_e_inclusao_manual():
    from datetime import datetime
    import radar
    with engine.begin() as c:
        c.execute(text("DELETE FROM noticias"))
        radar.gravar_noticias(c, 101, "saúde", [
            {"titulo": "Obras paradas em São Paulo", "link": "https://a.exemplo/1", "fonte": "G1", "resumo": "Saúde e educação"},
            {"titulo": "Campeonato termina", "link": "https://a.exemplo/2", "fonte": "Poder360"}], origem="portal")
    # pesquisa: sem acento, várias palavras, em qualquer campo
    assert [n["link"] for n in radar.lista(101, busca="sao paulo")] == ["https://a.exemplo/1"]
    assert [n["link"] for n in radar.lista(101, busca="SAUDE obras")] == ["https://a.exemplo/1"]
    assert [n["link"] for n in radar.lista(101, busca="poder360")] == ["https://a.exemplo/2"]
    assert radar.lista(101, busca="inexistente") == []

    # inclusão manual: lê os metadados da página
    pag = lambda u: (PAGINA_NOTICIA, u)
    nid, nova = radar.incluir_manual(101, {"link": "https://correio.exemplo/uti", "termo": ""}, "admin", baixar_=pag)
    assert nova
    n = next(x for x in radar.lista(101, filtro="manual"))
    assert n["id"] == nid and n["titulo"] == "Hospital do DF fecha UTI por falta de médicos"
    assert n["fonte"] == "Correio Braziliense" and n["resumo"] == "Pacientes foram transferidos às pressas."
    assert n["publicado"] == datetime(2026, 11, 2, 14, 30) and n["termo"] == "Inclusão manual" and n["status"] == "Pendente"
    assert radar.incluir_manual(101, {"link": "https://correio.exemplo/uti"}, baixar_=pag) == (nid, False)   # repetida
    with pytest.raises(ValueError):
        radar.incluir_manual(101, {"link": "correio sem http"})

    def falha(u):
        raise ValueError("HTTP 403")
    with pytest.raises(ValueError, match="título manualmente"):
        radar.incluir_manual(101, {"link": "https://bloqueado.exemplo/x"}, baixar_=falha)
    nid2, _ = radar.incluir_manual(101, {"link": "https://bloqueado.exemplo/x", "titulo": "Matéria fechada",
                                         "termo": "saúde", "publicado": "2026-11-03"}, baixar_=falha)
    n2 = next(x for x in radar.lista(101) if x["id"] == nid2)
    assert n2["fonte"] == "bloqueado.exemplo" and n2["termo"] == "saúde" and n2["publicado"] == datetime(2026, 11, 3)

    # busca avulsa no Google Notícias marca o que já está no radar
    web = radar.buscar_google(101, "Ana Oposição", buscar=_buscar_fake)
    assert web and web[0]["ja_id"] is None and web[0]["fonte"] == "Folha de S.Paulo"

    # tela: pesquisa, botão do Google, resultados com "Incluir" e inclusão pelo formulário
    adm = _cliente()
    html = adm.get("/meu/101?secao=radar&q=obras").get_data(as_text=True)
    assert "Obras paradas em São Paulo" in html and "Campeonato termina" not in html
    assert "Buscar “obras” no Google Notícias" in html and "Incluir notícia pelo link" in html
    assert "Incluídas manualmente (2)" in html
    assert "Incluída manualmente" in adm.get("/meu/101?secao=radar&filtro=manual").get_data(as_text=True)
    import radar as rd
    orig = rd.baixar
    rd.baixar = _buscar_fake
    try:
        html = adm.get("/meu/101?secao=radar&q=Ana%20Oposi%C3%A7%C3%A3o&web=1").get_data(as_text=True)
    finally:
        rd.baixar = orig
    assert "Deputada Ana Oposição visita hospital em SP" in html and "＋ Incluir" in html
    tok = _tok(adm, "/meu/101?secao=radar")
    r = adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "manual", "link": web[0]["link"],
                                               "titulo": web[0]["titulo"], "fonte": web[0]["fonte"], "q": "Ana Oposição"})
    assert r.status_code == 302 and "web=1" in r.headers["Location"]
    assert any(x["link"] == web[0]["link"] and x["origem"] == "manual" for x in radar.lista(101))
    assert radar.buscar_google(101, "Ana Oposição", buscar=_buscar_fake)[0]["ja_id"]
    r = adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "manual", "link": "nada"})
    assert "link completo" in adm.get("/meu/101?secao=radar").get_data(as_text=True)


def test_agenda_proxima_traz_sessao_do_plenario_convocada_depois():
    """Sessão convocada depois da coleta diária aparece na atualização dos próximos dias; dia com
    falha na API fica como estava."""
    from datetime import date, datetime, timedelta
    import app as painel
    import coleta_meu
    import meu
    from db import agenda
    from sqlalchemy import delete as dele
    hoje = coleta_meu.hoje_brt()
    amanha = hoje + timedelta(days=1)
    with engine.begin() as c:
        c.execute(dele(agenda).where(agenda.c.origem == "camara"))
        c.execute(text("DELETE FROM meta WHERE chave = :k"), {"k": coleta_meu.CHAVE_AGENDA_PROXIMA})
    # coleta diária da manhã: só a reunião da comissão
    manha = {hoje: [_evento(95001, hoje.isoformat(), "10:00", "CCJC", "Reunião Deliberativa Ordinária")],
             amanha: [_evento(95003, amanha.isoformat(), "09:00", "CFT", "Audiência Pública")]}
    orig = coleta_meu.pauta
    coleta_meu.pauta = lambda eid: []
    try:
        coleta_meu.atualizar_agenda_proxima(hoje, dias=1, ler_dia=lambda d: manha.get(d, []))
        assert {r[0] for r in _q("SELECT chave FROM agenda WHERE origem='camara'")} == {"E95001", "E95003"}
        assert not coleta_meu.precisa_agenda_proxima()
        assert coleta_meu.precisa_agenda_proxima(datetime.now() + timedelta(minutes=31))
        # depois: o Presidente convoca a sessão deliberativa; a API falha para amanhã
        tarde = {hoje: manha[hoje] + [_evento(95002, hoje.isoformat(), "13:55", "PLEN",
                                              "Sessão Deliberativa Extraordinária", "Plenário")]}
        def ler(d):
            if d not in tarde:
                raise RuntimeError("Falha ao obter /eventos após 7 tentativas")
            return tarde[d]
        coleta_meu.atualizar_agenda_proxima(hoje, dias=1, ler_dia=ler)
    finally:
        coleta_meu.pauta = orig
    chaves = {r[0] for r in _q("SELECT chave FROM agenda WHERE origem='camara'")}
    assert chaves == {"E95001", "E95002", "E95003"}          # amanhã (falha) preservado
    # aparece na agenda do deputado (Plenário vale para todos) e em "próximos" antes do horário
    from app import app as _app
    with _app.test_request_context("/"):
        from flask import g as _g
        _g.usuario = {"id": 1, "login": "admin", "admin": True}
        dias = meu.agenda_deputado(101, hoje, hoje, so_dele=True)
    plen = next(e for d in dias for e in d["itens"] if e["chave"] == "E95002")
    assert plen["titulo"] == "Plenário · Sessão Deliberativa Extraordinária" and plen["membro"]
    prox, _ = meu.separar_agenda(dias, datetime.combine(hoje, datetime.min.time()) + timedelta(hours=11, minutes=34))
    assert any(e["chave"] == "E95002" for d in prox for e in d["itens"])
    # agendador: roda quando passou o intervalo; AUTO_AGENDA=0 desliga
    os.environ["AUTO_AGENDA"] = "0"
    try:
        assert painel.agenda_proxima_se_preciso() is False
    finally:
        os.environ.pop("AUTO_AGENDA", None)


def test_agenda_relida_ao_acessar():
    import time
    from datetime import datetime, timedelta
    import coleta_meu
    chamadas = []
    orig = coleta_meu.atualizar_agenda_proxima

    def falso(*a, **k):
        chamadas.append(1)
        time.sleep(0.3)
        coleta_meu._gravar_meta(coleta_meu.CHAVE_AGENDA_PROXIMA, datetime.now().isoformat(timespec="seconds"))
    coleta_meu.atualizar_agenda_proxima = falso
    os.environ["AGENDA_AO_ACESSAR"] = "1"
    try:
        with engine.begin() as c:
            c.execute(text("DELETE FROM meta WHERE chave = :k"), {"k": coleta_meu.CHAVE_AGENDA_PROXIMA})
        adm = _cliente()
        html = adm.get("/meu/101?secao=agenda").get_data(as_text=True)
        assert "Conferindo a agenda com a Câmara" in html and 'data-rodando="1"' in html
        st = adm.get("/meu/101/agenda/status").get_json()
        assert st["rodando"] is True
        adm.get("/meu/101?secao=agenda")                      # segunda abertura durante a leitura: não duplica
        for _ in range(50):
            if not coleta_meu.status_agenda_proxima()["rodando"]:
                break
            time.sleep(0.05)
        st = adm.get("/meu/101/agenda/status").get_json()
        assert st["rodando"] is False and st["atualizado"] and chamadas == [1]
        html = adm.get("/meu/101?secao=agenda").get_data(as_text=True)   # acabou de ler: não relê de novo
        assert chamadas == [1] and "conferida com a Câmara agora há pouco" in html and 'data-rodando="0"' in html
        assert coleta_meu.reler_agenda_ao_acessar(datetime.now() + timedelta(seconds=61))   # passado 1 min, relê
        for _ in range(50):
            if not coleta_meu.status_agenda_proxima()["rodando"]:
                break
            time.sleep(0.05)
        assert chamadas == [1, 1]
        assert _anonimo().get("/meu/101/agenda/status").status_code in (302, 401, 403)
    finally:
        coleta_meu.atualizar_agenda_proxima = orig
        os.environ["AGENDA_AO_ACESSAR"] = "0"


def test_alerta_8_de_janeiro_retirado():
    import alertas
    from datetime import datetime
    from db import termos_alerta
    from sqlalchemy import insert as ins
    with engine.begin() as c:
        c.execute(text("DELETE FROM meta WHERE chave = 'alertas:retirado_8_de_janeiro'"))
        c.execute(text("DELETE FROM termos_alerta WHERE termo = '8 de janeiro'"))
        c.execute(ins(termos_alerta).values(termo="8 de janeiro", criado_por="padrão", criado_em=datetime.now()))
    assert "8 de janeiro" not in alertas.TERMOS_PADRAO
    assert "8 de janeiro" not in alertas.termos()                  # banco já existente: sai da lista salva
    assert "anistia" in alertas.termos()
    alertas.adicionar("8 de janeiro", "admin")                     # se a equipe quiser de volta, fica
    assert "8 de janeiro" in alertas.termos()
    alertas.remover("8 de janeiro")


def test_selecao_devolve_as_abertas_pela_ia_e_mantem_lotes():
    """Solicitações abertas sozinhas pela versão anterior voltam a pendentes (as já trabalhadas ficam); lotes
    antigos com pendentes continuam visíveis, cada um com dia e hora."""
    from datetime import datetime
    import selecao_ia as si
    from db import anexos, meta, noticias, selecao_ia as tab, solicitacoes as tsol
    from sqlalchemy import insert as ins
    with engine.begin() as c:
        c.execute(text("DELETE FROM selecao_ia WHERE codigo_deputado = 103"))
        c.execute(text("DELETE FROM meta WHERE chave = :k"), {"k": si.CHAVE_DEVOLVIDAS})
        def velho(topico, status="aberta", resumo=None, anexo=False, lote="2026-11-04T07:05:00"):
            sid = c.execute(ins(tsol).values(codigo_deputado=103, titulo="Seleção da IA: " + topico, status=status,
                                             responsavel="out.um", resumo_realizado=resumo,
                                             solicitante="Seleção da IA (radar)")).inserted_primary_key[0]
            if anexo:
                c.execute(ins(anexos).values(solicitacao_id=sid, nome="x.pdf"))
            nid = c.execute(ins(noticias).values(codigo_deputado=103, termo="t", titulo=topico, link=f"https://v/{sid}",
                                                 origem="google", solicitacao_id=sid)).inserted_primary_key[0]
            rid = c.execute(ins(tab).values(codigo_deputado=103, lote=lote, tema="t", topico=topico, tipo="outros",
                                            acao_sugerida="Nota à imprensa sobre o caso", noticias=json.dumps([nid]),
                                            responsavel="out.um", solicitacao_id=sid,
                                            criado_em=datetime.now())).inserted_primary_key[0]
            return rid, sid, nid
        intocada = velho("Tópico intocado")
        andamento = velho("Tópico em andamento", status="andamento")
        com_anexo = velho("Tópico com anexo", anexo=True)
        outro_lote = velho("Tópico do lote de ontem", lote="2026-11-03T07:10:00")
    assert si.devolver_abertas_pela_ia() == 2
    assert si.devolver_abertas_pela_ia() == 0                                   # roda uma vez só
    assert not _q("SELECT id FROM solicitacoes WHERE id IN (:a, :b)", a=intocada[1], b=outro_lote[1])
    assert _q("SELECT status, solicitacao_id, responsavel FROM selecao_ia WHERE id = :i", i=intocada[0])[0] == ("pendente", None, None)
    assert _q("SELECT solicitacao_id FROM noticias WHERE id = :n", n=intocada[2])[0][0] is None
    for rid, sid, _ in (andamento, com_anexo):
        assert _q("SELECT status, solicitacao_id FROM selecao_ia WHERE id = :i", i=rid)[0] == ("aberta", sid)
    ls = si.lotes(103)
    assert [l["lote"] for l in ls] == ["2026-11-04T07:05:00", "2026-11-03T07:10:00"]
    assert ls[0]["pendente"] == 1 and ls[0]["aberta"] == 2 and ls[1]["pendente"] == 1
    r = _cliente().get("/meu/103?secao=radar&filtro=selecao")
    if r.status_code == 200:
        html = r.get_data(as_text=True)
        assert "Lote</span> <b>04/11/2026 às 07:05</b>" in html and "Lote</span> <b>03/11/2026 às 07:10</b>" in html


def test_prelista_envio_pelo_whatsapp_e_aprovacao_da_chefia():
    """Notícias de qualquer filtro e tópicos da Seleção da IA vão para a pré-lista; o envio gera imagem e link
    público; a chefia aprova parte, escolhe analista ou sorteia; o resto fica recusado."""
    import random
    from datetime import datetime, timedelta
    from urllib.parse import unquote
    import prelista as pl
    import selecao_ia as si
    from db import noticias, selecao_ia as tab
    from sqlalchemy import insert as ins
    adm = _cliente()
    tok = _tok(adm, "/usuarios")
    for login, nome in (("ana.com", "Ana Comissão"), ("beto.out", "Beto Outros"), ("caio.out", "Caio Outros")):
        adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": login, "nome": nome, "senha": "senha-segura-1"})
    si.salvar_equipes(["ana.com"], ["beto.out", "caio.out"])
    agora = datetime.now()
    with engine.begin() as c:
        c.execute(text("DELETE FROM prelista")); c.execute(text("DELETE FROM prelista_envios"))
        n1 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Ministro falta à comissão de novo",
                                            link="https://p/1", fonte="Folha", origem="google", publicado=agora, relevante=True,
                                            tipo_acao="Requerimento de convocação", acao_sugerida="Convocar o ministro na comissão",
                                            status="Em análise")).inserted_primary_key[0]
        n2 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Preço da gasolina sobe pela 3ª vez",
                                            link="https://p/2", fonte="G1", origem="portal", publicado=agora)).inserted_primary_key[0]
        n3 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Notícia que ninguém quer",
                                            link="https://p/3", origem="google", publicado=agora)).inserted_primary_key[0]
        t1 = c.execute(ins(tab).values(codigo_deputado=101, lote=agora.replace(microsecond=0).isoformat(), tema="Ana Oposição",
                                       topico="Fila do INSS passa de 2 milhões", area="Previdência", tipo="outros",
                                       instrumento="Requerimento de Informação (RIC)", status="pendente", noticias=json.dumps([n2]),
                                       acao_sugerida="RIC ao Ministério da Previdência sobre a fila", criado_em=agora)).inserted_primary_key[0]
    tok = _tok(adm, "/meu/101?secao=radar")
    # em qualquer filtro há "Abrir solicitação" e "Enviar à pré-lista"
    for filtro in ("", "relevantes", "Em análise", "portal"):
        html = adm.get(f"/meu/101?secao=radar&filtro={filtro}").get_data(as_text=True)
        assert "＋ Abrir solicitação" in html and "📋 Enviar à pré-lista" in html and "📋 Pré-lista" in html, filtro
    for nid in (n1, n2, n3):
        r = adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "prelista_add", "id": nid, "filtro": "relevantes"})
        assert r.status_code == 302
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "prelista_add", "id": n1})          # repetida
    adm.post("/meu/101?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_decidir", "pre_um": t1})
    assert pl.contagem(101) == 4 and _q("SELECT status FROM selecao_ia WHERE id = :i", i=t1)[0][0] == "prelista"
    html = adm.get("/meu/101?secao=radar").get_data(as_text=True)
    assert "📋 Pré-lista (4)" in html and "📋 na pré-lista" in html
    # retirar um e devolver o tópico; enviar de novo
    item_t1 = _q("SELECT id FROM prelista WHERE origem = 'selecao' AND ref_id = :i", i=t1)[0][0]
    adm.post("/meu/101?secao=radar&filtro=prelista", data={"_csrf": tok, "acao": "prelista_remover", "item": item_t1})
    assert _q("SELECT status FROM selecao_ia WHERE id = :i", i=t1)[0][0] == "pendente" and pl.contagem(101) == 3
    adm.post("/meu/101?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_decidir", "modo": "prelista", "ids": [t1]})
    html = adm.get("/meu/101?secao=radar&filtro=prelista").get_data(as_text=True)
    assert "Pré-lista para aprovação da chefia" in html and "Fila do INSS passa de 2 milhões" in html
    assert "Gerar imagem e link para o WhatsApp" in html
    # gerar o envio
    r = adm.post("/meu/101?secao=radar&filtro=prelista", data={"_csrf": tok, "acao": "prelista_enviar"})
    assert r.status_code == 302 and "envio=" in r.headers["Location"]
    e = pl.aberta(101)
    assert len(e["itens"]) == 4 and e["status"] == "aguardando" and pl.contagem(101) == 4 and pl.envios(101) == []
    # o que entra depois vai para a MESMA pré-lista (mesmo link) até a chefia responder
    with engine.begin() as c:
        n5 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Entrou depois do envio",
                                            link="https://p/5", origem="google", publicado=agora)).inserted_primary_key[0]
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "prelista_add", "id": n5})
    e = pl.aberta(101)
    assert len(e["itens"]) == 5 and pl.contagem(101) == 5 and len(_q("SELECT id FROM prelista_envios WHERE status='aguardando'")) == 1
    assert pl.gerar_envio(101, "admin") == e["id"]                                         # não abre outra
    assert "aguardando a chefia" in adm.get("/meu/101?secao=radar").get_data(as_text=True)
    html = adm.get("/meu/101?secao=radar&filtro=prelista").get_data(as_text=True)
    assert "Pré-lista aberta" in html and "incluído depois" in html and "Reenviar pelo WhatsApp" in html
    assert "Gerar imagem e link para o WhatsApp" not in html and "Entrou depois do envio" in html
    # duas listas abertas (versão anterior) são juntadas na mais antiga
    from db import prelista_envios as tenv
    with engine.begin() as c:
        e_dup = c.execute(ins(tenv).values(codigo_deputado=101, token="duplicada-xyz", status="aguardando", criado_por="admin",
                                           criado_em=agora, expira_em=agora + timedelta(days=7))).inserted_primary_key[0]
        c.execute(text("UPDATE prelista SET envio_id = :d WHERE ref_id = :n AND origem = 'noticia'"), {"d": e_dup, "n": n5})
    assert pl.consolidar(101) == e["id"]
    assert _q("SELECT status FROM prelista_envios WHERE id = :i", i=e_dup)[0][0] == "cancelada"
    assert len(pl.aberta(101)["itens"]) == 5
    item_n5 = _q("SELECT id FROM prelista WHERE ref_id = :n AND origem = 'noticia'", n=n5)[0][0]
    html = adm.get(f"/meu/101?secao=radar&filtro=prelista&envio={e['id']}").get_data(as_text=True)
    assert "Enviar pelo WhatsApp" in html and "https://wa.me/?text=" in html and f"/aprovar/{e['token']}" in html
    wa = unquote(re.search(r'href="https://wa.me/\?text=([^"]+)"', html).group(1))
    assert "Pré-lista de providências" in wa and "1. Ministro falta à comissão de novo" in wa and e["token"] in wa
    assert "5. Entrou depois do envio" in wa
    assert "aguardando a chefia" in adm.get("/meu/101?secao=radar").get_data(as_text=True)
    img = adm.get(f"/meu/101/prelista/{e['id']}.png")
    assert img.status_code == 200 and img.data[:8] == b"\x89PNG\r\n\x1a\n" and len(img.data) > 5000
    # página pública: sem login
    anon = _anonimo()
    pub = anon.get(f"/aprovar/{e['token']}")
    page = pub.get_data(as_text=True)
    assert pub.status_code == 200 and "Pré-lista para aprovação" in page and "Fila do INSS" in page
    assert 'property="og:image"' in page and "Beto Outros" in page and "Sortear" in page
    assert anon.get(f"/aprovar/{e['token']}/imagem.png").data[:4] == b"\x89PNG"
    assert anon.get("/aprovar/token-que-nao-existe").status_code == 404
    assert anon.get("/meu/101?secao=radar&filtro=prelista").status_code == 302           # o painel continua fechado
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    ids = {it["titulo"]: it["id"] for it in e["itens"]}
    vistos = [i for t, i in ids.items() if t != "Entrou depois do envio"]          # como se tivesse entrado com a página aberta
    i_min, i_gas, i_inss, i_nao = (ids[k] for k in ("Ministro falta à comissão de novo", "Preço da gasolina sobe pela 3ª vez",
                                                    "Fila do INSS passa de 2 milhões", "Notícia que ninguém quer"))
    assert "Marque pelo menos uma" in anon.post(f"/aprovar/{e['token']}", data={"_csrf": csrf, "nome": "Chefe"}).get_data(as_text=True)
    random.seed(1)
    r = anon.post(f"/aprovar/{e['token']}", data={"_csrf": csrf, "nome": "Dra. Chefe de Gabinete", "vistos": vistos,
                                                  "itens": [i_min, i_gas, i_inss], f"resp_{i_min}": pl.SORTEAR,
                                                  f"resp_{i_gas}": "caio.out", f"resp_{i_inss}": pl.SORTEAR,
                                                  f"resp_{i_nao}": "beto.out"})
    page = r.get_data(as_text=True)
    assert "Pré-lista respondida por Dra. Chefe de Gabinete" in page and "3 solicitações criadas" in page
    assert "Caio Outros" in page and "não aprovada" in page
    sols = {t: (resp, sol, por) for t, resp, sol, por in _q(
        "SELECT titulo, responsavel, solicitante, criado_por FROM solicitacoes WHERE solicitante = 'Pré-lista aprovada pela chefia'")}
    assert sols["Radar: Ministro falta à comissão de novo"][0] == "ana.com"               # convocação → comissão (sorteio)
    assert sols["Radar: Preço da gasolina sobe pela 3ª vez"][0] == "caio.out"            # escolhido pela chefia
    assert sols["Seleção da IA: Fila do INSS passa de 2 milhões"][0] in ("beto.out", "caio.out")
    assert "Radar: Notícia que ninguém quer" not in sols and len(sols) == 3
    assert all(v[2] == "Pré-lista · Dra. Chefe de Gabinete" for v in sols.values())
    assert _q("SELECT status, solicitacao_id IS NOT NULL FROM selecao_ia WHERE id = :i", i=t1)[0] == ("aberta", 1)
    assert _q("SELECT solicitacao_id IS NOT NULL, assessor FROM noticias WHERE id = :n", n=n1)[0] == (1, "Ana Comissão")
    assert _q("SELECT solicitacao_id FROM noticias WHERE id = :n", n=n3)[0][0] is None
    desc = _q("SELECT descricao FROM solicitacoes WHERE titulo = 'Radar: Ministro falta à comissão de novo'")[0][0]
    assert "Aprovada na pré-lista por Dra. Chefe de Gabinete" in desc and "responsável sorteado" in desc and "https://p/1" in desc
    ana = _cliente_usuario("ana.com", "senha-segura-1")
    assert "Radar: Ministro falta à comissão de novo" in ana.get("/orientacoes").get_data(as_text=True)    # aviso de tarefa
    # link vale uma vez
    r = anon.post(f"/aprovar/{e['token']}", data={"_csrf": csrf, "itens": [i_nao]})
    assert "Pré-lista respondida" in r.get_data(as_text=True) and len(_q(
        "SELECT id FROM solicitacoes WHERE solicitante = 'Pré-lista aprovada pela chefia'")) == 3
    html = adm.get("/meu/101?secao=radar&filtro=prelista").get_data(as_text=True)
    assert "Respondida por Dra. Chefe de Gabinete" in html and "3 aprovadas, 1 recusada" in html
    assert _q("SELECT status, envio_id FROM prelista WHERE id = :i", i=item_n5)[0] == ("na_lista", None)   # não foi recusado
    assert pl.contagem(101) == 1 and "Pré-listas encerradas" in html and "Gerar imagem e link" in html
    pl.remover(101, item_n5)
    # cancelar um envio devolve os itens; envio expirado não aceita resposta
    with engine.begin() as c:
        n4 = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Outra",
                                            link="https://p/4", origem="google", publicado=agora)).inserted_primary_key[0]
    pl.adicionar_noticia(101, n4, "admin")
    e2 = pl.envio_por_id(101, pl.gerar_envio(101, "admin"))
    assert pl.cancelar_envio(101, e2["id"]) and pl.contagem(101) == 1
    assert "não está mais disponível" in anon.get(f"/aprovar/{e2['token']}").get_data(as_text=True)
    e3 = pl.envio_por_id(101, pl.gerar_envio(101, "admin"))
    with engine.begin() as c:
        c.execute(text("UPDATE prelista_envios SET expira_em = :d WHERE id = :i"), {"d": agora - timedelta(days=1), "i": e3["id"]})
    assert "expirou" in anon.get(f"/aprovar/{e3['token']}").get_data(as_text=True)
    try:
        pl.responder(e3["token"], {e3["itens"][0]["id"]: pl.SORTEAR}, "x")
        assert False
    except ValueError:
        pass
    # notícia já com solicitação não entra
    try:
        pl.adicionar_noticia(101, n1, "admin")
        assert False
    except ValueError as ex:
        assert "já tem solicitação" in str(ex)


def test_ia_sugere_alternativas_curtas_com_vies_de_oposicao():
    import questoes
    import radar
    import selecao_ia as si
    from datetime import datetime
    from db import noticias, selecao_ia as tab
    from sqlalchemy import insert as ins
    # linha editorial nos dois prompts
    for prompt in (radar.SISTEMA, si.SISTEMA):
        assert "OPOSIÇÃO ao governo federal atual" in prompt and "Não fique em cima do" in prompt
        assert "valores caros à oposição" in prompt and "não invente" in prompt
    assert "2 a 3" in radar.SISTEMA and "NO MÁXIMO 20 palavras" in radar.SISTEMA and '"alternativas"' in radar.SISTEMA
    assert "2 a 3 ALTERNATIVAS" in si.SISTEMA and "NO MÁXIMO 20 palavras" in si.SISTEMA
    # radar: grava as alternativas (curtas, sem instrumento repetido); a 1ª vira tipo_acao/acao_sugerida
    with engine.begin() as c:
        nid = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Governo esconde gastos do cartão",
                                             link="https://alt/1", origem="google", publicado=datetime.now())).inserted_primary_key[0]
    longa = " ".join(["palavra"] * 45)
    resp = {"relevante": True, "categoria": "Gastos públicos / corrupção", "justificativa": "Falta de transparência.",
            "alternativas": [{"tipo_acao": "Requerimento de informações", "acao": "RIC à Casa Civil cobrando os gastos do cartão corporativo"},
                             {"tipo_acao": "Requerimento de informações", "acao": "repetida"},
                             {"tipo_acao": "Denúncia ao TCU/MPF", "acao": longa},
                             {"tipo_acao": "Nota à imprensa", "acao": "Nota do Líder denunciando o sigilo dos gastos"}]}
    orig_cfg, orig_ia = questoes.configurado, questoes.chamar_ia
    questoes.configurado = lambda: True
    questoes.chamar_ia = lambda *a, **k: json.dumps(resp)
    try:
        assert radar.analisar(101, ids=[nid]) == 1
    finally:
        questoes.configurado, questoes.chamar_ia = orig_cfg, orig_ia
    n = next(x for x in radar.lista(101) if x["id"] == nid)
    assert [a["instrumento"] for a in n["alts"]] == ["Requerimento de informações", "Denúncia ao TCU/MPF", "Nota à imprensa"]
    assert n["tipo_acao"] == "Requerimento de informações" and n["acao_sugerida"].startswith("RIC à Casa Civil")
    assert len(n["alts"][1]["acao"].split()) <= radar.MAX_PALAVRAS_ALT and n["alts"][1]["acao"].endswith("…")
    adm = _cliente()
    html = adm.get("/meu/101?secao=radar&filtro=relevantes").get_data(as_text=True)
    assert "Caminhos possíveis — escolha um" in html and f'data-usar="tarefa-{nid}"' in html and "Nota do Líder denunciando" in html
    # ao abrir pelo radar, a solicitação leva a escolha e as alternativas
    tok = _tok(adm, "/meu/101?secao=radar")
    adm.post("/meu/101?secao=radar", data={"_csrf": tok, "acao": "item", "id": nid, "status": "Pendente", "criar": "1",
                                            "tarefa": "Nota à imprensa: Nota do Líder denunciando o sigilo dos gastos"})
    desc = _q("SELECT s.descricao FROM solicitacoes s JOIN noticias n ON n.solicitacao_id = s.id WHERE n.id = :n", n=nid)[0][0]
    assert "O que fazer: Nota à imprensa: Nota do Líder" in desc and "Alternativas sugeridas pela IA" in desc
    assert "2. Denúncia ao TCU/MPF:" in desc
    # registro antigo (sem alternativas) continua aparecendo
    assert radar.alternativas_de({"tipo_acao": "PEC", "acao_sugerida": "PEC X"}) == [{"instrumento": "PEC", "acao": "PEC X"}]
    # seleção: alternativas validadas; ao abrir, a escolhida vira o "caminho escolhido"
    ok = si._topico_ok({"topico": "Sigilo nos gastos do cartão corporativo", "area": "Corrupção e Transparência", "tipo": "outros",
                        "alternativas": [{"instrumento": "RIC", "acao": "RIC à Casa Civil cobrando a abertura dos gastos"},
                                         {"instrumento": "Nota à imprensa", "acao": "Avaliar"},              # genérica: fora
                                         {"instrumento": "Requerimento de convocação de ministro",
                                          "acao": "Convocar o ministro da Casa Civil para explicar o sigilo"},
                                         {"instrumento": "Inventado", "acao": "algo"}]})
    assert [a["instrumento"] for a in ok["alternativas"]] == ["Requerimento de Informação (RIC)", "Requerimento de convocação de ministro"]
    assert ok["instrumento"] == "Requerimento de Informação (RIC)" and ok["tipo"] == "outros"
    with engine.begin() as c:
        tid = c.execute(ins(tab).values(codigo_deputado=101, lote="2026-11-05T09:00:00", tema="Ana Oposição", topico=ok["topico"],
                                        area=ok["area"], tipo="outros", instrumento=ok["instrumento"], acao_sugerida=ok["acao_sugerida"],
                                        alternativas=json.dumps(ok["alternativas"]), noticias="[]", status="pendente",
                                        criado_em=datetime.now())).inserted_primary_key[0]
    html = adm.get("/meu/101?secao=radar&filtro=selecao").get_data(as_text=True)
    assert f'name="alt_{tid}" value="1"' in html and "Escolha o caminho:" in html
    adm.post("/meu/101?secao=radar&filtro=selecao", data={"_csrf": tok, "acao": "selecao_decidir", "um": tid, f"alt_{tid}": "1"})
    desc = _q("SELECT s.descricao FROM solicitacoes s JOIN selecao_ia t ON t.solicitacao_id = s.id WHERE t.id = :t", t=tid)[0][0]
    assert "Caminho escolhido (Requerimento de convocação de ministro): Convocar o ministro" in desc
    assert "Outras alternativas sugeridas pela IA:\n1. Requerimento de Informação (RIC): RIC à Casa Civil" in desc


def test_reanalise_das_relevantes_recentes_ganha_alternativas():
    import questoes
    import radar
    from datetime import datetime, timedelta
    from db import noticias
    from sqlalchemy import insert as ins
    agora = datetime.now()
    with engine.begin() as c:
        c.execute(text("DELETE FROM meta WHERE chave = :k"), {"k": radar.CHAVE_REANALISE})
        c.execute(text("UPDATE noticias SET alternativas = '[]' WHERE alternativas IS NULL"))   # isola o teste
        def nova(tit, dias, rel=True, sid=None):
            return c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo=tit, link=f"https://re/{tit}",
                                                  origem="google", publicado=agora - timedelta(days=dias), relevante=rel,
                                                  tipo_acao="Ofício", acao_sugerida="Sugestão antiga e longa", status="Em análise",
                                                  assessor="Fulano", solicitacao_id=sid)).inserted_primary_key[0]
        recente = nova("Recente relevante", 1, sid=4242)
        vira_nao = nova("IA agora acha que não", 2)
        antiga = nova("Antiga", 20)
        irrelevante = nova("Irrelevante", 1, rel=False)
    respostas = {"Recente relevante": {"relevante": True, "categoria": "Saúde pública", "justificativa": "Falha do governo.",
                                       "alternativas": [{"tipo_acao": "Requerimento de convocação", "acao": "Convocar o ministro da Saúde"},
                                                        {"tipo_acao": "Nota à imprensa", "acao": "Nota denunciando o descaso"}]},
                 "IA agora acha que não": {"relevante": False, "alternativas": [], "justificativa": "x"}}
    chamadas = []
    orig_cfg, orig_ia = questoes.configurado, questoes.chamar_ia
    questoes.configurado = lambda: True
    def ia(msg, *a, **k):
        tit = re.search(r"Título: (.+)", msg).group(1)
        chamadas.append(tit)
        return json.dumps(respostas[tit])
    questoes.chamar_ia = ia
    try:
        assert radar.reanalisar_alternativas() == 1
        assert radar.reanalisar_alternativas() == 0                       # roda uma vez só
    finally:
        questoes.configurado, questoes.chamar_ia = orig_cfg, orig_ia
    assert sorted(chamadas) == ["IA agora acha que não", "Recente relevante"]  # antigas e irrelevantes ficam de fora
    r = _q("SELECT tipo_acao, acao_sugerida, status, assessor, solicitacao_id, relevante FROM noticias WHERE id = :i", i=recente)[0]
    assert r[:2] == ("Requerimento de convocação", "Convocar o ministro da Saúde") and r[2:5] == ("Em análise", "Fulano", 4242)
    assert json.loads(_q("SELECT alternativas FROM noticias WHERE id = :i", i=recente)[0][0])[1]["instrumento"] == "Nota à imprensa"
    assert _q("SELECT acao_sugerida, relevante FROM noticias WHERE id = :i", i=vira_nao)[0] == ("Sugestão antiga e longa", True)
    # IA fora do ar no meio: não marca como concluída
    with engine.begin() as c:
        c.execute(text("DELETE FROM meta WHERE chave = :k"), {"k": radar.CHAVE_REANALISE})
    questoes.configurado = lambda: True
    questoes.chamar_ia = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
    try:
        radar.reanalisar_alternativas()
    finally:
        questoes.configurado, questoes.chamar_ia = orig_cfg, orig_ia
    assert not _q("SELECT valor FROM meta WHERE chave = :k", k=radar.CHAVE_REANALISE)


def test_prelista_acompanha_as_sugestoes_atuais():
    import prelista as pl
    from datetime import datetime
    from db import noticias
    from sqlalchemy import insert as ins
    with engine.begin() as c:
        c.execute(text("DELETE FROM prelista")); c.execute(text("DELETE FROM prelista_envios"))
        nid = c.execute(ins(noticias).values(codigo_deputado=101, termo="Ana Oposição", titulo="Sugestão vai mudar",
                                             link="https://sinc/1", origem="google", publicado=datetime.now(), relevante=True,
                                             tipo_acao="Ofício", acao_sugerida="Sugestão antiga e longa demais")).inserted_primary_key[0]
    pl.adicionar_noticia(101, nid, "admin")
    eid = pl.gerar_envio(101, "admin")
    e = pl.envio_por_id(101, eid)
    assert e["itens"][0]["alts"] == [{"instrumento": "Ofício", "acao": "Sugestão antiga e longa demais"}]
    novas = [{"instrumento": "Requerimento de convocação", "acao": "Convocar o ministro"},
             {"instrumento": "Nota à imprensa", "acao": "Nota denunciando o caso"}]
    with engine.begin() as c:       # como faz a reanálise do radar
        c.execute(text("UPDATE noticias SET alternativas = :a, tipo_acao = 'Requerimento de convocação', "
                       "acao_sugerida = 'Convocar o ministro' WHERE id = :i"), {"a": json.dumps(novas), "i": nid})
    e = pl.aberta(101)
    it = e["itens"][0]
    assert it["alts"] == novas and it["acao"] == "Convocar o ministro" and it["tipo"] == "comissao"
    page = _anonimo().get(f"/aprovar/{e['token']}").get_data(as_text=True)
    assert "Convocar o ministro" in page and "Nota denunciando o caso" in page and "Sugestão antiga" not in page
    assert pl.sincronizar() == 0                                           # nada mais a mudar
    # depois de respondida, o item fica como foi aprovado
    pl.responder(e["token"], {it["id"]: pl.SORTEAR}, "Chefe")
    with engine.begin() as c:
        c.execute(text("UPDATE noticias SET alternativas = :a WHERE id = :i"),
                  {"a": json.dumps([{"instrumento": "PEC", "acao": "Outra"}]), "i": nid})
    pl.sincronizar()
    assert json.loads(_q("SELECT alternativas FROM prelista WHERE id = :i", i=it["id"])[0][0]) == novas


def test_api_de_tarefas_para_o_minha_pauta():
    from datetime import datetime
    from db import solicitacoes
    from sqlalchemy import insert as ins
    anon = _anonimo()
    r = anon.get("/api/tarefas?login=admin")
    assert r.status_code == 503 and r.get_json()["erro"] == "sem_token"           # sem TAREFAS_TOKEN: desligado
    os.environ["TAREFAS_TOKEN"] = "chave-secreta-123"
    try:
        assert anon.get("/api/tarefas?login=admin").status_code == 403
        assert anon.get("/api/tarefas?login=admin", headers={"X-Painel-Token": "errada"}).status_code == 403
        with engine.begin() as c:
            sid = c.execute(ins(solicitacoes).values(codigo_deputado=101, titulo="RIC sobre o apagão", status="aberta",
                                                     responsavel="tarefa.mp", prazo="2026-11-10", atribuido_em=datetime.now(),
                                                     criado_em=datetime.now())).inserted_primary_key[0]
            c.execute(ins(solicitacoes).values(codigo_deputado=101, titulo="Concluída", status="concluida", responsavel="tarefa.mp"))
        r = anon.get("/api/tarefas?login=Tarefa.MP", headers={"X-Painel-Token": "chave-secreta-123"})
        d = r.get_json()
        assert r.status_code == 200 and d["total"] == 1 and d["novas"] == 1 and d["url"].endswith("/orientacoes")
        assert d["itens"][0]["titulo"] == "RIC sobre o apagão" and d["itens"][0]["url"].endswith(f"abrir={sid}#s{sid}")
        assert "descricao" not in d["itens"][0]
        d = anon.get("/api/tarefas?login=ninguem", headers={"X-Painel-Token": "chave-secreta-123"}).get_json()
        assert d["total"] == 0 and d["usuario_encontrado"] is False
        # login diferente no Minha Pauta: casa pelo nome exibido
        adm = _cliente()
        tok = _tok(adm, "/usuarios")
        adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": "tarefa.mp", "nome": "Tárefa Pauta", "senha": "senha-segura-1"})
        d = anon.get("/api/tarefas?login=outro.login&nome=Tarefa%20Pauta", headers={"X-Painel-Token": "chave-secreta-123"}).get_json()
        assert d["login"] == "tarefa.mp" and d["total"] == 1 and d["usuario_encontrado"] is True
    finally:
        os.environ.pop("TAREFAS_TOKEN", None)


def test_selecao_gerar_roda_em_segundo_plano_quando_a_ia_demora():
    """O botão não prende a requisição (o gunicorn mata em 120 s): responde 'em andamento' e grava o lote depois."""
    import threading
    import questoes
    import selecao_ia as si
    from db import engine as eng, noticias
    from sqlalchemy import insert as ins
    from datetime import datetime as dt
    with eng.begin() as c:
        nid = c.execute(ins(noticias).values(codigo_deputado=104, termo="saúde", titulo="Fila do SUS cresce",
                                             fonte="G1", publicado=dt.now(), link="https://x/fila-sus-104")).inserted_primary_key[0]
    libera = threading.Event()
    resposta = {"temas": [{"tema": "saúde", "topicos": [
        {"topico": "Crescimento da fila do SUS", "area": "Saúde",
         "alternativas": [{"instrumento": "RIC", "acao": "RIC ao Ministério da Saúde cobrando o tamanho da fila por estado"},
                          {"instrumento": "Nota à imprensa", "acao": "Nota do Líder denunciando a fila recorde do SUS"}],
         "tipo": "outros", "noticias": [nid]},
        {"topico": "Falta de médicos nos postos", "area": "Saúde",
         "alternativas": [{"instrumento": "Indicação ao Executivo", "acao": "Indicação ao Ministério da Saúde para reforçar o Mais Médicos nas capitais"}],
         "tipo": "outros", "noticias": [nid]}]}]}

    def lenta(*a, **k):
        libera.wait(10)
        return json.dumps(resposta)
    orig_cfg, orig_ia, orig_esp = questoes.configurado, questoes.chamar_ia, si.ESPERA_BOTAO
    questoes.configurado, questoes.chamar_ia = (lambda: True), lenta
    try:
        r = si.gerar_em_segundo_plano(104, esperar=0.2)
        assert r["rodando"]
        assert si.gerar_em_segundo_plano(104, esperar=0)["rodando"]       # um clique a mais não dispara outra rodada
        libera.set()
        for _ in range(100):
            if not si.estado(104).get("rodando"):
                break
            __import__("time").sleep(0.05)
        e = si.estado(104)
        assert not e["rodando"] and e["n"] == 2 and not e.get("erro")
        assert si.ultimo_lote(104)["total"] == 2
    finally:
        questoes.configurado, questoes.chamar_ia, si.ESPERA_BOTAO = orig_cfg, orig_ia, orig_esp


def test_selecao_mostra_o_motivo_quando_a_ia_nao_responde_em_json():
    import questoes
    import selecao_ia as si
    from db import engine as eng, noticias
    from sqlalchemy import insert as ins
    from datetime import datetime as dt
    with eng.begin() as c:
        c.execute(ins(noticias).values(codigo_deputado=105, termo="economia", titulo="Inflação sobe",
                                       fonte="G1", publicado=dt.now(), link="https://x/inflacao-105"))
    pedidos = []

    def prosa(msg, *a, **k):
        pedidos.append(msg)
        questoes.ULTIMA.stop_reason = "end_turn"
        return "Desculpe, não consigo selecionar tópicos com essas notícias."
    orig_cfg, orig_ia = questoes.configurado, questoes.chamar_ia
    questoes.configurado, questoes.chamar_ia = (lambda: True), prosa
    try:
        with pytest.raises(ValueError, match="não respondeu em JSON — começou com: 'Desculpe"):
            si.gerar(105)
        assert len(pedidos) == 2 and "SOMENTE com o objeto JSON" in pedidos[1]
    finally:
        questoes.configurado, questoes.chamar_ia = orig_cfg, orig_ia


def test_chamar_ia_sem_texto_diz_o_motivo(monkeypatch):
    import questoes

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"content": [{"type": "thinking", "thinking": "..."}], "stop_reason": "max_tokens"}

    class S:
        def post(self, *a, **k):
            return R()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    with pytest.raises(RuntimeError, match="sem texto .*max_tokens.*thinking"):
        questoes.chamar_ia("oi", S())
