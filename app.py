"""Painel da Câmara — Liderança da Oposição.  Flask + PostgreSQL (Railway)."""
import hashlib
import logging
import os
import threading
from datetime import date

from flask import (Flask, abort, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)
from jinja2 import ChoiceLoader, FileSystemLoader

import auth
import metrics
import paineis
from db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
RAIZ = os.path.dirname(os.path.abspath(__file__))
# static_folder=None: os arquivos estáticos são servidos pela rota /static abaixo, que aceita
# tanto a pasta static/ quanto arquivos soltos na raiz (upload pelo site do GitHub costuma achatar pastas)
app = Flask(__name__, static_folder=None)
app.jinja_loader = ChoiceLoader([FileSystemLoader(os.path.join(RAIZ, "templates")),
                                 FileSystemLoader(RAIZ)])

TEMPLATES = ("base.html", "_macros.html", "_selo.html", "login.html", "conta.html", "usuarios.html",
             "erro.html", "vazio.html", "perfil.html", "proposicoes.html", "cota.html", "votacoes.html",
             "orientacoes.html", "rankings.html", "meu.html", "meu_escolher.html", "plenario.html")
ESTATICOS = ("style.css", "plenario.svg", "favicon.svg")


def _achar(nome, pasta):
    for base in (os.path.join(RAIZ, pasta), RAIZ):
        if os.path.isfile(os.path.join(base, nome)):
            return base
    return None


_faltando = [t for t in TEMPLATES if not _achar(t, "templates")] + \
            [e for e in ESTATICOS if not _achar(e, "static")]
if _faltando:
    logging.error("ARQUIVOS FALTANDO NO DEPLOY: %s", ", ".join(_faltando))


@app.route("/static/<path:filename>")
def static(filename):
    nome = filename
    if nome not in ESTATICOS:          # só os arquivos do app — nunca expõe .py ou outros da raiz
        abort(404)
    base = _achar(nome, "static")
    if not base:
        abort(404)
    return send_from_directory(base, nome, max_age=3600)

init_db()
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024   # anexos (10 MB cada) e Regimento em PDF
auth.configurar(app)
import meu  # noqa: E402
app.register_blueprint(meu.bp)

ABAS = [("perfil", "Perfil"), ("proposicoes", "Proposições"), ("cota", "Cota"),
        ("votacoes", "Votações"), ("orientacoes", "Orientações"), ("rankings", "Rankings"),
        ("plenario", "Plenário")]
ABAS_DEPUTADO = {"perfil", "proposicoes", "cota"}   # abas com seletor de deputado
ABAS_LIVRES = {"plenario"}                          # abas que não usam os filtros de votação


@app.context_processor
def _ctx():
    return {"abas": ABAS, "abas_deputado": ABAS_DEPUTADO, "abas_livres": ABAS_LIVRES, "versao_css": _versao_css()}


def _versao_css():
    """Muda quando o style.css muda: força o navegador a baixar o arquivo novo em vez do guardado em cache."""
    global _VERSAO_CSS
    if _VERSAO_CSS is None:
        base = _achar("style.css", "static")
        try:
            with open(os.path.join(base, "style.css"), "rb") as f:
                _VERSAO_CSS = hashlib.md5(f.read()).hexdigest()[:10]
        except (OSError, TypeError):
            _VERSAO_CSS = "0"
    return _VERSAO_CSS


_VERSAO_CSS = None


def _contexto_filtros():
    f = metrics.Filtros.from_args(request.args)
    return f, metrics.opcoes_filtro()


# ---------------- filtros de template ----------------

@app.template_filter("pct")
def _fmt_pct(v):
    return "—" if v is None else f"{v:.1f}%".replace(".", ",")


@app.template_filter("data")
def _fmt_data(v):
    if not v:
        return ""
    s = str(v)
    return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"


@app.template_filter("brl")
def _fmt_brl(v, casas=2):
    if v is None:
        return "—"
    s = f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


@app.template_filter("mil")
def _fmt_mil(v):
    return "—" if v is None else f"{v:,.0f}".replace(",", ".")


# ---------------- abas de votação ----------------

@app.route("/")
def index():
    return redirect(url_for("orientacoes"))


@app.route("/orientacoes")
def orientacoes():
    f, op = _contexto_filtros()
    return render_template(
        "orientacoes.html", aba="orientacoes", f=f, op=op,
        resumo=metrics.resumo(f),
        governismo=metrics.alinhamento("Governo", f),
        oposicionismo=metrics.alinhamento("Oposição", f),
        por_partido=metrics.alinhamento_agrupado("partido", f),
        por_uf=metrics.alinhamento_agrupado("uf", f),
        votacoes=metrics.votacoes_orientadas(f, 150))


@app.route("/rankings")
def rankings():
    f, op = _contexto_filtros()
    ano_ini = f.inicio.year if f.inicio else None
    ano_fim = f.fim.year if f.fim else None

    tipo_prop = "todas" if request.args.get("tipo_prop") == "todas" else "projetos"

    def _filtra(lista):
        return [r for r in lista if (not f.partido or r.get("partido") == f.partido)
                and (not f.uf or r.get("uf") == f.uf)]

    return render_template(
        "rankings.html", aba="rankings", f=f, op=op,
        participacao=metrics.participacao(f),
        oposicionismo=metrics.alinhamento("Oposição", f, minimo=10),
        governismo=metrics.alinhamento("Governo", f, minimo=10),
        fidelidade=[r for r in metrics.fidelidade(f) if r["total"] >= 10],
        projetos=_filtra(paineis.ranking_proposicoes(f.inicio, f.fim, so_projetos=tipo_prop == "projetos")),
        tipo_prop=tipo_prop,
        economia=_filtra(paineis.ranking_economia(ano_ini, ano_fim)))


@app.route("/votacoes")
def votacoes():
    f, op = _contexto_filtros()
    busca = (request.args.get("busca") or "").strip()
    lista = metrics.lista_votacoes(f, busca)
    vid = (request.args.get("id") or "").strip() or None
    if vid is None and lista:
        vid = next((v["id"] for v in lista if not v["secreta"]), lista[0]["id"])
    det = metrics.detalhe_votacao(vid) if vid is not None else None
    return render_template("votacoes.html", aba="votacoes", f=f, op=op, busca=busca,
                           lista=lista, det=det, vid=vid)


# ---------------- abas por deputado ----------------

def _deputado_da_requisicao():
    lista = paineis.lista_deputados()
    pedido = request.args.get("deputado", type=int) or session.get("deputado")
    cod = paineis.escolher_deputado(pedido, lista)
    if cod:
        session["deputado"] = cod
    return cod, lista


def _data(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


@app.route("/perfil")
def perfil():
    cod, lista = _deputado_da_requisicao()
    _, op = _contexto_filtros()
    if not cod:
        return render_template("vazio.html", aba="perfil", op=op, lista=lista)
    s = paineis.deputado(cod)
    return render_template(
        "perfil.html", aba="perfil", op=op, lista=lista, s=s, cod=cod,
        perfil=paineis.perfil(cod), ind=paineis.indicadores_deputado(cod),
        props=paineis.proposicoes(cod, limite=0), cota=paineis.cota_deputado(cod))


@app.route("/proposicoes")
def proposicoes():
    cod, lista = _deputado_da_requisicao()
    _, op = _contexto_filtros()
    if not cod:
        return render_template("vazio.html", aba="proposicoes", op=op, lista=lista)
    a = request.args
    filtros = {"papel": a.get("papel", ""), "sigla": a.get("sigla", ""),
               "situacao": a.get("situacao", ""), "inicio": _data(a.get("inicio")),
               "fim": _data(a.get("fim")), "busca": (a.get("busca") or "").strip()}
    return render_template(
        "proposicoes.html", aba="proposicoes", op=op, lista=lista, cod=cod,
        s=paineis.deputado(cod), fp=filtros, props=paineis.proposicoes(cod, **filtros),
        todas_siglas=paineis.proposicoes(cod, limite=0)["siglas"])


@app.route("/cota")
def cota():
    cod, lista = _deputado_da_requisicao()
    _, op = _contexto_filtros()
    if not cod:
        return render_template("vazio.html", aba="cota", op=op, lista=lista)
    ano = request.args.get("ano", type=int)
    dados = paineis.cota_deputado(cod, ano, ano) if ano else paineis.cota_deputado(cod)
    todos_anos = paineis.cota_deputado(cod)["anos"]
    return render_template("cota.html", aba="cota", op=op, lista=lista, cod=cod,
                           c=dados, ano=ano, todos_anos=todos_anos)


# ---------------- Plenário (pauta do app Minha Pauta) ----------------

@app.route("/plenario", endpoint="plenario")
def plenario_pagina():
    import plenario
    _, op = _contexto_filtros()
    filtro = request.args.get("filtro", "")
    filtro = filtro if filtro in ("com_nota", "sem_nota") else ""
    evento = request.args.get("evento", type=int)
    return render_template("plenario.html", aba="plenario", op=op, filtro=filtro,
                           p=plenario.pauta(evento, filtro), recentes=plenario.pautas_recentes(),
                           configurado=plenario.configurado(), ultima_sync=plenario.ultima_sincronizacao(),
                           grupos=plenario.GRUPOS)


_plenario_lock = threading.Lock()


@app.post("/plenario/atualizar")
def plenario_atualizar():
    import plenario
    from flask import flash
    if not plenario.configurado():
        flash("Falta a variável MINHA_PAUTA_DATABASE_URL no Railway para ler o Minha Pauta.", "erro")
    elif _plenario_lock.acquire(blocking=False):
        try:
            n = plenario.sincronizar()
            flash(f"Pauta atualizada a partir do Minha Pauta ({n} sessão(ões) lidas).", "ok")
        except Exception as e:
            logging.exception("Plenário")
            flash(f"Não consegui ler o Minha Pauta agora: {str(e).splitlines()[0][:160]}", "erro")
        finally:
            _plenario_lock.release()
    else:
        flash("Já há uma atualização em andamento.", "erro")
    return redirect(url_for("plenario", evento=request.args.get("evento") or None))



@app.template_filter("html_seguro")
def _html_seguro(v):
    import plenario
    return plenario.html_seguro(v)


# ---------------- coleta ----------------
_coleta_lock = threading.Lock()


def _rodar_coleta(fn, *a):
    try:
        fn(*a)
    except Exception:
        logging.exception("falha na coleta")
    finally:
        paineis.limpar_cache()
        _coleta_lock.release()


@app.post("/api/coletar")
def api_coletar():
    token = os.environ.get("COLETA_TOKEN")
    if not token or request.args.get("token") != token:
        abort(403)
    if not _coleta_lock.acquire(blocking=False):
        return jsonify(status="já existe uma coleta em andamento"), 409
    desde = request.args.get("desde")
    import collector
    threading.Thread(target=_rodar_coleta, daemon=True,
                     args=(collector.executar, _data(desde))).start()
    return jsonify(status="coleta iniciada", desde=desde or "rotina diária"), 202


def _carga_inicial_em_segundo_plano():
    """No primeiro deploy o banco está vazio: carrega a legislatura inteira sem travar o site."""
    if not _coleta_lock.acquire(blocking=False):
        return
    import collector
    threading.Thread(target=_rodar_coleta, daemon=True,
                     args=(collector.carga_inicial_se_vazio,)).start()


def precisa_atualizar(agora=None, ultima=None):
    """Atualiza uma vez por dia, a partir das 6h de Brasília, se a última coleta tem mais de 20h."""
    from datetime import datetime, timedelta, timezone
    brt = timezone(timedelta(hours=-3))
    agora = agora or datetime.now(brt)
    if agora.hour < 6:
        return False
    if ultima is None:
        ultima = metrics.opcoes_filtro().get("ultima_coleta")
    if not ultima:
        return True
    try:
        dt = datetime.fromisoformat(str(ultima))
    except ValueError:
        return True
    if dt.tzinfo is None:          # gravada no relógio do servidor (UTC no Railway)
        dt = dt.replace(tzinfo=timezone.utc)
    return agora - dt > timedelta(hours=20)


def radar_se_preciso():
    """Radar do Meu Parlamentar a cada 2 horas, como o Monitor Legislativo (RADAR_HORAS; AUTO_RADAR=0 desliga)."""
    import radar
    if os.environ.get("AUTO_RADAR") == "0" or not radar.precisa_ciclo():
        return False
    radar.ciclo()
    return True


def plenario_se_preciso():
    """Cópia da pauta do Minha Pauta a cada 30 minutos (PLENARIO_MINUTOS)."""
    import plenario
    if not plenario.precisa_sincronizar() or not _plenario_lock.acquire(blocking=False):
        return False
    try:
        plenario.sincronizar()
    finally:
        _plenario_lock.release()
    return True


def _agendador_diario():
    """Dispensa o serviço de cron: o próprio app atualiza os dados uma vez por dia."""
    import time
    import collector
    time.sleep(120)  # deixa o boot e a carga inicial começarem primeiro
    while True:
        try:
            if precisa_atualizar() and _coleta_lock.acquire(blocking=False):
                logging.info("atualização diária iniciada")
                _rodar_coleta(collector.executar)
        except Exception:
            logging.exception("agendador diário")
        try:
            radar_se_preciso()
        except Exception:
            logging.exception("radar de notícias")
        try:
            plenario_se_preciso()
        except Exception:
            logging.exception("Plenário")
        time.sleep(30 * 60)


# Só roda no Railway (ou com AUTO_CARGA=1 local). Desligue com AUTO_CARGA=0 / AUTO_COLETA=0.
if os.environ.get("AUTO_CARGA") != "0" and (os.environ.get("RAILWAY_ENVIRONMENT")
                                             or os.environ.get("AUTO_CARGA") == "1"):
    _carga_inicial_em_segundo_plano()
    if os.environ.get("AUTO_COLETA") != "0":
        threading.Thread(target=_agendador_diario, daemon=True).start()


@app.get("/saude")
def saude():
    return jsonify(ok=True)


@app.errorhandler(403)
def _proibido(e):
    return render_template("erro.html", aba=None, codigo=403,
                           msg="Você não tem permissão para acessar esta página."), 403


@app.errorhandler(404)
def _nao_encontrado(e):
    return render_template("erro.html", aba=None, codigo=404, msg="Página não encontrada."), 404


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
