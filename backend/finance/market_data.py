"""
Cours de bourse via Yahoo Finance (bibliothèque yfinance).

  - recherche d'un titre par ISIN, nom ou ticker ;
  - historique des cours de clôture, stocké en base (PricePoint) pour tracer
    l'évolution du portefeuille sans réinterroger Yahoo à chaque page ;
  - taux de change (ExchangeRate) pour les titres cotés hors euro.

Les appels réseau lèvent MarketDataError en cas de problème (réseau, ticker
inconnu) : l'application reste utilisable avec les derniers cours connus.
"""

import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Min
from django.utils import timezone

from .models import ExchangeRate, Instrument, PortfolioOperation, PricePoint
from django.utils.translation import gettext as _


logger = logging.getLogger(__name__)

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# Types proposés à la recherche (pas les futures, options, indices, devises)
SEARCHABLE_TYPES = {"ETF", "EQUITY", "MUTUALFUND", "CRYPTOCURRENCY"}

# Cotations en centimes (Londres) : devise principale et facteur
MINOR_CURRENCIES = {
    "GBp": ("GBP", Decimal("0.01")),
    "GBX": ("GBP", Decimal("0.01")),
}

# Les cours sont considérés à jour pendant ce délai
REFRESH_AFTER = timedelta(hours=6)

# Marge avant le premier achat : il faut un cours de clôture le jour même
# ou juste avant (achat un week-end, jour férié...)
HISTORY_MARGIN = timedelta(days=7)

PRICE_STEP = Decimal("0.0001")


class MarketDataError(Exception):
    pass


def _yfinance():
    try:
        import yfinance
    except ImportError as error:
        raise MarketDataError(
            _("Le module yfinance n'est pas installé (pip install yfinance).")
        ) from error
    return yfinance


def base_currency(currency):
    """Devise principale et facteur de conversion : GBp -> (GBP, 0.01)."""
    currency = currency or "EUR"
    return MINOR_CURRENCIES.get(currency, (currency.upper(), Decimal("1")))


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite():
        return None
    return result.quantize(PRICE_STEP)


# ============================================================
# APPELS À YAHOO FINANCE
# ============================================================

def search_instruments(query, limit=8):
    """Titres correspondant à un ISIN, un nom ou un ticker."""

    query = (query or "").strip()
    if len(query) < 2:
        return []

    yf = _yfinance()
    try:
        quotes = yf.Search(query, max_results=limit, news_count=0, lists_count=0).quotes
    except Exception as error:
        logger.warning("Recherche Yahoo Finance impossible (%s) : %s", query, error)
        raise MarketDataError(_("Yahoo Finance ne répond pas. Réessaie dans un instant.")) from error

    # Yahoo ne renvoie pas l'ISIN : on le garde quand c'est ce qui a été cherché
    isin = query.upper() if ISIN_RE.match(query.upper()) else ""

    results = []
    for quote in quotes:
        if quote.get("quoteType") not in SEARCHABLE_TYPES or not quote.get("symbol"):
            continue
        results.append({
            "symbol": quote["symbol"],
            "name": quote.get("longname") or quote.get("shortname") or quote["symbol"],
            "exchange": quote.get("exchDisp") or quote.get("exchange") or "",
            "quote_type": quote.get("quoteType", ""),
            "type_label": quote.get("typeDisp") or quote.get("quoteType", ""),
            "isin": isin,
        })
    return results[:limit]


def fetch_history(symbol, start, end=None, adjusted=False):
    """
    Cours de clôture quotidiens de `start` à `end` (inclus, aujourd'hui par
    défaut). Renvoie {"currency", "name", "closes": [(date, Decimal)]}.
    `adjusted` : cours ajustés des dividendes et divisions (rendement total).
    """

    yf = _yfinance()
    try:
        ticker = yf.Ticker(symbol)
        frame = ticker.history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat() if end else None,
            interval="1d",
            auto_adjust=adjusted,
            actions=False,
        )
        metadata = ticker.history_metadata or {}
    except Exception as error:
        logger.warning("Cours Yahoo Finance impossibles pour %s : %s", symbol, error)
        raise MarketDataError(_('Cours indisponibles pour %(symbol)s.') % {"symbol": symbol}) from error

    closes = []
    if frame is not None and not frame.empty and "Close" in frame:
        for timestamp, value in frame["Close"].items():
            close = _decimal(value)
            if close is not None and close > 0:
                closes.append((timestamp.date(), close))

    if not closes:
        raise MarketDataError(_('Aucun cours trouvé pour %(symbol)s sur Yahoo Finance.') % {"symbol": symbol})

    return {
        "currency": metadata.get("currency") or "EUR",
        "name": metadata.get("longName") or metadata.get("shortName") or "",
        "closes": closes,
    }


def fx_symbol(currency):
    return f"{currency}EUR=X"


def price_on(symbol, day):
    """
    Cours de clôture d'un titre le `day` (ou à la dernière séance avant),
    converti en euros : sert à pré-remplir le prix d'achat.
    """

    data = fetch_history(symbol, day - timedelta(days=10), end=day)
    closes = [point for point in data["closes"] if point[0] <= day]
    if not closes:
        raise MarketDataError(_("Pas de cotation à cette date."))
    close_date, close = closes[-1]

    currency, factor = base_currency(data["currency"])
    price = close * factor
    if currency != "EUR":
        rates = [
            point for point in fetch_history(fx_symbol(currency), day - timedelta(days=10), end=day)["closes"]
            if point[0] <= day
        ]
        if not rates:
            raise MarketDataError(_('Taux de change %(currency)s/EUR indisponible.') % {"currency": currency})
        price *= rates[-1][1]

    return {
        "date": close_date,
        "close": close,
        "currency": data["currency"],
        "eur": price.quantize(PRICE_STEP),
    }


# ============================================================
# MISE À JOUR DES COURS EN BASE
# ============================================================

def _fetch_start(first_stored, last_stored, start):
    """Date à partir de laquelle télécharger : tout l'historique si on n'a
    rien (ou si un achat plus ancien a été ajouté), sinon les derniers jours."""

    if first_stored is None or first_stored > start + timedelta(days=10):
        return start
    return last_stored - timedelta(days=5)


def refresh_exchange_rate(currency, start):
    stored = ExchangeRate.objects.filter(currency=currency)
    bounds = stored.aggregate(first=Min("date"))
    last = stored.order_by("-date").values_list("date", flat=True).first()
    fetch_from = _fetch_start(bounds["first"], last, start)

    closes = fetch_history(fx_symbol(currency), fetch_from)["closes"]
    ExchangeRate.objects.bulk_create(
        [ExchangeRate(currency=currency, date=day, rate=rate) for day, rate in closes],
        update_conflicts=True,
        unique_fields=["currency", "date"],
        update_fields=["rate"],
    )


def refresh_instrument(instrument, start):
    """
    Télécharge les cours manquants d'un titre depuis `start` et met à jour
    son dernier cours. Renvoie True si les cours ont pu être récupérés.
    """

    bounds = instrument.prices.aggregate(first=Min("date"))
    last = instrument.prices.order_by("-date").values_list("date", flat=True).first()
    fetch_from = _fetch_start(bounds["first"], last, start)

    try:
        data = fetch_history(instrument.symbol, fetch_from)
    except MarketDataError as error:
        instrument.price_error = str(error)[:255]
        instrument.prices_updated_at = timezone.now()
        instrument.save(update_fields=["price_error", "prices_updated_at"])
        return False

    PricePoint.objects.bulk_create(
        [PricePoint(instrument=instrument, date=day, close=close) for day, close in data["closes"]],
        update_conflicts=True,
        unique_fields=["instrument", "date"],
        update_fields=["close"],
    )

    # Titre coté hors euro : il faut aussi le taux de change sur la période
    error = ""
    currency, _factor = base_currency(data["currency"])
    if currency != "EUR":
        try:
            refresh_exchange_rate(currency, start)
        except MarketDataError as fx_error:
            error = str(fx_error)

    last_date, last_close = data["closes"][-1]
    instrument.currency = data["currency"]
    instrument.last_price = last_close
    instrument.last_price_date = last_date
    instrument.prices_updated_at = timezone.now()
    instrument.price_error = error[:255]
    instrument.save(update_fields=[
        "currency", "last_price", "last_price_date", "prices_updated_at", "price_error",
    ])
    return True


def held_instrument_starts(user):
    """{instrument_id: date du premier mouvement} pour les titres de l'utilisateur."""

    rows = (
        PortfolioOperation.objects.filter(account__user=user, instrument__isnull=False)
        .values("instrument_id")
        .annotate(first=Min("date"))
    )
    return {row["instrument_id"]: row["first"] for row in rows}


def is_stale(instrument, now=None):
    now = now or timezone.now()
    return instrument.prices_updated_at is None or now - instrument.prices_updated_at > REFRESH_AFTER


def prices_are_stale(user):
    starts = held_instrument_starts(user)
    return any(is_stale(instrument) for instrument in Instrument.objects.filter(pk__in=starts))


def refresh_prices(user, force=False):
    """
    Met à jour les cours de tous les titres de l'utilisateur (ceux dont les
    cours ont plus de REFRESH_AFTER, ou tous avec `force`).
    Renvoie (nombre de titres mis à jour, messages d'erreur).
    """

    starts = held_instrument_starts(user)
    updated = 0
    errors = []
    for instrument in Instrument.objects.filter(pk__in=starts):
        if not force and not is_stale(instrument):
            continue
        if refresh_instrument(instrument, starts[instrument.pk] - HISTORY_MARGIN):
            updated += 1
        else:
            errors.append(instrument.price_error)
    return updated, errors


def first_needed_date(instrument):
    first = instrument.operations.aggregate(first=Min("date"))["first"]
    return (first or date.today()) - HISTORY_MARGIN
