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
A Câmara republica os arquivos do ano todo dia, então o app recarrega o **ano corrente inteiro** (votações,
proposições), a CEAP do ano (e a do ano anterior até março, quando ainda chegam lançamentos atrasados) e os
perfis. Se o servidor da Câmara cair e um ano falhar, ele fica anotado e é refeito na coleta seguinte.
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
paineis.py    Perfil, Proposições, Cota e rankings correlatos
cota.py       tetos da CEAP por UF
db.py         tabelas e normalização
templates/ static/ tests/
```
