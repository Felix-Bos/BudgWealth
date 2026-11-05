"""
Prévision du solde du compte courant.

Point de départ : le dernier solde connu du compte courant (synchronisation
bancaire ou saisie), puis jour par jour jusqu'à l'horizon :
  - opérations déjà enregistrées après la date du solde (saisies à la main,
    postes fixes générés pour le mois) ;
  - postes fixes à venir (loyer, assurance… et revenus fixes) ;
  - abonnements suivis, à la date attendue d'après le dernier paiement ;
  - salaire, s'il n'est pas déjà un poste fixe : jour habituel et moyenne
    des derniers mois ;
  - dépenses courantes : moyenne quotidienne des 90 derniers jours, hors
    postes fixes, abonnements, épargne et tickets restaurant.
Les virements vers l'épargne ne sont pas prévus : la prévision dit justement
combien on peut mettre de côté sans passer sous la marge de sécurité.
"""

import calendar
import re
import statistics
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils.translation import gettext as _

from .bank_import import SAVINGS_ACCOUNT_RE
from .budget import SAVINGS_CATEGORY_NAME, ZERO, add_months, average_salary, money
from .models import (
    SALARY_CATEGORY_NAME,
    BalanceSource,
    CurrentAccount,
    CurrentAccountBalance,
    EntryType,
    RecurrenceKind,
    RecurrenceUnit,
    RecurringItem,
    Transaction,
)

SPENDING_WINDOW_DAYS = 90
STALE_AFTER_DAYS = 7
HORIZONS = {"mois": None, "30": 30, "60": 60, "90": 90}

CURRENT_ACCOUNT_RE = re.compile(r"compte\s+(de\s+d[ée]p[ôo]t|courant|ch[èe]que)", re.I)
SECURITIES_RE = re.compile(r"\bpea\b|en actions|titres", re.I)


# ============================================================
# SOLDE DU COMPTE COURANT
# ============================================================

def import_current_balance(user, balances):
    """Solde du compte courant lu dans l'export bancaire. Renvoie le
    CurrentAccountBalance enregistré, ou None."""

    candidates = [
        b for b in balances
        if not SAVINGS_ACCOUNT_RE.search(b.account or "") and not SECURITIES_RE.search(b.account or "")
    ]
    if not candidates:
        return None
    account = CurrentAccount.objects.filter(user=user).first()
    chosen = None
    if account and account.bank_account_number:
        chosen = next((b for b in candidates if b.account_number == account.bank_account_number), None)
    if chosen is None:
        chosen = next((b for b in candidates if CURRENT_ACCOUNT_RE.search(b.account or "")), candidates[0])
    if account is None:
        account = CurrentAccount.objects.create(
            user=user, name=(chosen.account or _("Compte courant"))[:100],
            bank_account_number=chosen.account_number or "",
        )
    elif not account.bank_account_number:
        account.bank_account_number = chosen.account_number or ""
        account.save(update_fields=["bank_account_number"])
    snapshot, _created = CurrentAccountBalance.objects.update_or_create(
        account=account, date=chosen.balance_date,
        defaults={"balance": chosen.balance, "source": BalanceSource.BANK},
    )
    return snapshot


def set_manual_balance(user, day, amount):
    account, _created = CurrentAccount.objects.get_or_create(user=user)
    snapshot, _created = CurrentAccountBalance.objects.update_or_create(
        account=account, date=day, defaults={"balance": amount, "source": BalanceSource.MANUAL},
    )
    return snapshot


# ============================================================
# ÉVÉNEMENTS À VENIR
# ============================================================

def _add_months(day, months, anchor_day=None):
    year, month = add_months(day.year, day.month, months)
    wanted = anchor_day or day.day
    return date(year, month, min(wanted, calendar.monthrange(year, month)[1]))


def _step(day, item):
    interval = item.frequency_interval or 1
    if item.frequency_unit == RecurrenceUnit.DAY:
        return day + timedelta(days=interval)
    if item.frequency_unit == RecurrenceUnit.WEEK:
        return day + timedelta(weeks=interval)
    if item.frequency_unit == RecurrenceUnit.YEAR:
        return _add_months(day, 12 * interval)
    return _add_months(day, interval)


def _signed(entry_type, amount):
    return amount if entry_type == EntryType.INCOME else -amount


def _event(day, label, amount, kind, detail=""):
    return {"date": day, "label": label, "amount": money(amount), "kind": kind, "detail": detail}


def _real_dated(queryset):
    return queryset.annotate(real_date=Coalesce("actual_date", "transaction_date"))


def _recurring_events(user, after, start, end):
    """Postes fixes saisis (hors abonnements détectés) entre start et end."""

    events = []
    items = RecurringItem.objects.filter(user=user, is_active=True, match_key="").select_related("category")
    for item in items:
        if item.category and item.category.name == SAVINGS_CATEGORY_NAME:
            continue
        day = item.start_date or start
        guard = 0
        while day < start and guard < 2000:
            day, guard = _step(day, item), guard + 1
        while day <= end:
            if item.end_date and day > item.end_date:
                break
            stored = Transaction.budget_date(item.entry_type, item.category, day)
            already = Transaction.objects.filter(
                user=user, recurring_item=item, transaction_date__in={day, stored}
            ).exists()
            if not already and day > after:
                kind = "income" if item.entry_type == EntryType.INCOME else (
                    "subscription" if item.kind == RecurrenceKind.SUBSCRIPTION else "fixed"
                )
                events.append(_event(day, item.name, _signed(item.entry_type, item.default_amount), kind))
            day = _step(day, item)
    return events


def _subscription_events(user, today, start, end):
    """Abonnements détectés sur le relevé : prochaine échéance d'après le
    dernier paiement ; une échéance en retard est attendue demain."""

    events = []
    items = RecurringItem.objects.filter(user=user, is_active=True, entry_type=EntryType.EXPENSE).exclude(match_key="")
    for item in items:
        last = _real_dated(item.transactions.all()).order_by("-real_date").first()
        if last is None:
            continue
        due = _step(last.real_date, item)
        late = False
        if due < start:
            # Paiement attendu mais pas encore vu : demain, s'il n'a pas trop de retard
            if (start - due).days > 10:
                continue
            due, late = start, True
        while due <= end:
            events.append(_event(
                due, item.name, -item.default_amount, "subscription",
                _("en retard sur la date habituelle") if late else "",
            ))
            late = False
            due = _step(due, item)
    return events


def _salary_events(user, today, start, end):
    """Salaire attendu, quand il n'est pas déjà un poste fixe."""

    if RecurringItem.objects.filter(
        user=user, is_active=True, entry_type=EntryType.INCOME, category__name=SALARY_CATEGORY_NAME, match_key=""
    ).exists():
        return []
    salaries = list(
        _real_dated(Transaction.objects.filter(
            user=user, transaction_type=EntryType.INCOME, category__name=SALARY_CATEGORY_NAME,
            transaction_date__gte=today - timedelta(days=130),
        )).order_by("-real_date")
    )
    if not salaries:
        return []
    amount, _months = average_salary(user, today.year, today.month, 3)
    if amount <= 0:
        return []
    anchor = int(statistics.median(t.real_date.day for t in salaries[:4]))
    last = salaries[0].real_date
    due = _add_months(last, 1, anchor)
    if due < start:
        if (start - due).days > 7:
            return []
        due = start
    events = []
    while due <= end:
        events.append(_event(due, _("Salaire (estimé)"), amount, "income", _("moyenne des 3 derniers mois")))
        due = _add_months(due, 1, anchor)
    return events


def daily_spending(user, today):
    """Dépenses courantes par jour : moyenne des 90 derniers jours."""

    since = today - timedelta(days=SPENDING_WINDOW_DAYS)
    total = (
        _real_dated(Transaction.objects.filter(
            user=user, transaction_type=EntryType.EXPENSE, recurring_item__isnull=True, paid_with_meal_voucher=False,
        ))
        .filter(real_date__gt=since, real_date__lte=today)
        .exclude(category__name=SAVINGS_CATEGORY_NAME)
        .aggregate(total=Sum("amount"))["total"]
    ) or ZERO
    return money(total / SPENDING_WINDOW_DAYS)


# ============================================================
# PRÉVISION
# ============================================================

def horizon_end(today, horizon):
    days = HORIZONS.get(horizon)
    if days:
        return today + timedelta(days=days)
    # Par défaut : fin du mois prochain si on est en fin de mois, sinon fin du mois
    last_day = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    if (last_day - today).days < 10:
        year, month = add_months(today.year, today.month, 1)
        return date(year, month, calendar.monthrange(year, month)[1])
    return last_day


def forecast(user, today=None, horizon="mois"):
    today = today or date.today()
    account = CurrentAccount.objects.filter(user=user).first()
    snapshot = account.latest_balance if account else None
    if snapshot is None:
        return {"has_balance": False, "account": account}

    end = horizon_end(today, horizon)
    start = today + timedelta(days=1)
    balance_date = snapshot.date

    # Opérations après la date du solde : déjà passées (jusqu'à aujourd'hui) ou datées dans le futur
    known = list(
        _real_dated(Transaction.objects.filter(user=user, paid_with_meal_voucher=False))
        .filter(real_date__gt=balance_date, real_date__lte=end)
        .select_related("category")
        .order_by("real_date")
    )
    since_balance = sum(
        (_signed(t.transaction_type, t.amount) for t in known if t.real_date <= today), ZERO
    )
    current = money(snapshot.balance + since_balance)

    events = [
        _event(t.real_date, t.description or str(t.category or ""), _signed(t.transaction_type, t.amount), "known")
        for t in known if t.real_date > today
    ]
    events += _recurring_events(user, max(today, balance_date), start, end)
    events += _subscription_events(user, today, start, end)
    events += _salary_events(user, today, start, end)
    events.sort(key=lambda e: (e["date"], -e["amount"]))

    per_day = daily_spending(user, today)
    by_day = {}
    for event in events:
        by_day.setdefault(event["date"], []).append(event)

    labels, values = [today.isoformat()], [float(current)]
    running = current
    low = {"date": today, "balance": current}
    first_negative = None
    month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    month_end_balance = current if month_end <= today else None
    day = start
    while day <= end:
        running -= per_day
        for event in by_day.get(day, []):
            running += event["amount"]
            event["after"] = money(running)
        if running < low["balance"]:
            low = {"date": day, "balance": money(running)}
        if running < 0 and first_negative is None:
            first_negative = day
        if day == month_end:
            month_end_balance = money(running)
        labels.append(day.isoformat())
        values.append(round(float(running), 2))
        day += timedelta(days=1)

    buffer = account.safety_buffer
    if low["balance"] < 0:
        status, label = "off", _("Risque de découvert")
    elif low["balance"] < buffer:
        status, label = "watch", _("Sous ta marge de sécurité")
    else:
        status, label = "on", _("Trésorerie confortable")

    income = sum((e["amount"] for e in events if e["amount"] > 0), ZERO)
    outflows = sum((e["amount"] for e in events if e["amount"] < 0), ZERO)
    days = (end - today).days
    return {
        "has_balance": True,
        "account": account,
        "snapshot": snapshot,
        "stale": (today - balance_date).days > STALE_AFTER_DAYS,
        "since_balance": money(since_balance),
        "current": current,
        "end": end,
        "end_balance": money(running),
        "month_end": month_end,
        "month_end_balance": month_end_balance,
        "low": low,
        "first_negative": first_negative,
        "buffer": buffer,
        "safe_to_save": money(max(ZERO, low["balance"] - buffer)),
        "status": status,
        "status_label": label,
        "events": events,
        "income_total": money(income),
        "outflow_total": money(-outflows),
        "per_day": per_day,
        "spending_total": money(per_day * days),
        "days": days,
        "chart": {"labels": labels, "values": values, "buffer": float(buffer)},
    }
