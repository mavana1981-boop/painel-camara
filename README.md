# Painel da Câmara — Liderança da Oposição

App Flask (com login) gêmeo do **Painel do Senado**, para a Câmara dos Deputados: **Perfil, Proposições, Cota,
Votações, Orientações e Rankings**, com identidade visual no verde da Câmara.

## Deploy no Railway

1. Suba esta pasta para um repositório no GitHub (privado). Pode subir com as pastas `templates/`, `static/` e
   `tests/` ou tudo solto na raiz — o app encontra os arquivos nos dois jeitos.
2. No Railway: **New Project → Deploy from GitHub repo**.
3. No mesmo projeto: **+ New → Database → PostgreSQL**.
4. No serviço do app, em **Variables**, crie:

| Variável | Valor |
|---|---|
| `DATABASE_URL` | `${{ Postgres.DATABASE_URL }}` |
| `SECRET_KEY` | uma sequência longa e aleatória (assina as sessões de login) |
| `ADMIN_USER` | seu usuário de administrador, ex.: `marcelo` |
| `ADMIN_PASSWORD` | senha forte (mín. 8 caracteres) |
| `ADMIN_NOME` | opcional, nome exibido no topo |
| `COLETA_TOKEN` | opcional, senha para disparar coleta manual pela URL |

5. Faça o deploy. No primeiro boot:
   - o administrador é criado a partir de `ADMIN_USER`/`ADMIN_PASSWORD`;
   - o app detecta o banco vazio e baixa **toda a legislatura em segundo plano**: votações nominais do Plenário
     desde fev/2023, CEAP 2023→hoje, proposições e perfis de todos os deputados. Leva de 20 a 40 minutos (são
     ~600 deputados); o site já abre e vai se preenchendo.

Depois do primeiro acesso, troque sua senha em **Conta** e crie os usuários da equipe em **Usuários**
(perfil “Leitura” ou “Admin”). `ADMIN_PASSWORD` só é usada para criar o admin na primeira vez.

### Atualização diária (automática)
O próprio app atualiza os dados uma vez por dia, a partir das 6h de Brasília — não precisa de serviço de cron.
A Câmara republica os arquivos do ano todo dia, então o app recarrega o **ano corrente inteiro** de votações,
as **proposições de todos os anos da legislatura** (os arquivos trazem a situação atual de cada uma — é assim que
o monitor de situação percebe as mudanças), a CEAP do ano (e a do ano anterior até março, quando ainda chegam
lançamentos atrasados), os perfis e o Meu Parlamentar (agenda, pautas e notícias). Se o servidor da Câmara cair e um ano falhar, ele fica anotado e é refeito na coleta seguinte.
Para desligar: `AUTO_COLETA=0`.
Coleta manual: `POST https://SEU-APP.up.railway.app/api/coletar?token=SEU_COLETA_TOKEN`
(com `&desde=2023-02-01` faz a recarga completa).

## Fontes de dados (todas públicas, sem chave)
| Aba | Fonte |
|---|---|
| Votações, Orientações, Rankings | arquivos anuais `votacoes`, `votacoesVotos`, `votacoesOrientacoes`, `votacoesProposicoes` em `dadosabertos.camara.leg.br/arquivos/{nome}/csv/{nome}-{ano}.csv` |
| Deputados (foto, partido, UF) | API v2 `/deputados?idLegislatura=57` e `/deputados` (em exercício) |
| Perfil | `/deputados/{id}`, `/deputados/{id}/orgaos`, `/deputados/{id}/frentes` |
| Proposições | arquivos anuais `proposicoes` e `proposicoesAutores` |
| Cota (CEAP) | `www.camara.leg.br/cotas/Ano-{ano}.csv.zip` (plano B: `/deputados/{id}/despesas`) |

Por que arquivos em lote e não a API? São poucos downloads por ano em vez de centenas de milhares de chamadas,
e a API `/votacoes/{id}/votos` deixou de devolver votos individuais de votações recentes — os arquivos trazem.

## Regras de cálculo (iguais às do painel do Senado)
- **Governismo/Oposicionismo**: votos SIM/NÃO iguais à orientação ÷ votos SIM/NÃO do deputado quando a liderança
  orientou SIM ou NÃO. “Liberado”, abstenção, obstrução e Art. 17 (presidente da sessão) ficam fora. Filtro
  **só orientações opostas** (padrão) usa apenas votações em que Governo e Oposição orientaram em sentidos contrários.
- **Só votações nominais do Plenário** (`siglaOrgao = PLEN`). Simbólicas e de comissão ficam de fora
  (mude com `ORGAO_VOTACOES`).
- **Por partido / por estado / aba Votações**: partido **na data do voto**. Listas de deputados: partido **atual**.
- **Participação**: votações em que registrou voto (inclui obstrução e Art. 17) ÷ votações realizadas entre o
  primeiro e o último voto do deputado no período.
- **Fidelidade**: votos iguais à orientação que valeu para o partido — a do próprio partido; se ele não orientou,
  a da federação (ex.: `Fdr PT-PCdoB-PV`); senão, a do bloco. Blocos que a Câmara publica com nome truncado
  (`Bl UniPpPsd...`) têm a composição completada por dedução a partir das próprias votações.
- **Proposições**: autoria e coautoria (apoiadores não contam); “1º autor” = primeiro signatário.
  “Virou norma” = situação “Transformado em Norma Jurídica”. Emendas, substitutivos, pareceres e redações ficam
  de fora (lista em `TIPOS_EXCLUIDOS`).
- **Cota**: teto mensal da CEAP por UF (valores em vigor desde fev/2023, de R$ 36.582,46 no DF a R$ 51.406,33 em
  RR — `cota.py`). Cota disponível = teto × meses no cargo no ano (estimados pela atividade registrada).
  Gasto = valor líquido (já descontadas glosas e restituições). Jan/2023 pertence à legislatura anterior e fica fora.
  **Reajuste de 2026**: a Mesa corrigiu a cota em 20/02/2026 pelo IPCA (≈13,75%), sem publicar tabela nem data
  de vigência; o app aplica a partir de 01/03/2026. Para ajustar: `CEAP_REAJUSTES=2026-02-01:0.1375`
  (data:percentual; substitui o padrão).

## Segurança
Senhas com hash scrypt; sessão assinada (cookie HttpOnly, Secure no Railway); proteção CSRF em todos os formulários;
bloqueio de 15 min após 5 tentativas de login erradas; desativar um usuário ou trocar a senha derruba as sessões dele.

## Rodar localmente
```bash
pip install -r requirements.txt
export ADMIN_USER=admin ADMIN_PASSWORD=troque-esta-senha SECRET_KEY=dev
python collector.py --desde 2023-02-01   # cria camara.db (SQLite) com tudo
flask --app app run
```

## Testes
```bash
python tests/test_pipeline.py                                   # SQLite
TEST_DATABASE_URL=postgresql://... python tests/test_pipeline.py   # PostgreSQL
```
Cobrem: leitura dos CSVs no layout da Câmara (BOM, `;`, quebras de linha em campos), filtro de votações
nominais do Plenário, voto duplicado, Art. 17, proposição principal da votação, orientação de partido/federação/
bloco (inclusive bloco truncado deduzido), os cálculos das seis abas, tetos da CEAP, ZIP da CEAP, autoria, perfil,
login, CSRF, permissões, gestão de usuários e retomada de anos que falharam. As fixtures são fictícias.

## Estrutura
```
app.py        rotas
auth.py       login, sessões, usuários
collector.py  coleta (votações, CEAP, proposições, perfis)
metrics.py    cálculos de votação
paineis.py    Perfil, Proposições, Cota, notificações e rankings correlatos
meu.py        Meu Parlamentar (rotas e regras)
coleta_meu.py agenda, pautas/relatorias e radar de notícias
questoes.py   questões de ordem por IA com base no RICD
plenario.py   aba Plenário (pauta e notas do app Minha Pauta)
radar.py      radar de notícias (portais do Monitor Legislativo + Google Notícias, análise por IA)
cota.py       tetos da CEAP por UF
db.py         tabelas e normalização
templates/ static/ tests/
```

## Meu Parlamentar

Área por deputado, aberta só a quem o administrador vincular (em **Usuários → Meu parlamentar**).
O administrador vê todos os deputados em exercício.

| Seção | De onde vem |
|---|---|
| Agenda | Eventos dos órgãos do deputado e sessões do Plenário (API `/eventos`, 7 dias atrás e 45 à frente) + compromissos lançados pela equipe. “Deputado na pauta” = relator de item da pauta |
| Notificações | Mudanças de situação das proposições e proposições novas, detectadas na coleta diária (guardadas por 180 dias) |
| Solicitações | Registro com status, prazo e anexos (até 10 MB cada, guardados no banco) |
| Questões de ordem | IA (Claude, API da Anthropic) com base no Regimento Interno da Câmara enviado pelo admin (PDF/TXT) |
| Radar de notícias | No modelo do Monitor Legislativo: os mesmos portais (RSS, editáveis pelo admin) a cada 2 horas, mais o Google Notícias; entram as notícias que citam o deputado ou um tema cadastrado, e a IA indica relevância, categoria, tipo de ação e ação sugerida. A equipe marca Pendente / Em análise / Ação efetuada |
| Rankings | Posição entre os deputados em exercício e quanto falta para subir |
| Clipping | Proposições do deputado com situação de aprovação e relatorias com parecer aprovado, por data |

A Câmara não tem serviço de “relatorias do deputado”: elas são montadas a partir das pautas das reuniões
deliberativas (`/eventos/{id}/pauta`, que traz o relator de cada item e o resultado). Na primeira coleta, o app
lê as pautas dos últimos 180 dias (`MEU_DIAS_HISTORICO`) e depois acompanha dia a dia.
O radar roda a cada 2 horas (`RADAR_HORAS`; `AUTO_RADAR=0` desliga) só para deputados com usuário vinculado; o botão
**Atualizar agora** busca na hora. A IA do radar e das questões de ordem usa `ANTHROPIC_API_KEY`; o modelo do radar
fica em `RADAR_MODEL` (padrão `claude-haiku-5-5`) e o limite de análises por deputado e ciclo em `RADAR_MAX_ANALISES` (30).

## Plenário (pauta do Minha Pauta)

A aba **Plenário** mostra a pauta mais recente preparada no app Minha Pauta (repositório `plenario-assessoria`),
com as notas da equipe: resumo da matéria (nota técnica), parecer, orientação, quem salvou e quando, as
orientações por grupo (PL, NOVO, Oposição, Minoria), o resumo da ementa feito pela IA e os itens de extrapauta.
Dá para trocar entre as últimas sessões e filtrar itens com e sem nota.

O painel lê o banco do Minha Pauta em modo somente leitura e guarda uma cópia das últimas pautas, renovada a cada
30 minutos (`PLENARIO_MINUTOS`) e pelo botão **Atualizar agora**. Para ligar, crie no serviço do painel a variável
`MINHA_PAUTA_DATABASE_URL` com o endereço do PostgreSQL do Minha Pauta (no mesmo projeto do Railway:
`${{ NomeDoPostgresDoMinhaPauta.DATABASE_URL }}`; em outro projeto: a `DATABASE_PUBLIC_URL` dele).
Notas, orientações e ordem continuam sendo editadas no Minha Pauta.
