"""Teto mensal da CEAP (Cota para o Exercício da Atividade Parlamentar) da Câmara, por UF.

Valores em vigor a partir de 01/02/2023 (início da 57ª legislatura), quando a cota subiu
R$ 5.793,80 em todas as UFs (DF: R$ 30.788,66 -> R$ 36.582,46; RR: R$ 45.612,53 -> R$ 51.406,33).
Em 20/02/2026 a Mesa Diretora corrigiu a cota pelo IPCA acumulado de fev/2023 a dez/2025
(cerca de 13,75%; RR passa a ~R$ 58,4 mil). O ato não divulgou a tabela nem a data exata de vigência:
aqui o reajuste entra a partir de 01/03/2026.

Para corrigir ou acrescentar reajustes sem mexer no código, defina no Railway
CEAP_REAJUSTES="2026-03-01:0.1375" (data:percentual, separados por vírgula) — quando definida,
ela SUBSTITUI a lista abaixo.
"""
import os
from datetime import date

BASE_2023 = {
    "AC": 50426.26, "AL": 46737.90, "AM": 49363.92, "AP": 49168.58, "BA": 44804.65,
    "CE": 48245.57, "DF": 36582.46, "ES": 43217.71, "GO": 41300.86, "MA": 47945.49,
    "MG": 41886.51, "MS": 46336.64, "MT": 45221.83, "PA": 48021.25, "PB": 47826.36,
    "PE": 47470.60, "PI": 46765.57, "PR": 44665.66, "RJ": 41553.77, "RN": 48525.79,
    "RO": 49466.29, "RR": 51406.33, "RS": 46669.70, "SC": 45671.58, "SE": 45933.06,
    "SP": 42837.33, "TO": 45297.41,
}
INICIO_BASE = date(2023, 2, 1)
AUMENTO_2023 = 5793.80  # acréscimo linear de fev/2023 (antes disso: base - aumento)

REAJUSTES = [(date(2026, 3, 1), 0.1375)]
_env = os.environ.get("CEAP_REAJUSTES", "").strip()
if _env:
    REAJUSTES = []
    for item in filter(None, _env.split(",")):
        d, _, pct = item.partition(":")
        try:
            REAJUSTES.append((date.fromisoformat(d.strip()), float(pct)))
        except ValueError:
            pass
REAJUSTES.sort()


def teto_mensal(uf: str, ano: int, mes: int) -> float | None:
    """Valor da cota de um mês para a UF."""
    base = BASE_2023.get((uf or "").upper())
    if base is None:
        return None
    ref = date(ano, mes, 1)
    if ref < INICIO_BASE:
        return round(base - AUMENTO_2023, 2)
    v = base
    for quando, pct in REAJUSTES:
        if ref >= quando:
            v *= 1 + pct
    return round(v, 2)


def teto_periodo(uf: str, ano: int, meses) -> float:
    return round(sum(teto_mensal(uf, ano, m) or 0 for m in meses), 2)
