"""Banco de dados: PostgreSQL no Railway (DATABASE_URL) ou SQLite local."""
import os
import re
import unicodedata

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, Integer, LargeBinary, MetaData,
                        String, Table, Text, create_engine)


def _db_url():
    url = os.environ.get("DATABASE_URL", "sqlite:///camara.db")
    # Railway entrega "postgres://"; o SQLAlchemy 2 + psycopg 3 exige o dialeto explícito
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(_db_url(), pool_pre_ping=True, future=True)
metadata = MetaData()

deputados = Table(
    "deputados", metadata,
    Column("codigo", Integer, primary_key=True),      # id da API de Dados Abertos
    Column("nome", String(120), nullable=False),
    Column("nome_norm", String(120), index=True),
    Column("nome_completo", String(200)),
    Column("partido", String(40)),
    Column("uf", String(2)),
    Column("bloco", String(120)),
    Column("foto", String(255)),
    Column("email", String(120)),
    Column("em_exercicio", Boolean, default=False),
)

votacoes = Table(
    "votacoes", metadata,
    Column("id", String(30), primary_key=True),       # ex.: "2265603-43"
    Column("ano", Integer, index=True),               # ano do arquivo de origem (para recarga anual)
    Column("data", DateTime, index=True),
    Column("sigla_materia", String(20), index=True),
    Column("numero_materia", Integer),
    Column("ano_materia", Integer),
    Column("proposicao_id", BigInteger),
    Column("descricao_materia", Text),                # ementa da proposição
    Column("descricao_votacao", Text),                # o que foi votado ("Aprovado o Requerimento...")
    Column("dispositivo", Text),
    Column("sessao", String(200)),
    Column("presidente", String(120)),
    Column("qtd_sim", Integer),
    Column("qtd_nao", Integer),
    Column("qtd_abstencao", Integer),
    Column("qtd_outros", Integer),
    Column("aprovacao", Boolean),
    Column("secreta", Boolean, default=False),
)

votos = Table(
    "votos", metadata,
    Column("votacao_id", String(30), primary_key=True),
    Column("codigo_deputado", Integer, primary_key=True, index=True),
    Column("nome", String(120)),
    Column("partido", String(40)),                    # partido na data do voto
    Column("uf", String(2)),
    Column("voto", String(30)),
)

orientacoes = Table(  # como vêm no arquivo: Governo, Oposição, Maioria, Minoria, partidos, federações, blocos
    "orientacoes", metadata,
    Column("votacao_id", String(30), primary_key=True),
    Column("lideranca", String(80), primary_key=True),
    Column("voto", String(30)),
)

orientacoes_partido = Table(  # orientação que valeu para cada partido (própria, da federação ou do bloco)
    "orientacoes_partido", metadata,
    Column("votacao_id", String(30), primary_key=True),
    Column("partido", String(40), primary_key=True),
    Column("voto", String(30)),
    Column("fonte", String(80)),                      # rótulo da bancada que orientou
)

despesas = Table(  # CEAP
    "despesas", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ano", Integer, index=True),
    Column("mes", Integer),
    Column("data", String(10)),
    Column("codigo_deputado", Integer, index=True),
    Column("nome", String(120)),
    Column("tipo", String(300)),
    Column("tipo_documento", String(80)),
    Column("fornecedor", String(300)),
    Column("cnpj_cpf", String(30)),
    Column("documento", String(80)),
    Column("detalhamento", Text),
    Column("valor", Float),
    Column("url", String(300)),
)

processos = Table(  # proposições
    "processos", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("ano_carga", Integer, index=True),         # ano do arquivo de origem (ano de apresentação)
    Column("identificacao", String(60)),
    Column("sigla", String(20), index=True),
    Column("numero", String(20)),
    Column("ano", Integer, index=True),
    Column("ementa", Text),
    Column("autoria", Text),
    Column("tipo_documento", String(120)),
    Column("tipo_conteudo", String(160)),             # forma de apreciação
    Column("situacao", String(200)),
    Column("data_apresentacao", String(10), index=True),
    Column("data_situacao", String(10)),
    Column("tramitando", Boolean),
    Column("virou_norma", Boolean, default=False),
    Column("url", String(300)),
)

autorias = Table(
    "autorias", metadata,
    Column("processo_id", BigInteger, primary_key=True),
    Column("codigo_deputado", Integer, primary_key=True, index=True),
    Column("principal", Boolean, default=False),
)

notificacoes = Table(  # mudanças de situação das proposições, detectadas na coleta diária
    "notificacoes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("codigo_deputado", Integer, index=True),
    Column("processo_id", BigInteger, index=True),
    Column("identificacao", String(60)),
    Column("tipo", String(20)),                 # "situacao" (mudou) ou "nova" (proposição nova)
    Column("situacao_anterior", String(200)),
    Column("situacao_nova", String(200)),
    Column("data_situacao", String(10)),        # data da situação nova, segundo a Câmara
    Column("detectado_em", DateTime, index=True),
)

perfis = Table(  # detalhe, órgãos e frentes de cada deputado (JSON da API)
    "perfis", metadata,
    Column("codigo", Integer, primary_key=True),
    Column("detalhe", Text),
    Column("orgaos", Text),
    Column("frentes", Text),
    Column("atualizado", DateTime),
)

usuarios = Table(
    "usuarios", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("login", String(60), unique=True, nullable=False),
    Column("nome", String(120)),
    Column("senha_hash", String(300), nullable=False),
    Column("admin", Boolean, default=False),
    Column("ativo", Boolean, default=True),
    Column("criado", DateTime),
    Column("ultimo_acesso", DateTime),
)

# ---------------- Meu Parlamentar ----------------

usuario_deputados = Table(  # quem pode abrir o "Meu Parlamentar" de qual deputado (admin vê todos)
    "usuario_deputados", metadata,
    Column("usuario_id", Integer, primary_key=True),
    Column("codigo_deputado", Integer, primary_key=True, index=True),
)

agenda = Table(  # agenda pública da Câmara (codigo_deputado nulo) e compromissos lançados pela equipe
    "agenda", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("origem", String(10)),                # "camara" ou "equipe"
    Column("chave", String(80), index=True),     # identificador do evento na API (evita duplicar)
    Column("codigo_deputado", Integer, index=True),
    Column("data", String(10), index=True),      # AAAA-MM-DD
    Column("hora", String(5)),
    Column("titulo", String(300)),
    Column("colegiado", String(30)),             # sigla do órgão, "PLEN" para o Plenário
    Column("local", String(200)),
    Column("situacao", String(80)),
    Column("descricao", Text),                   # pauta (texto) ou observação da equipe
    Column("relatores", Text),                   # ",101,204," — relatores de itens da pauta
    Column("url", String(300)),
    Column("criado_por", String(60)),
    Column("criado_em", DateTime),
)

solicitacoes = Table(
    "solicitacoes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("codigo_deputado", Integer, index=True),
    Column("titulo", String(200)),
    Column("descricao", Text),
    Column("solicitante", String(120)),          # quem pediu (o deputado, chefe de gabinete...)
    Column("status", String(20)),                # aberta, andamento, concluida, arquivada
    Column("prazo", String(10)),
    Column("criado_por", String(60)),
    Column("criado_em", DateTime),
    Column("atualizado_em", DateTime),
)

anexos = Table(
    "anexos", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("solicitacao_id", Integer, index=True),
    Column("nome", String(200)),
    Column("mime", String(100)),
    Column("tamanho", Integer),
    Column("conteudo", LargeBinary),
    Column("enviado_por", String(60)),
    Column("enviado_em", DateTime),
)

questoes_ordem = Table(
    "questoes_ordem", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("codigo_deputado", Integer, index=True),
    Column("autor", String(60)),
    Column("situacao", Text),
    Column("resposta", Text),
    Column("modelo", String(60)),
    Column("criado_em", DateTime),
)

documentos = Table(  # textos de referência (ex.: Regimento Interno), enviados pelo admin
    "documentos", metadata,
    Column("chave", String(40), primary_key=True),
    Column("nome", String(200)),
    Column("texto", Text),
    Column("atualizado", DateTime),
)

temas_radar = Table(
    "temas_radar", metadata,
    Column("codigo_deputado", Integer, primary_key=True),
    Column("termo", String(120), primary_key=True),
)

noticias = Table(
    "noticias", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("codigo_deputado", Integer, index=True),
    Column("termo", String(120)),
    Column("titulo", String(500)),
    Column("link", String(1000)),
    Column("fonte", String(200)),
    Column("publicado", DateTime, index=True),
    Column("coletado_em", DateTime),
)

relatorias = Table(  # montadas a partir das pautas das reuniões (o relator de cada item)
    "relatorias", metadata,
    Column("codigo_deputado", Integer, primary_key=True),
    Column("chave", String(120), primary_key=True),   # proposição + colegiado
    Column("identificacao", String(60)),
    Column("codigo_materia", BigInteger),             # id da proposição na API
    Column("ementa", Text),
    Column("colegiado", String(30)),
    Column("data_designacao", String(10)),            # primeira pauta em que apareceu como relator
    Column("data_destituicao", String(10)),
    Column("situacao", String(200)),                  # resultado do item na pauta mais recente
    Column("data_situacao", String(10)),
    Column("url", String(300)),
    Column("atualizado", DateTime),
)

meta = Table(
    "meta", metadata,
    Column("chave", String(60), primary_key=True),
    Column("valor", Text),
)


def init_db():
    metadata.create_all(engine)


# ---------- Normalização ----------

def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def norm_nome(nome: str) -> str:
    """'Zé Trovão ' -> 'ze trovao'; remove acentos e pontuação."""
    if not nome:
        return ""
    s = re.sub(r"[^a-zA-Z ]", " ", sem_acento(nome)).lower()
    return re.sub(r"\s+", " ", s).strip()


# Grafias diferentes do mesmo partido (cadastro, votos e rótulos de orientação, que vêm truncados)
_PARTIDOS = {
    "REPUBLICA": "REPUBLICANOS", "REPUBLICAN": "REPUBLICANOS", "REPUBLICANOS": "REPUBLICANOS",
    "PODEMOS": "PODE", "PODE": "PODE",
    "UNIAO": "UNIÃO", "UNIÃO": "UNIÃO", "UNIAO BRASIL": "UNIÃO",
    "SOLIDARIED": "SOLIDARIEDADE", "SOLIDARIEDADE": "SOLIDARIEDADE", "SD": "SOLIDARIEDADE",
    "PCDOB": "PCdoB", "CIDADANIA": "CIDADANIA", "CIDADANI": "CIDADANIA",
    "MISSAO": "MISSÃO", "MISSÃO": "MISSÃO", "PATRIOTA": "PATRIOTA",
    "S.PART.": "S/PARTIDO", "S/PARTIDO": "S/PARTIDO", "SEM PARTIDO": "S/PARTIDO",
}


def norm_partido(p: str) -> str:
    if not p:
        return ""
    chave = p.strip().upper()
    return _PARTIDOS.get(chave) or _PARTIDOS.get(sem_acento(chave)) or chave


# Lideranças institucionais (o arquivo às vezes traz "GOVERNO", "Oposicao"...)
_INSTITUCIONAIS = {"GOVERNO": "Governo", "OPOSICAO": "Oposição", "MAIORIA": "Maioria",
                   "MINORIA": "Minoria"}
INSTITUCIONAIS = tuple(_INSTITUCIONAIS.values())


def norm_lideranca(rotulo: str) -> str:
    r = (rotulo or "").strip()
    return _INSTITUCIONAIS.get(sem_acento(r).upper(), r)


def norm_voto(v: str) -> str:
    """Padroniza: SIM, NÃO, ABSTENÇÃO, OBSTRUÇÃO, LIVRE, ART. 17; demais códigos ficam como vêm."""
    if not v:
        return ""
    s = v.strip().upper()
    s_sem = sem_acento(s)
    mapa = {"SIM": "SIM", "NAO": "NÃO", "ABSTENCAO": "ABSTENÇÃO",
            "OBSTRUCAO": "OBSTRUÇÃO", "LIVRE": "LIVRE", "LIBERADO": "LIVRE",
            "ARTIGO 17": "ART. 17", "ART. 17": "ART. 17", "ART 17": "ART. 17",
            "SECRETO": "SECRETO"}
    return mapa.get(s_sem, s)


# Votos que contam como participação (o deputado registrou presença na votação).
# "Art. 17" = presidente da sessão, que registra presença mas não vota.
VOTOS_PARTICIPACAO = ("SIM", "NÃO", "ABSTENÇÃO", "OBSTRUÇÃO", "ART. 17", "SECRETO")
# Votos e orientações que permitem medir alinhamento
POSICOES = ("SIM", "NÃO")
