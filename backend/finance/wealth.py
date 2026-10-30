"""
Calculs du module Épargne : livrets, portefeuilles (PEA, compte-titres),
frais et allocation.

Conventions :
  - prix d'achat, frais et dividendes sont saisis en euros ;
  - les cours Yahoo sont convertis en euros (titres cotés en dollars...) ;
  - PRU frais inclus, au coût moyen pondéré (comme les courtiers) ;
  - les frais courants des ETF (TER) ne sont jamais facturés à part : ils
    sont prélevés dans le cours. On les estime au jour le jour sur la
    valeur détenue, pour montrer ce qu'ils coûtent vraiment.
"""

import re
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum

from .bank_import import SAVINGS_ACCOUNT_RE
from .budget import SAVINGS_CATEGORY_NAME, add_months, money, percent
from .market_data import base_currency
from .models import (
    PORTFOLIO_KINDS,
    AssetClass,
    BalanceSnapshot,
    BalanceSource,
    EntryType,
    ExchangeRate,
    Instrument,
    OperationKind,
    PortfolioOperation,
    PricePoint,
    SavingsAccount,
    SavingsKind,
    Transaction,
    Zone,
)


ZERO = Decimal("0")
QUANTITY_EPSILON = Decimal("0.000001")

# Plafonds de versement réglementaires
DEFAULT_CEILINGS = {
    SavingsKind.LIVRET_A: Decimal("22950"),
    SavingsKind.LDDS: Decimal("12000"),
    SavingsKind.LEP: Decimal("10000"),
    SavingsKind.LIVRET_JEUNE: Decimal("1600"),
    SavingsKind.CEL: Decimal("15300"),
    SavingsKind.PEL: Decimal("61200"),
    SavingsKind.PEA: Decimal("150000"),
}

# Après 5 ans, un PEA garde son avantage fiscal même en cas de retrait
PEA_TAX_YEARS = 5

LIVRET_KINDS = {
    SavingsKind.LIVRET_A,
    SavingsKind.LDDS,
    SavingsKind.LEP,
    SavingsKind.LIVRET_JEUNE,
    SavingsKind.CEL,
    SavingsKind.PEL,
    SavingsKind.LIVRET,
}

BALANCE_KIND_CHOICES = [(k, label) for k, label in SavingsKind.choices if k not in PORTFOLIO_KINDS]
PORTFOLIO_KIND_CHOICES = [(k, label) for k, label in SavingsKind.choices if k in PORTFOLIO_KINDS]

# Pseudo-classes d'actifs de la vue d'ensemble
CASH_LABEL = "Liquidités"
LIVRETS_LABEL = "Livrets & épargne réglementée"
INSURANCE_LABEL = "Assurance vie & PER"


# ============================================================
# CLASSEMENT AUTOMATIQUE D'UN TITRE
# ============================================================

ASSET_CLASS_RULES = [
    (r"bond|oblig|treasury|aggregate|govt|government|corporate|\bbund", AssetClass.BOND),
    (r"money market|mon[ée]taire|overnight|\bestr\b|€str|smart cash", AssetClass.MONEY),
    (r"real estate|immobili|\breit|epra|propert", AssetClass.REAL_ESTATE),
    (r"\bgold\b|physical|commodit|mati[èe]res premi|silver|\bor\b", AssetClass.COMMODITY),
]

ZONE_RULES = [
    (r"world|monde|acwi|all.country|global", Zone.WORLD),
    (r"emerging|[ée]mergent|\bem\b|china|india|brazil|chine|inde", Zone.EMERGING),
    (r"s&p ?500|nasdaq|\busa?\b|united states|[ée]tats.unis|dow jones|russell|america", Zone.US),
    (r"\bcac\b|france|sbf", Zone.FRANCE),
    (r"europe|stoxx|\beuro|\bemu\b|\bdax\b|ftse mib|ibex", Zone.EUROPE),
    (r"japan|japon|topix|nikkei|pacific|asia|asie", Zone.ASIA),
]

# Actions : la zone se déduit de la place de cotation
EXCHANGE_ZONES = [
    (r"paris", Zone.FRANCE),
    (r"nasdaq|nyse|amex|bats|cboe|otc", Zone.US),
    (r"tokyo|hong kong|shanghai|shenzhen|taiwan|korea|singapore", Zone.ASIA),
    (r"xetra|frankfurt|amsterdam|milan|brussels|madrid|lisbon|london|swiss|vienna|dublin|oslo|stockholm|copenhagen|helsinki", Zone.EUROPE),
]


def _first_match(rules, text, default):
    return next((value for pattern, value in rules if re.search(pattern, text, re.I)), default)


def guess_classification(name, quote_type="", exchange=""):
    """(classe d'actifs, zone) devinées d'après le nom, le type et la place
    de cotation. L'utilisateur peut corriger ensuite."""

    if quote_type == "CRYPTOCURRENCY":
        return AssetClass.CRYPTO, Zone.WORLD
    if quote_type == "EQUITY":
        return AssetClass.EQUITY, _first_match(EXCHANGE_ZONES, exchange or "", Zone.OTHER)
    return (
        _first_match(ASSET_CLASS_RULES, name or "", AssetClass.EQUITY),
        _first_match(ZONE_RULES, name or "", Zone.WORLD),
    )


def get_or_create_instrument(user, symbol, name="", isin="", exchange="", quote_type=""):
    instrument = Instrument.objects.filter(user=user, symbol=symbol).first()
    if instrument:
        if isin and not instrument.isin:
            instrument.isin = isin
            instrument.save(update_fields=["isin"])
        return instrument, False

    asset_class, zone = guess_classification(name, quote_type, exchange)
    instrument = Instrument.objects.create(
        user=user,
        symbol=symbol,
        name=(name or symbol)[:200],
        isin=isin[:12],
        exchange=exchange[:50],
        quote_type=quote_type[:20],
        asset_class=asset_class,
        zone=zone,
    )
    return instrument, True


# ============================================================
# SOLDES DES LIVRETS DEPUIS L'IMPORT BANCAIRE
# ============================================================

BANK_KIND_RULES = [
    (r"livret a\b", SavingsKind.LIVRET_A),
    (r"\bldds?\b|d[ée]veloppement durable", SavingsKind.LDDS),
    (r"\blep\b|[ée]pargne populaire", SavingsKind.LEP),
    (r"livret jeune", SavingsKind.LIVRET_JEUNE),
    (r"\bcel\b|compte (d.)?[ée]pargne logement", SavingsKind.CEL),
    (r"\bpel\b|plan (d.)?[ée]pargne logement", SavingsKind.PEL),
]

# Livrets réglementés : un seul par personne. Un livret de ce type saisi à
# la main est donc forcément celui de la banque.
UNIQUE_KINDS = {
    SavingsKind.LIVRET_A,
    SavingsKind.LDDS,
    SavingsKind.LEP,
    SavingsKind.LIVRET_JEUNE,
    SavingsKind.CEL,
    SavingsKind.PEL,
}

# Comptes de titres : suivis ligne par ligne, pas par leur solde
SECURITIES_ACCOUNT_RE = re.compile(r"\bpea\b|en actions|titres", re.I)


def guess_savings_kind(label):
    return _first_match(BANK_KIND_RULES, label or "", SavingsKind.LIVRET)


def import_bank_balances(user, balances, institution="Crédit Agricole"):
    """
    Soldes des livrets lus dans l'export bancaire : le livret est créé à la
    première synchronisation (ou rattaché au livret du même type déjà saisi
    à la main), puis son solde mis à jour à chaque fois.
    Renvoie les livrets mis à jour.
    """

    updated = []
    for balance in balances:
        label = balance.account or ""
        if not SAVINGS_ACCOUNT_RE.search(label) or SECURITIES_ACCOUNT_RE.search(label):
            continue

        account = SavingsAccount.objects.filter(
            user=user, bank_account_number=balance.account_number
        ).first()
        kind = guess_savings_kind(label)
        if account is None and kind in UNIQUE_KINDS:
            account = SavingsAccount.objects.filter(
                user=user, kind=kind, bank_account_number=""
            ).first()
            if account is not None:
                account.bank_account_number = balance.account_number or ""
                account.save(update_fields=["bank_account_number"])
        if account is None:
            account = SavingsAccount.objects.create(
                user=user,
                kind=kind,
                name=label[:100],
                institution=institution,
                bank_account_number=balance.account_number or "",
                ceiling=DEFAULT_CEILINGS.get(kind),
            )

        BalanceSnapshot.objects.update_or_create(
            account=account,
            date=balance.balance_date,
            defaults={"balance": balance.balance, "source": BalanceSource.BANK},
        )
        updated.append(account)
    return updated


# ============================================================
# LIVRETS ET COMPTES SUIVIS PAR LEUR SOLDE
# ============================================================

def _balance_at(snapshots_desc, day):
    """Solde au `day` d'après des relevés triés du plus récent au plus ancien."""
    return next((snapshot.balance for snapshot in snapshots_desc if snapshot.date <= day), None)


def balance_account_rows(accounts, today=None):
    today = today or date.today()
    rows = []
    for account in accounts:
        snapshots = list(account.snapshots.all())
        latest = snapshots[0] if snapshots else None
        balance = latest.balance if latest else ZERO
        year_ago = _balance_at(snapshots, today - timedelta(days=365))
        interest = money(balance * account.interest_rate / 100) if account.interest_rate else None
        ceiling = account.ceiling
        rows.append({
            "account": account,
            "balance": balance,
            "date": latest.date if latest else None,
            "is_bank": bool(latest and latest.source == BalanceSource.BANK),
            "interest_year": interest,
            "interest_month": money(interest / 12) if interest else None,
            "ceiling": ceiling,
            "fill": min(100, percent(balance, ceiling)) if ceiling else None,
            "room": max(ZERO, ceiling - balance) if ceiling else None,
            "is_full": bool(ceiling and balance >= ceiling),
            "change_year": balance - year_ago if year_ago is not None else None,
            "snapshots": snapshots,
            "series": [(snapshot.date, snapshot.balance) for snapshot in reversed(snapshots)],
        })
    return rows


def balance_totals(rows):
    total = sum((row["balance"] for row in rows), ZERO)
    interest = sum((row["interest_year"] or ZERO for row in rows), ZERO)
    return {
        "balance": total,
        "interest_year": money(interest),
        "average_rate": (interest / total * 100).quantize(Decimal("0.01")) if total else None,
        "count": len(rows),
    }


# ============================================================
# PORTEFEUILLES : COURS, POSITIONS, PERFORMANCE
# ============================================================

class _Series:
    """Valeurs datées, lues au dernier point connu à une date donnée."""

    def __init__(self, points):
        self.dates = [day for day, _ in points]
        self.values = [value for _, value in points]

    def at(self, day):
        index = bisect_right(self.dates, day) - 1
        return self.values[index] if index >= 0 else None

    def last(self):
        return (self.dates[-1], self.values[-1]) if self.dates else None

    def before_last(self):
        return (self.dates[-2], self.values[-2]) if len(self.dates) > 1 else None


def _eur_price_series(instruments):
    """{instrument_id: _Series des cours de clôture en euros}."""

    raw = defaultdict(list)
    for instrument_id, day, close in (
        PricePoint.objects.filter(instrument__in=instruments)
        .order_by("date")
        .values_list("instrument_id", "date", "close")
    ):
        raw[instrument_id].append((day, close))

    currencies = {base_currency(instrument.currency)[0] for instrument in instruments} - {"EUR"}
    rates = {
        currency: _Series(list(
            ExchangeRate.objects.filter(currency=currency).order_by("date").values_list("date", "rate")
        ))
        for currency in currencies
    }

    series = {}
    for instrument in instruments:
        currency, factor = base_currency(instrument.currency)
        points = []
        for day, close in raw[instrument.pk]:
            value = close * factor
            if currency != "EUR":
                fx = rates[currency]
                rate = fx.at(day) or (fx.values[0] if fx.values else None)
                if rate is None:
                    continue
                value *= rate
            points.append((day, value))
        series[instrument.pk] = _Series(points)
    return series


class Position:
    """Titres d'un compte : quantité, coût de revient (PRU frais inclus),
    plus-values réalisées, dividendes et courtage payé."""

    def __init__(self, account, instrument):
        self.account = account
        self.instrument = instrument
        self.quantity = ZERO
        self.cost = ZERO
        self.realized = ZERO
        self.dividends = ZERO
        self.fees = ZERO
        self.orders = 0
        self.bought = ZERO
        self.volume = ZERO

    def apply(self, op):
        if op.kind == OperationKind.BUY:
            self.quantity += op.quantity
            self.cost += op.total
            self.bought += op.total
            self.volume += op.gross
            self.fees += op.fees
            self.orders += 1
        elif op.kind == OperationKind.SELL:
            sold = min(op.quantity, self.quantity)
            sold_cost = self.cost * sold / self.quantity if self.quantity else ZERO
            self.realized += op.total - sold_cost
            self.cost -= sold_cost
            self.quantity -= op.quantity
            self.volume += op.gross
            self.fees += op.fees
            self.orders += 1
            if self.quantity <= QUANTITY_EPSILON:
                self.quantity = ZERO
                self.cost = ZERO
        elif op.kind == OperationKind.DIVIDEND:
            self.dividends += op.amount or ZERO

    @property
    def pru(self):
        return self.cost / self.quantity if self.quantity else None


def held_quantity(account, instrument, day, exclude_pk=None):
    """Nombre de titres détenus sur un compte à une date (pour valider une vente)."""

    position = Position(account, instrument)
    operations = PortfolioOperation.objects.filter(
        account=account, instrument=instrument, date__lte=day
    ).order_by("date", "created_at", "pk")
    if exclude_pk:
        operations = operations.exclude(pk=exclude_pk)
    for op in operations:
        position.apply(op)
    return position.quantity


def xirr(flows):
    """
    Rendement annualisé (taux de rendement interne) de flux datés
    [(date, montant)] : négatif = argent investi, positif = argent récupéré.
    Renvoie un pourcentage, ou None si le calcul n'a pas de sens.
    """

    amounts = [float(amount) for _, amount in flows]
    if not any(a < 0 for a in amounts) or not any(a > 0 for a in amounts):
        return None
    start = min(day for day, _ in flows)
    years = [(day - start).days / 365.0 for day, _ in flows]

    def npv(rate):
        return sum(amount / (1 + rate) ** t for amount, t in zip(amounts, years))

    low, high = -0.99, 10.0
    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:
        return None
    mid = low
    for _ in range(200):
        mid = (low + high) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_low * f_mid < 0:
            high = mid
        else:
            low, f_low = mid, f_mid
    return Decimal(str(round(mid * 100, 2)))


def _allocation(values):
    """[(libellé, valeur)] -> parts triées, en pourcentage du total."""

    merged = defaultdict(lambda: ZERO)
    for label, value in values:
        if value > 0:
            merged[label] += value
    total = sum(merged.values(), ZERO)
    return [
        {
            "name": label,
            "value": money(value),
            "percent": (value / total * 100).quantize(Decimal("0.1")) if total else ZERO,
        }
        for label, value in sorted(merged.items(), key=lambda item: -item[1])
    ]


def _empty_flows():
    return {"buys": ZERO, "sells": ZERO, "dividends": ZERO, "fees": ZERO}


def analyse_portfolio(accounts, today=None):
    """
    Positions, performance, frais et allocation d'un ou plusieurs comptes
    (PEA, compte-titres), et leur évolution jour par jour depuis le premier
    achat.
    """

    today = today or date.today()
    accounts = list(accounts)
    operations = list(
        PortfolioOperation.objects.filter(account__in=accounts, date__lte=today)
        .select_related("instrument", "account")
        .order_by("date", "created_at", "pk")
    )
    instruments = {op.instrument_id: op.instrument for op in operations if op.instrument_id}
    prices = _eur_price_series(list(instruments.values()))

    positions = {}
    flows = defaultdict(_empty_flows)
    cash_flows = []
    account_fees = ZERO
    account_dividends = ZERO
    brokerage = ZERO
    ter_accrued = ZERO
    last_year = today - timedelta(days=365)
    account_fees_year = ZERO

    history = {"dates": [], "value": [], "invested": [], "brokerage": [], "accountFees": [], "ter": []}

    def price_of(position, day):
        series = prices.get(position.instrument.pk)
        price = series.at(day) if series else None
        return price if price is not None else position.pru

    first_day = operations[0].date if operations else today
    days = sorted(
        {day for series in prices.values() for day in series.dates if first_day <= day <= today}
        | {op.date for op in operations}
        | ({today} if operations else set())
    )

    index = 0
    previous_day = None
    for day in days:
        # Frais courants sur la période écoulée, au prorata des jours
        if previous_day is not None:
            gap = Decimal((day - previous_day).days) / Decimal("365")
            for position in positions.values():
                ter = position.instrument.ter
                if position.quantity > 0 and ter:
                    ter_accrued += position.quantity * price_of(position, previous_day) * ter / 100 * gap

        while index < len(operations) and operations[index].date <= day:
            op = operations[index]
            index += 1
            account_flow = flows[op.account_id]
            if op.kind == OperationKind.FEE:
                account_fees += op.amount or ZERO
                account_flow["fees"] += op.amount or ZERO
                if op.date > last_year:
                    account_fees_year += op.amount or ZERO
                cash_flows.append((op.date, -(op.amount or ZERO)))
                continue
            if op.kind == OperationKind.DIVIDEND:
                account_flow["dividends"] += op.amount or ZERO
                cash_flows.append((op.date, op.amount or ZERO))
                if not op.instrument_id:
                    account_dividends += op.amount or ZERO
                    continue
            key = (op.account_id, op.instrument_id)
            if key not in positions:
                positions[key] = Position(op.account, op.instrument)
            positions[key].apply(op)
            if op.kind == OperationKind.BUY:
                brokerage += op.fees
                account_flow["buys"] += op.total
                cash_flows.append((op.date, -op.total))
            elif op.kind == OperationKind.SELL:
                brokerage += op.fees
                account_flow["sells"] += op.total
                cash_flows.append((op.date, op.total))

        held = [position for position in positions.values() if position.quantity > 0]
        history["dates"].append(day.isoformat())
        history["value"].append(str(money(sum((p.quantity * price_of(p, day) for p in held), ZERO))))
        history["invested"].append(str(money(sum((p.cost for p in held), ZERO))))
        history["brokerage"].append(str(money(brokerage)))
        history["accountFees"].append(str(money(account_fees)))
        history["ter"].append(str(money(ter_accrued)))
        previous_day = day

    # ---------- Positions détenues ----------
    rows = []
    closed = []
    for position in positions.values():
        instrument = position.instrument
        if position.quantity <= 0:
            if position.realized or position.dividends:
                closed.append(position)
            continue
        series = prices.get(instrument.pk)
        last = series.last() if series else None
        previous = series.before_last() if series else None
        price = last[1] if last else None
        value = position.quantity * price if price is not None else position.cost
        pnl = value - position.cost
        day_change = position.quantity * (last[1] - previous[1]) if last and previous else ZERO
        rows.append({
            "position": position,
            "account": position.account,
            "instrument": instrument,
            "quantity": clean_quantity(position.quantity),
            "pru": position.pru,
            "cost": money(position.cost),
            "price": price,
            "price_date": last[0] if last else None,
            "has_price": price is not None,
            "value": money(value),
            "pnl": money(pnl),
            "pnl_pct": (pnl / position.cost * 100).quantize(Decimal("0.01")) if position.cost else None,
            "day_change": money(day_change),
            "day_change_pct": (
                (last[1] - previous[1]) / previous[1] * 100
            ).quantize(Decimal("0.01")) if last and previous and previous[1] else None,
            "realized": money(position.realized),
            "dividends": money(position.dividends),
            "fees": money(position.fees),
            "orders": position.orders,
            "bought": money(position.bought),
            "fees_pct": (position.fees / position.volume * 100).quantize(Decimal("0.01")) if position.volume else None,
            "ter": instrument.ter,
            "ter_year": money(value * instrument.ter / 100) if instrument.ter else None,
            "is_fund": instrument.quote_type in {"ETF", "MUTUALFUND"},
        })

    securities = sum((row["value"] for row in rows), ZERO)
    for row in rows:
        row["weight"] = (row["value"] / securities * 100).quantize(Decimal("0.1")) if securities else ZERO
    rows.sort(key=lambda row: -row["value"])

    # ---------- Totaux et performance ----------
    cash = sum((account.cash_balance for account in accounts), ZERO)
    cost = sum((row["cost"] for row in rows), ZERO)
    latent = securities - cost
    realized = sum((position.realized for position in positions.values()), ZERO)
    dividends = sum((position.dividends for position in positions.values()), ZERO) + account_dividends
    bought = sum((position.bought for position in positions.values()), ZERO)
    gain = latent + realized + dividends - account_fees
    day_change = sum((row["day_change"] for row in rows), ZERO)
    held_days = (today - first_day).days if operations else 0

    annualized = None
    if operations and held_days >= 365:
        annualized = xirr(cash_flows + [(today, securities)])

    totals = {
        "value": money(securities + cash),
        "securities": money(securities),
        "cash": money(cash),
        "cost": money(cost),
        "latent": money(latent),
        "latent_pct": (latent / cost * 100).quantize(Decimal("0.01")) if cost else None,
        "realized": money(realized),
        "dividends": money(dividends),
        "account_fees": money(account_fees),
        "gain": money(gain),
        "gain_pct": (gain / bought * 100).quantize(Decimal("0.01")) if bought else None,
        "annualized": annualized,
        "first_date": first_day if operations else None,
        "day_change": money(day_change),
        "day_change_pct": (
            day_change / (securities - day_change) * 100
        ).quantize(Decimal("0.01")) if securities - day_change else None,
    }

    # ---------- Frais ----------
    volume = sum((position.volume for position in positions.values()), ZERO)
    orders = sum(position.orders for position in positions.values())
    ter_year = sum((row["ter_year"] or ZERO for row in rows), ZERO)
    with_ter = sum((row["value"] for row in rows if row["ter"]), ZERO)
    annual_cost = ter_year + account_fees_year
    fees = {
        "brokerage": money(brokerage),
        "orders": orders,
        "average_order_pct": (brokerage / volume * 100).quantize(Decimal("0.01")) if volume else None,
        "average_order_fee": money(brokerage / orders) if orders else None,
        "account_fees": money(account_fees),
        "account_fees_year": money(account_fees_year),
        "ter_to_date": money(ter_accrued),
        "paid": money(brokerage + account_fees),
        "total": money(brokerage + account_fees + ter_accrued),
        "ter_year": money(ter_year),
        "weighted_ter": (ter_year / with_ter * 100).quantize(Decimal("0.001")) if with_ter else None,
        "missing_ter": [row for row in rows if row["is_fund"] and not row["ter"]],
        "annual_cost": money(annual_cost),
        "annual_cost_pct": (annual_cost / (securities + cash) * 100).quantize(Decimal("0.01")) if securities + cash else None,
        "ten_years": money(annual_cost * 10),
        "gain_before_fees": money(gain + brokerage + account_fees),
    }

    # ---------- Allocation ----------
    by_line = defaultdict(lambda: ZERO)
    for row in rows:
        by_line[row["instrument"].name] += row["value"]
    allocation = {
        "lines": _allocation(list(by_line.items()) + [(CASH_LABEL, cash)]),
        "classes": _allocation(
            [(row["instrument"].get_asset_class_display(), row["value"]) for row in rows] + [(CASH_LABEL, cash)]
        ),
        "zones": _allocation([(row["instrument"].get_zone_display(), row["value"]) for row in rows]),
    }

    # ---------- Par compte (PEA : plafond et ancienneté) ----------
    per_account = []
    for account in accounts:
        account_rows = [row for row in rows if row["account"].pk == account.pk]
        account_value = sum((row["value"] for row in account_rows), ZERO)
        account_cost = sum((row["cost"] for row in account_rows), ZERO)
        account_flow = flows[account.pk]
        info = {
            "account": account,
            "securities": money(account_value),
            "value": money(account_value + account.cash_balance),
            "cost": money(account_cost),
            "latent": money(account_value - account_cost),
            "latent_pct": ((account_value - account_cost) / account_cost * 100).quantize(Decimal("0.01")) if account_cost else None,
            "lines": len(account_rows),
        }
        if account.kind == SavingsKind.PEA:
            # Espèces = versements - achats + ventes + dividendes - frais
            deposits = max(
                ZERO,
                account.cash_balance + account_flow["buys"] - account_flow["sells"]
                - account_flow["dividends"] + account_flow["fees"],
            )
            ceiling = account.ceiling or DEFAULT_CEILINGS[SavingsKind.PEA]
            info["pea"] = {
                "deposits": money(deposits),
                "ceiling": ceiling,
                "fill": min(100, percent(deposits, ceiling)),
                "room": max(ZERO, ceiling - deposits),
            }
            if account.opened_on:
                tax_date = add_years(account.opened_on, PEA_TAX_YEARS)
                info["pea"].update({
                    "tax_date": tax_date,
                    "is_mature": today >= tax_date,
                    "age_years": (today - account.opened_on).days // 365,
                })
        per_account.append(info)

    return {
        "has_operations": bool(operations),
        "operations": list(reversed(operations)),
        "positions": rows,
        "closed": closed,
        "totals": totals,
        "fees": fees,
        "allocation": allocation,
        "accounts": per_account,
        "history": history,
        "price_date": max((row["price_date"] for row in rows if row["price_date"]), default=None),
        "price_errors": [instrument for instrument in instruments.values() if instrument.price_error],
    }


def clean_quantity(value):
    """10.000000 -> 10, 2.500000 -> 2.5 (sans notation scientifique)."""
    if value == value.to_integral_value():
        return value.quantize(Decimal("1"))
    return value.normalize()


def add_years(value, years):
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # 29 février
        return value.replace(year=value.year + years, day=28)


# ============================================================
# VUE D'ENSEMBLE
# ============================================================

def savings_contributions(user, today=None, months=12):
    """Versements vers l'épargne enregistrés dans le budget (12 derniers mois)."""

    today = today or date.today()
    start_year, start_month = add_months(today.year, today.month, -(months - 1))
    total = Transaction.objects.filter(
        user=user,
        transaction_type=EntryType.EXPENSE,
        category__name=SAVINGS_CATEGORY_NAME,
        transaction_date__gte=date(start_year, start_month, 1),
        transaction_date__lte=today,
    ).aggregate(total=Sum("amount"))["total"]
    return money(total)


def _support_class(kind):
    return LIVRETS_LABEL if kind in LIVRET_KINDS else INSURANCE_LABEL


def savings_overview(user, today=None):
    today = today or date.today()
    accounts = list(SavingsAccount.objects.filter(user=user).prefetch_related("snapshots"))
    balance_rows = balance_account_rows([a for a in accounts if not a.is_portfolio], today)
    portfolio_accounts = [a for a in accounts if a.is_portfolio]
    portfolio = analyse_portfolio(portfolio_accounts, today) if portfolio_accounts else None

    supports = []
    for row in balance_rows:
        supports.append({
            "account": row["account"],
            "value": row["balance"],
            "date": row["date"],
            "interest_year": row["interest_year"],
        })
    for info in (portfolio["accounts"] if portfolio else []):
        supports.append({
            "account": info["account"],
            "value": info["value"],
            "latent": info["latent"],
            "latent_pct": info["latent_pct"],
            "lines": info["lines"],
        })

    total = sum((support["value"] for support in supports), ZERO)
    for support in supports:
        support["weight"] = (support["value"] / total * 100).quantize(Decimal("0.1")) if total else ZERO
    supports.sort(key=lambda support: -support["value"])

    balances = balance_totals(balance_rows)
    class_values = [(_support_class(row["account"].kind), row["balance"]) for row in balance_rows]
    if portfolio:
        class_values += [(item["name"], item["value"]) for item in portfolio["allocation"]["classes"]]

    return {
        "has_accounts": bool(accounts),
        "total": money(total),
        "balances": balances,
        "portfolio": portfolio,
        "supports": supports,
        "contributions": savings_contributions(user, today),
        "allocation": {
            "supports": _allocation([(s["account"].name, s["value"]) for s in supports]),
            "classes": _allocation(class_values),
        },
        "history": _overview_history(balance_rows, portfolio, today),
    }


def _overview_history(balance_rows, portfolio, today):
    """Livrets (d'après leurs relevés) et titres (cours quotidiens) cumulés."""

    portfolio_dates = [date.fromisoformat(day) for day in portfolio["history"]["dates"]] if portfolio else []
    portfolio_values = _Series(list(zip(
        portfolio_dates,
        [Decimal(value) for value in portfolio["history"]["value"]] if portfolio else [],
    )))
    balance_series = [_Series(row["series"]) for row in balance_rows if row["series"]]
    days = sorted(
        set(portfolio_dates)
        | {day for series in balance_series for day in series.dates}
    )
    if not days:
        return {"dates": [], "balances": [], "securities": []}
    if days[-1] < today:
        days.append(today)

    history = {"dates": [], "balances": [], "securities": []}
    for day in days:
        history["dates"].append(day.isoformat())
        history["balances"].append(str(money(sum((s.at(day) or ZERO for s in balance_series), ZERO))))
        history["securities"].append(str(money(portfolio_values.at(day) or ZERO)))
    return history
