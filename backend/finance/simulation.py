"""
Générateur de scénarios économiques « monde réel » pour le PEA, entièrement
calibré sur l'historique.

Modèle (pas mensuel, 100 scénarios) :
  - chaque ligne du PEA suit un mouvement brownien géométrique
        S(t+dt) = S(t) · exp((μ - σ²/2)·dt + σ·√dt·ε) ;
  - l'inflation suit un modèle de Vasicek (retour vers une moyenne θ)
        π(t+dt) = π(t) + κ·(θ - π(t))·dt + s·√dt·ε_π ;
  - les chocs (ε des lignes et ε_π de l'inflation) sont corrélés.

Calibration, sur 10 ans d'historique mensuel au plus :
  - lignes : cours ajustés (dividendes réinvestis) Yahoo Finance, en euros ;
        σ = écart-type des rendements log × √12,
        μ = moyenne des rendements log × 12 + σ²/2 (dérive du brownien),
        β face au STOXX Europe 600 (indicatif : il ne sert pas à la dérive) ;
    une ligne à l'historique trop court reprend celui du STOXX Europe 600 ;
  - inflation : IPCH France (Eurostat, glissement annuel mensuel), régression
        π(t+1) - π(t) = a + b·π(t) + e  ->  κ = -12·b, θ = -a/b, s = σ(e)·√12 ;
  - corrélations : rendements mensuels des lignes et résidus e de l'inflation.

Les calculs (calibrate, simulate, fit_vasicek) sont purs ; seules les
fonctions du bas lisent Yahoo Finance et Eurostat (gardés 24 h en cache).
"""

import json
import logging
import math
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
from django.core.cache import cache

from . import market_data
from .models import AssetClass


logger = logging.getLogger(__name__)

SCENARIOS = 100
STEPS_PER_YEAR = 12
HISTORY_YEARS = 10
MIN_MONTHS = 24          # en dessous : la ligne reprend l'historique du marché
PEA_CEILING = 150_000
SOCIAL_CHARGES = 0.172   # prélèvements sociaux sur les gains d'un PEA de plus de 5 ans
MARKET_SYMBOL = "^STOXX"  # STOXX Europe 600, en euros
MARKET_NAME = "STOXX Europe 600"
CACHE_SECONDS = 24 * 60 * 60
HICP_URL = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr"
    "?format=JSON&lang=EN&geo=FR&coicop=CP00&sinceTimePeriod={since}"
)

# Faute de toute donnée (Yahoo et Eurostat injoignables)
FALLBACK_VOLATILITY = 0.18
FALLBACK_DRIFT = 0.06
FALLBACK_INFLATION = {"target": 0.02, "start": 0.02, "speed": 0.5, "volatility": 0.01}


@dataclass
class Assumptions:
    """Choix de l'utilisateur ; tout le reste vient de l'historique."""

    years: int = 10
    monthly_contribution: float = 0.0
    seed: int = 1


@dataclass
class AssetCalibration:
    key: str
    name: str
    value: float              # valeur actuelle en euros
    volatility: float
    drift: float
    beta: float = None
    months: int = 0           # mois d'historique propre
    source: str = "history"   # "history", "market" (historique du marché) ou "default"
    asset_class: str = AssetClass.EQUITY


@dataclass
class InflationCalibration:
    target: float
    start: float
    speed: float
    volatility: float
    months: int = 0
    first: str = ""
    last: str = ""
    estimated: bool = False


@dataclass
class Calibration:
    assets: list
    inflation: InflationCalibration
    correlation: np.ndarray    # (n + 1) × (n + 1) : les lignes puis l'inflation
    market: AssetCalibration = None
    notes: list = field(default_factory=list)


# ============================================================
# SÉRIES MENSUELLES
# ============================================================

def month_end_series(closes):
    """[(date, prix)] quotidiens -> [(année, mois, dernier prix du mois)]."""

    last = {}
    for day, close in sorted(closes):
        last[(day.year, day.month)] = float(close)
    return [(year, month, price) for (year, month), price in sorted(last.items())]


def log_returns(series):
    """{(année, mois): rendement logarithmique du mois}."""

    returns = {}
    for (y0, m0, p0), (y1, m1, p1) in zip(series, series[1:]):
        if p0 > 0 and p1 > 0:
            returns[(y1, m1)] = math.log(p1 / p0)
    return returns


def _overlap(a, b):
    keys = sorted(set(a) & set(b))
    return np.array([a[k] for k in keys]), np.array([b[k] for k in keys])


def nearest_correlation(matrix):
    """Matrice de corrélation valide (définie positive) la plus proche :
    valeurs propres négatives ramenées à un petit seuil, diagonale à 1."""

    sym = (matrix + matrix.T) / 2
    values, vectors = np.linalg.eigh(sym)
    values = np.clip(values, 1e-6, None)
    fixed = vectors @ np.diag(values) @ vectors.T
    scale = np.sqrt(np.diag(fixed))
    fixed = fixed / np.outer(scale, scale)
    np.fill_diagonal(fixed, 1.0)
    return fixed


# ============================================================
# CALIBRATION
# ============================================================

def return_statistics(returns):
    """(μ, σ) annuels d'un brownien géométrique à partir de rendements log mensuels."""

    values = np.array(list(returns.values()))
    sigma = float(np.std(values, ddof=1) * math.sqrt(STEPS_PER_YEAR))
    mu = float(values.mean() * STEPS_PER_YEAR + sigma ** 2 / 2)
    return mu, sigma


def fit_vasicek(rates):
    """
    rates : {(année, mois): inflation annuelle en décimal}, mois consécutifs.
    Renvoie (InflationCalibration, résidus {(année, mois): e}) ou (None, {}).
    """

    keys = sorted(rates)
    pairs = [(k0, k1) for k0, k1 in zip(keys, keys[1:])]
    if len(pairs) < MIN_MONTHS:
        return None, {}
    x = np.array([rates[k0] for k0, _ in pairs])
    dx = np.array([rates[k1] - rates[k0] for k0, k1 in pairs])
    design = np.column_stack([np.ones_like(x), x])
    (a, b), *_ = np.linalg.lstsq(design, dx, rcond=None)
    residuals = dx - (a + b * x)
    dt = 1.0 / STEPS_PER_YEAR

    # Sans retour à la moyenne estimable (b ≥ 0), on garde un retour lent
    # vers la moyenne observée plutôt qu'une inflation qui dérive sans fin.
    speed = -b / dt
    if speed < 0.05:
        speed = 0.05
        target = float(x.mean())
    else:
        target = -a / b
    calibration = InflationCalibration(
        target=float(min(max(target, -0.01), 0.10)),
        start=float(rates[keys[-1]]),
        speed=float(min(speed, 5.0)),
        volatility=float(np.std(residuals, ddof=2) / math.sqrt(dt)),
        months=len(keys),
        first=f"{keys[0][1]:02d}/{keys[0][0]}",
        last=f"{keys[-1][1]:02d}/{keys[-1][0]}",
        estimated=True,
    )
    return calibration, {k1: float(e) for (_, k1), e in zip(pairs, residuals)}


def calibrate(lines, histories, market_returns, inflation_rates):
    """
    lines : [{"key", "name", "value", "asset_class"}]
    histories : {key: {(année, mois): rendement log}} (vide = pas d'historique)
    market_returns : rendements log mensuels du STOXX Europe 600 (peut être vide)
    inflation_rates : {(année, mois): inflation annuelle en décimal} (peut être vide)
    """

    notes = []
    market = None
    if len(market_returns) >= MIN_MONTHS:
        mu, sigma = return_statistics(market_returns)
        market = AssetCalibration(key=MARKET_SYMBOL, name=MARKET_NAME, value=0, volatility=sigma,
                                  drift=mu, beta=1.0, months=len(market_returns))
    else:
        notes.append("market")

    assets, series = [], []
    for line in lines:
        returns = histories.get(line["key"]) or {}
        if len(returns) >= MIN_MONTHS:
            mu, sigma = return_statistics(returns)
            source = "history"
        elif market:
            # Historique trop court : la ligne se comporte comme le marché
            mu, sigma, returns, source = market.drift, market.volatility, market_returns, "market"
        else:
            mu, sigma, source = FALLBACK_DRIFT, FALLBACK_VOLATILITY, "default"

        beta = None
        if market and source == "history":
            x, m = _overlap(returns, market_returns)
            if len(x) >= MIN_MONTHS and np.var(m, ddof=1) > 0:
                beta = float(np.cov(x, m, ddof=1)[0, 1] / np.var(m, ddof=1))
        elif source == "market":
            beta = 1.0

        assets.append(AssetCalibration(
            key=line["key"], name=line["name"], value=float(line["value"]),
            volatility=max(sigma, 0.005), drift=mu, beta=beta,
            months=len(histories.get(line["key"]) or {}), source=source,
            asset_class=line.get("asset_class") or AssetClass.EQUITY,
        ))
        series.append(returns if source != "default" else {})

    inflation, residuals = fit_vasicek(inflation_rates)
    if inflation is None:
        inflation = InflationCalibration(**FALLBACK_INFLATION)
        notes.append("inflation")
    series.append(residuals)

    # Corrélations sur les mois communs ; faute de recouvrement : 0
    size = len(series)
    correlation = np.eye(size)
    for i in range(size):
        for j in range(i + 1, size):
            x, y = _overlap(series[i], series[j])
            if len(x) >= MIN_MONTHS and np.std(x) > 0 and np.std(y) > 0:
                correlation[i, j] = correlation[j, i] = float(np.corrcoef(x, y)[0, 1])
    correlation = nearest_correlation(correlation)

    return Calibration(assets=assets, inflation=inflation, correlation=correlation, market=market, notes=notes)


# ============================================================
# SIMULATION
# ============================================================

def simulate(calibration, assumptions, cash=0.0, invested=0.0, deposits=0.0, ceiling=PEA_CEILING):
    """
    Trajectoires mensuelles du PEA. Renvoie des tableaux numpy :
      nominal, real : (SCENARIOS, mois + 1) valeur totale du PEA
      invested      : (mois + 1,) capital investi (prix de revient + versements)
      inflation     : (SCENARIOS, mois + 1) taux d'inflation annuel
    """

    rng = np.random.default_rng(assumptions.seed)
    steps = int(assumptions.years * STEPS_PER_YEAR)
    dt = 1.0 / STEPS_PER_YEAR
    assets = calibration.assets
    infl = calibration.inflation
    n = len(assets)

    values0 = np.array([a.value for a in assets], dtype=float)
    total0 = values0.sum()
    weights = values0 / total0 if total0 > 0 else np.full(n, 1.0 / max(n, 1))
    sigma = np.array([a.volatility for a in assets])
    mu = np.array([a.drift for a in assets])
    chol = np.linalg.cholesky(calibration.correlation)

    holdings = np.tile(values0, (SCENARIOS, 1))
    nominal = np.empty((SCENARIOS, steps + 1))
    real = np.empty((SCENARIOS, steps + 1))
    inflation = np.empty((SCENARIOS, steps + 1))
    invested_path = np.empty(steps + 1)

    pi = np.full(SCENARIOS, infl.start)
    deflator = np.ones(SCENARIOS)
    nominal[:, 0] = holdings.sum(axis=1) + cash
    real[:, 0] = nominal[:, 0]
    inflation[:, 0] = pi
    invested_path[0] = invested + cash
    paid_in = deposits

    drift_term = (mu - sigma ** 2 / 2) * dt
    for t in range(1, steps + 1):
        # Versement en début de mois, dans la limite du plafond
        contribution = 0.0
        if assumptions.monthly_contribution > 0 and paid_in < ceiling:
            contribution = min(assumptions.monthly_contribution, ceiling - paid_in)
            paid_in += contribution
            holdings += contribution * weights

        shocks = rng.standard_normal((SCENARIOS, n + 1)) @ chol.T
        holdings *= np.exp(drift_term + sigma * math.sqrt(dt) * shocks[:, :n])

        pi = pi + infl.speed * (infl.target - pi) * dt + infl.volatility * math.sqrt(dt) * shocks[:, n]
        deflator *= np.exp(-pi * dt)

        nominal[:, t] = holdings.sum(axis=1) + cash
        real[:, t] = nominal[:, t] * deflator
        inflation[:, t] = pi
        invested_path[t] = invested_path[t - 1] + contribution

    return {"nominal": nominal, "real": real, "invested": invested_path, "inflation": inflation}


def max_drawdown(paths):
    """Plus forte baisse depuis un sommet, par scénario (fraction positive)."""

    peaks = np.maximum.accumulate(paths, axis=1)
    return ((peaks - paths) / np.where(peaks > 0, peaks, 1)).max(axis=1)


def summarize(result, assumptions, pea_age_years=None):
    """Indicateurs à l'horizon, sans et avec inflation."""

    invested = float(result["invested"][-1])
    years = assumptions.years
    out = {"invested": invested}
    for kind in ("nominal", "real"):
        final = result[kind][:, -1]
        start = result[kind][:, 0]
        median = float(np.median(final))
        gains = np.maximum(final - invested, 0)
        out[kind] = {
            "median": median,
            "mean": float(final.mean()),
            "p5": float(np.percentile(final, 5)),
            "p25": float(np.percentile(final, 25)),
            "p75": float(np.percentile(final, 75)),
            "p95": float(np.percentile(final, 95)),
            "loss_probability": float((final < invested).mean() * 100),
            # Rendement annualisé médian (sans versement uniquement : sinon il mélange apports et gains)
            "annualized": float(((median / start.mean()) ** (1 / years) - 1) * 100)
            if start.mean() > 0 and not assumptions.monthly_contribution else None,
            "drawdown": float(np.median(max_drawdown(result[kind])) * 100),
            "net_median": float(np.median(final - gains * SOCIAL_CHARGES)),
        }
    out["inflation_median"] = float(np.median(result["inflation"][:, 1:].mean(axis=1)) * 100)
    if pea_age_years is not None:
        out["pea_mature"] = pea_age_years + years >= 5
    return out


def chart_data(result, today=None):
    """Données du graphique : percentiles et 100 trajectoires, un point par mois."""

    today = today or date.today()
    steps = result["nominal"].shape[1]
    labels = []
    year, month = today.year, today.month
    for _step in range(steps):
        labels.append(f"{year}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1

    def pack(paths):
        rounded = lambda row: [round(float(v)) for v in row]
        return {
            "paths": [rounded(row) for row in paths],
            "p5": rounded(np.percentile(paths, 5, axis=0)),
            "p25": rounded(np.percentile(paths, 25, axis=0)),
            "p50": rounded(np.percentile(paths, 50, axis=0)),
            "p75": rounded(np.percentile(paths, 75, axis=0)),
            "p95": rounded(np.percentile(paths, 95, axis=0)),
        }

    inflation = result["inflation"] * 100
    return {
        "labels": labels,
        "invested": [round(float(v)) for v in result["invested"]],
        "nominal": pack(result["nominal"]),
        "real": pack(result["real"]),
        # Taux d'inflation annuel (%) : éventail des scénarios
        "inflation": {
            key: [round(float(v), 2) for v in np.percentile(inflation, q, axis=0)]
            for key, q in (("p5", 5), ("p25", 25), ("p50", 50), ("p75", 75), ("p95", 95))
        },
    }


# ============================================================
# DONNÉES DE MARCHÉ (Yahoo Finance, Eurostat)
# ============================================================

def _history_start(today):
    return today.replace(day=1) - timedelta(days=365 * HISTORY_YEARS + 31)


def _cached_history(symbol, start):
    key = f"bw:sim:adjusted:{symbol}:{start.isoformat()}"
    data = cache.get(key)
    if data is None:
        data = market_data.fetch_history(symbol, start, adjusted=True)
        cache.set(key, data, CACHE_SECONDS)
    return data


def monthly_eur_prices(symbol, today=None):
    """Cours ajustés de fin de mois en euros sur HISTORY_YEARS ans, ou None si Yahoo échoue."""

    today = today or date.today()
    start = _history_start(today)
    try:
        data = _cached_history(symbol, start)
        series = month_end_series(data["closes"])
        currency, factor = market_data.base_currency(data["currency"])
        if currency != "EUR":
            fx = {(y, m): p for y, m, p in month_end_series(_cached_history(market_data.fx_symbol(currency), start)["closes"])}
            series = [(y, m, p * float(factor) * fx[(y, m)]) for y, m, p in series if (y, m) in fx]
        elif factor != 1:
            series = [(y, m, p * float(factor)) for y, m, p in series]
        # Le mois en cours n'est pas terminé
        return [(y, m, p) for y, m, p in series if (y, m) != (today.year, today.month)]
    except (market_data.MarketDataError, KeyError) as error:
        logger.info("Historique indisponible pour la simulation (%s) : %s", symbol, error)
        return None


def parse_hicp(payload):
    """Réponse JSON-stat d'Eurostat -> {(année, mois): inflation annuelle en décimal}."""

    index = payload["dimension"]["time"]["category"]["index"]
    values = payload.get("value", {})
    rates = {}
    for period, position in index.items():
        value = values.get(str(position))
        if value is not None:
            year, month = period.split("-")
            rates[(int(year), int(month))] = float(value) / 100
    return rates


def inflation_history(today=None):
    """Inflation française mensuelle (IPCH, glissement annuel) sur 10 ans, {} si Eurostat échoue."""

    today = today or date.today()
    start = _history_start(today)
    key = f"bw:sim:hicp:{start.isoformat()}"
    rates = cache.get(key)
    if rates is None:
        try:
            url = HICP_URL.format(since=f"{start.year}-{start.month:02d}")
            with urllib.request.urlopen(url, timeout=15) as response:
                rates = parse_hicp(json.load(response))
        except Exception as error:
            logger.info("Inflation Eurostat indisponible : %s", error)
            return {}
        cache.set(key, rates, CACHE_SECONDS)
    return rates


def pea_lines(analysis):
    """Lignes du PEA à simuler, d'après analyse_portfolio."""

    lines = []
    for row in analysis["positions"]:
        if row["value"] <= 0:
            continue
        instrument = row["instrument"]
        lines.append({
            "key": instrument.symbol,
            "name": instrument.name,
            "value": float(row["value"]),
            "cost": float(row["cost"]),
            "asset_class": instrument.asset_class,
        })
    return lines


def calibrate_from_market(lines, today=None):
    histories = {}
    for line in lines:
        series = monthly_eur_prices(line["key"], today)
        histories[line["key"]] = log_returns(series) if series else {}
    market = monthly_eur_prices(MARKET_SYMBOL, today)
    market_returns = log_returns(market) if market else {}
    return calibrate(lines, histories, market_returns, inflation_history(today))
