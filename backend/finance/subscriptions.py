"""
Détection des abonnements à partir des opérations (relevé bancaire).

Principe :
  1. on regroupe les dépenses par commerçant (clé normalisée) puis par
     montant proche (une même enseigne peut avoir plusieurs abonnements,
     ex. Apple : iCloud 0,99 € et Apple Music 10,99 €) ;
  2. un groupe est un abonnement si les paiements reviennent à intervalle
     régulier : chaque mois (≥ 3 mois) ou chaque année (≥ 2 ans) ;
  3. un abonnement est considéré arrêté quand le paiement attendu n'est
     pas arrivé après un délai de tolérance.

Les abonnements suivis (RecurringItem avec match_key) sont rattachés
automatiquement aux nouveaux paiements importés, et passent tout seuls
en « arrêté » (ou redeviennent actifs si un paiement réapparaît).
"""

import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .budget import FOOD_CATEGORY_NAME, SAVINGS_CATEGORY_NAME, money
from .models import (
    DismissedSubscription,
    EntryType,
    RecurrenceKind,
    RecurrenceUnit,
    RecurringItem,
    Transaction,
)

# ~2 ans : il faut deux paiements pour reconnaître un abonnement annuel
HISTORY_DAYS = 800

# Cadences reconnues : (unité, intervalle typique en jours, bornes acceptées, tolérance de retard)
CADENCES = {
    RecurrenceUnit.MONTH: {"days": 30, "min": 24, "max": 38, "grace": 10, "min_count": 3},
    RecurrenceUnit.YEAR: {"days": 365, "min": 340, "max": 390, "grace": 30, "min_count": 2},
}

# Écart de montant toléré dans un même abonnement (hausse de prix, TVA...)
AMOUNT_TOLERANCE = Decimal("0.20")

# Catégories qui ressemblent à des abonnements mais n'en sont pas
EXCLUDED_CATEGORIES = {SAVINGS_CATEGORY_NAME, FOOD_CATEGORY_NAME}

# Charges fixes plutôt qu'abonnements
FIXED_CATEGORIES = {"Logement & charges", "Impôts, taxes & frais"}

STOPWORDS = {
    "prlv", "sepa", "paiement", "par", "carte", "cb", "www", "com", "fr",
    "sas", "sa", "sarl", "eu", "europe", "france", "paris", "bill", "payment",
    "virement", "vir", "inst", "de", "du", "la", "le", "les", "des",
}


# ============================================================
# CLÉ COMMERÇANT
# ============================================================

def merchant_key(text):
    """« SPOTIFY P4776921FD » et « Spotify » donnent la même clé : spotify."""

    if not text:
        return ""

    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    tokens = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    tokens = [
        token for token in tokens
        if token not in STOPWORDS and not re.search(r"\d", token) and len(token) > 1
    ]
    return " ".join(tokens[:3])


# Un même service facturé sous plusieurs noms selon les mois
MERCHANT_ALIASES = {
    "claude": "anthropic",
    "chatgpt": "openai",
    "mycanal": "canal",
    "disneyplus": "disney",
    "itunes": "apple",
    "icloud": "apple",
}


def merchant_root(text):
    """Nom principal du commerçant : son premier mot significatif.
    La banque ne libelle pas toujours un même paiement de la même façon
    (« ANTHROPIC* CLAUDE SU » puis « Anthropic ») : on ne garde que
    « anthropic ». Les montants et la régularité séparent ensuite les
    éventuels services différents d'une même enseigne."""

    key = merchant_key(text)
    root = key.split()[0] if key else ""
    return MERCHANT_ALIASES.get(root, root)


def transaction_key(transaction):
    return merchant_root(transaction.description or transaction.bank_label)


def amounts_match(amount, reference):
    if not reference:
        return False
    return abs(amount - reference) <= reference * AMOUNT_TOLERANCE


# ============================================================
# DÉTECTION
# ============================================================

@dataclass
class Candidate:
    key: str                      # identifiant stable : commerçant|montant
    merchant_key: str
    name: str
    amount: Decimal
    unit: str
    interval_days: int
    transactions: list = field(default_factory=list)
    category: object = None
    subcategory: object = None

    @property
    def first_date(self):
        return self.transactions[0].transaction_date

    @property
    def last_date(self):
        return self.transactions[-1].transaction_date

    @property
    def count(self):
        return len(self.transactions)

    @property
    def kind(self):
        if self.category and self.category.name in FIXED_CATEGORIES:
            return RecurrenceKind.FIXED
        return RecurrenceKind.SUBSCRIPTION

    @property
    def frequency_label(self):
        return "Tous les mois" if self.unit == RecurrenceUnit.MONTH else "Tous les ans"

    @property
    def monthly_amount(self):
        return self.amount if self.unit == RecurrenceUnit.MONTH else money(self.amount / 12)


def _amount_clusters(transactions):
    """Sépare les paiements d'un même commerçant par montant proche."""

    clusters = []
    for transaction in sorted(transactions, key=lambda t: t.amount):
        for cluster in clusters:
            if amounts_match(transaction.amount, statistics.median(t.amount for t in cluster)):
                cluster.append(transaction)
                break
        else:
            clusters.append([transaction])
    return clusters


def _cadence(transactions):
    """Renvoie (unité, intervalle médian) si les dates sont régulières."""

    dates = [t.transaction_date for t in transactions]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    if not gaps:
        return None

    for unit, rule in CADENCES.items():
        if len(transactions) < rule["min_count"]:
            continue
        regular = [gap for gap in gaps if rule["min"] <= gap <= rule["max"]]
        # Au moins 75 % des écarts doivent correspondre à la cadence
        if len(regular) >= max(1, round(len(gaps) * 0.75)):
            return unit, int(statistics.median(regular))
    return None


def _expense_history(user, today):
    return (
        Transaction.objects.filter(
            user=user,
            transaction_type=EntryType.EXPENSE,
            transaction_date__gte=today - timedelta(days=HISTORY_DAYS),
            transaction_date__lte=today,
        )
        .exclude(category__name__in=EXCLUDED_CATEGORIES)
        .select_related("category", "subcategory", "recurring_item")
        .order_by("transaction_date")
    )


def detect_candidates(user, today=None):
    """Toutes les séries régulières de paiements, suivies ou non."""

    today = today or date.today()
    by_merchant = {}
    for transaction in _expense_history(user, today):
        # Les lignes générées par un poste manuel ne sont pas des paiements réels
        if transaction.recurring_item_id and not transaction.recurring_item.match_key:
            continue
        key = transaction_key(transaction)
        if key:
            by_merchant.setdefault(key, []).append(transaction)

    candidates = []
    for key, transactions in by_merchant.items():
        for cluster in _amount_clusters(transactions):
            cluster.sort(key=lambda t: t.transaction_date)
            cadence = _cadence(cluster)
            if not cadence:
                continue
            unit, interval = cadence
            last = cluster[-1]
            amount = money(statistics.median(t.amount for t in cluster))
            candidates.append(Candidate(
                key=f"{key}|{amount}",
                merchant_key=key,
                name=last.description or key.title(),
                amount=last.amount,
                unit=unit,
                interval_days=interval,
                transactions=cluster,
                category=last.category,
                subcategory=last.subcategory,
            ))

    return sorted(candidates, key=lambda c: c.name.lower())


# ============================================================
# STATUT : ACTIF / ATTENDU / ARRÊTÉ
# ============================================================

def payment_status(last_paid, unit, today, interval_days=None):
    """Statut d'après la date du dernier paiement."""

    rule = CADENCES.get(unit, CADENCES[RecurrenceUnit.MONTH])
    interval = interval_days or rule["days"]
    next_due = last_paid + timedelta(days=interval)
    stop_after = next_due + timedelta(days=rule["grace"])

    if today > stop_after:
        status, label = "stopped", "Arrêté"
    elif today >= next_due - timedelta(days=3):
        status, label = "due", "Paiement attendu"
    else:
        status, label = "active", "Actif"

    return {
        "status": status,
        "label": label,
        "last_paid": last_paid,
        "next_due": next_due,
        "stopped_since": next_due if status == "stopped" else None,
        "days_late": max(0, (today - next_due).days),
    }


def _item_unit(item):
    return RecurrenceUnit.YEAR if item.frequency_unit == RecurrenceUnit.YEAR else RecurrenceUnit.MONTH


def _item_interval_days(item):
    rule = CADENCES[_item_unit(item)]
    return rule["days"] * (item.frequency_interval or 1)


# ============================================================
# ABONNEMENTS SUIVIS
# ============================================================

def link_payments(user, items=None, today=None):
    """Rattache les paiements importés aux abonnements suivis."""

    today = today or date.today()
    items = list(items if items is not None else RecurringItem.objects.filter(user=user).exclude(match_key=""))
    if not items:
        return 0

    linked = 0
    unlinked = Transaction.objects.filter(
        user=user,
        transaction_type=EntryType.EXPENSE,
        recurring_item__isnull=True,
        transaction_date__gte=today - timedelta(days=HISTORY_DAYS),
    )
    for transaction in unlinked:
        key = transaction_key(transaction)
        for item in items:
            if key == merchant_root(item.match_key) and amounts_match(transaction.amount, item.default_amount):
                transaction.recurring_item = item
                transaction.save(update_fields=["recurring_item"])
                linked += 1
                break
    return linked


def sync_subscriptions(user, today=None):
    """Rattache les nouveaux paiements, puis met à jour le statut des
    abonnements suivis : arrêtés s'ils ne sont plus payés, réactivés si
    un paiement réapparaît. Renvoie les changements de statut."""

    today = today or date.today()
    items = list(RecurringItem.objects.filter(user=user).exclude(match_key=""))
    link_payments(user, items, today)

    changes = []
    for item in items:
        payments = item.transactions.order_by("transaction_date")
        last = payments.last()
        if last is None:
            continue

        # Le montant suit le dernier prix payé (hausse de tarif)
        if last.amount != item.default_amount:
            item.default_amount = last.amount
            item.save(update_fields=["default_amount"])

        status = payment_status(last.transaction_date, _item_unit(item), today, _item_interval_days(item))

        if status["status"] == "stopped" and item.is_active:
            item.is_active = False
            item.end_date = last.transaction_date
            item.save(update_fields=["is_active", "end_date"])
            changes.append(("stopped", item))
        elif status["status"] != "stopped" and not item.is_active:
            item.is_active = True
            item.end_date = None
            item.save(update_fields=["is_active", "end_date"])
            changes.append(("resumed", item))

    return changes


def tracked_subscriptions(user, today=None):
    today = today or date.today()
    rows = []
    for item in RecurringItem.objects.filter(
        user=user, entry_type=EntryType.EXPENSE
    ).exclude(match_key="").select_related("category"):
        last = item.transactions.order_by("transaction_date").last()
        status = (
            payment_status(last.transaction_date, _item_unit(item), today, _item_interval_days(item))
            if last else None
        )
        rows.append({
            "item": item,
            "status": status,
            "payments": item.transactions.count(),
            "total_paid": money(sum((t.amount for t in item.transactions.all()), Decimal("0"))),
            "monthly_amount": item.default_amount if _item_unit(item) == RecurrenceUnit.MONTH else money(item.default_amount / 12),
        })

    order = {"stopped": 2, "due": 0, "active": 1}
    rows.sort(key=lambda row: (order.get(row["status"]["status"], 3) if row["status"] else 3, row["item"].name.lower()))
    return rows


# ============================================================
# SUGGESTIONS
# ============================================================

def subscription_suggestions(user, today=None):
    """Séries détectées pas encore suivies ni ignorées, séparées entre
    abonnements en cours et abonnements déjà arrêtés."""

    today = today or date.today()
    tracked_keys = set(
        RecurringItem.objects.filter(user=user).exclude(match_key="").values_list("match_key", "default_amount")
    )
    dismissed = set(DismissedSubscription.objects.filter(user=user).values_list("key", flat=True))

    active, stopped = [], []
    for candidate in detect_candidates(user, today):
        if candidate.key in dismissed:
            continue
        if any(merchant_root(key) == candidate.merchant_key and amounts_match(candidate.amount, amount) for key, amount in tracked_keys):
            continue
        # Déjà rattachée à un poste suivi
        if all(t.recurring_item_id for t in candidate.transactions):
            continue

        status = payment_status(candidate.last_date, candidate.unit, today, candidate.interval_days)
        entry = {"candidate": candidate, "status": status}
        (stopped if status["status"] == "stopped" else active).append(entry)

    return {"active": active, "stopped": stopped}


def find_candidate(user, key, today=None):
    for candidate in detect_candidates(user, today):
        if candidate.key == key:
            return candidate
    return None


def follow_candidate(user, candidate, today=None):
    """Crée l'abonnement suivi et y rattache les paiements passés."""

    today = today or date.today()
    status = payment_status(candidate.last_date, candidate.unit, today, candidate.interval_days)
    is_stopped = status["status"] == "stopped"

    item = RecurringItem.objects.create(
        user=user,
        name=candidate.name[:150],
        kind=candidate.kind,
        entry_type=EntryType.EXPENSE,
        category=candidate.category,
        subcategory=candidate.subcategory,
        default_amount=candidate.amount,
        frequency_interval=1,
        frequency_unit=candidate.unit,
        start_date=candidate.first_date,
        end_date=candidate.last_date if is_stopped else None,
        is_active=not is_stopped,
        match_key=candidate.merchant_key,
    )
    Transaction.objects.filter(pk__in=[t.pk for t in candidate.transactions]).update(recurring_item=item)
    return item


def dismiss_candidate(user, key):
    DismissedSubscription.objects.get_or_create(user=user, key=key)


def subscriptions_summary(user, today=None):
    """Compteurs pour le dashboard."""

    today = today or date.today()
    suggestions = subscription_suggestions(user, today)
    tracked = tracked_subscriptions(user, today)
    active = [row for row in tracked if row["status"] and row["status"]["status"] != "stopped"]
    return {
        "suggestion_count": len(suggestions["active"]),
        "stopped_count": sum(1 for row in tracked if row["status"] and row["status"]["status"] == "stopped"),
        "active_count": len(active),
        "monthly_total": money(sum((row["monthly_amount"] for row in active), Decimal("0"))),
    }


# ============================================================
# VISION D'UN MOIS : PAYÉS / À VENIR / NON PAYÉS
# ============================================================

def month_subscriptions(user, year, month, today=None):
    """Abonnements attendus sur un mois et leur état :
    payé, à venir, en attente (échéance passée depuis peu), non payé."""

    import calendar

    today = today or date.today()
    _, last_day = calendar.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)

    items = RecurringItem.objects.filter(
        user=user, entry_type=EntryType.EXPENSE, kind=RecurrenceKind.SUBSCRIPTION
    ).select_related("category")

    rows = []
    for item in items:
        payments = list(
            item.transactions.filter(transaction_date__gte=month_start, transaction_date__lte=month_end)
            .order_by("transaction_date")
        )
        if payments:
            paid = sum((p.amount for p in payments), Decimal("0"))
            rows.append({"item": item, "state": "paid", "label": "Payé", "date": payments[-1].transaction_date, "amount": money(paid)})
            continue

        start = item.start_date or month_start
        if start > month_end:
            continue

        unit = _item_unit(item)
        if unit == RecurrenceUnit.YEAR and start.month != month:
            continue

        due = date(year, month, min(start.day, last_day))
        grace = timedelta(days=CADENCES[unit]["grace"])

        if not item.is_active:
            # Arrêté : on ne signale que la première échéance manquée
            if item.end_date and item.end_date < month_start:
                first_missed = item.end_date + timedelta(days=_item_interval_days(item))
                if month_start <= first_missed <= month_end + grace:
                    rows.append({"item": item, "state": "stopped", "label": "Arrêté", "date": due, "amount": item.default_amount})
            continue

        if due > today:
            state, label = "upcoming", "À venir"
        elif today - due <= grace:
            state, label = "pending", "En attente"
        else:
            state, label = "missed", "Non payé"
        rows.append({"item": item, "state": state, "label": label, "date": due, "amount": item.default_amount})

    rows.sort(key=lambda row: row["date"])
    paid = [row for row in rows if row["state"] == "paid"]
    remaining = [row for row in rows if row["state"] in {"upcoming", "pending"}]
    problems = [row for row in rows if row["state"] in {"missed", "stopped"}]
    return {
        "paid": paid,
        "remaining": remaining,
        "problems": problems,
        "paid_total": money(sum((row["amount"] for row in paid), Decimal("0"))),
        "remaining_total": money(sum((row["amount"] for row in remaining), Decimal("0"))),
        "count": len(rows),
    }


# ============================================================
# SUIVRE UNE OPÉRATION COMME ABONNEMENT (CHOIX MANUEL)
# ============================================================

def track_transaction(transaction, unit=RecurrenceUnit.MONTH):
    """Déclare une opération comme abonnement, même si la détection ne
    l'a pas reconnue. Les paiements passés et futurs du même commerçant
    (montant proche) y sont rattachés. Renvoie (abonnement, nb de paiements)."""

    user = transaction.user
    key = transaction_key(transaction)

    item = None
    for existing in RecurringItem.objects.filter(user=user, entry_type=EntryType.EXPENSE).exclude(match_key=""):
        if merchant_root(existing.match_key) == key and amounts_match(transaction.amount, existing.default_amount):
            item = existing
            break

    matches = [
        t for t in Transaction.objects.filter(
            user=user,
            transaction_type=EntryType.EXPENSE,
            transaction_date__gte=date.today() - timedelta(days=HISTORY_DAYS),
        ).order_by("transaction_date")
        if t.pk == transaction.pk or (key and transaction_key(t) == key and amounts_match(t.amount, transaction.amount))
    ]

    if item is None:
        item = RecurringItem.objects.create(
            user=user,
            name=(transaction.description or key.title() or "Abonnement")[:150],
            kind=RecurrenceKind.SUBSCRIPTION,
            entry_type=EntryType.EXPENSE,
            category=transaction.category,
            subcategory=transaction.subcategory,
            default_amount=transaction.amount,
            frequency_interval=1,
            frequency_unit=unit,
            start_date=matches[0].transaction_date if matches else transaction.transaction_date,
            is_active=True,
            # Sans clé (libellé vide), seul ce paiement est rattaché
            match_key=key or f"operation-{transaction.pk}",
        )

    Transaction.objects.filter(pk__in=[t.pk for t in matches]).update(recurring_item=item)
    # Statut (actif / arrêté) et montant à jour
    sync_subscriptions(user)
    item.refresh_from_db()
    return item, len(matches)
