import calendar
import json
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.formats import date_format
from django.views.decorators.http import require_GET, require_POST

from accounts.forms import AccountForm, AccountPasswordChangeForm

from .bank_sync import is_month_syncable, start_bank_sync
from .models import BankSyncKind
from .forms import (
    BankConnectionForm,
    CategoryForm,
    RecurringItemForm,
    SubcategoryForm,
    TransactionForm,
)
from .models import (
    BankConnection,
    BankSync,
    BankSyncStatus,
    Category,
    EntryType,
    MonthlyBudget,
    RecurrenceKind,
    RecurrenceUnit,
    RecurringItem,
    Subcategory,
    Transaction,
    TransactionSource,
)


def _month_context(year, month):
    first_day = date(year, month, 1)
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return {
        "year": year,
        "month": month,
        "month_label": date_format(first_day, "F Y").capitalize(),
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
    }


def _add_months(base_date, months):
    target_month = base_date.month - 1 + months
    target_year = base_date.year + target_month // 12
    target_month = target_month % 12 + 1
    target_day = min(base_date.day, calendar.monthrange(target_year, target_month)[1])
    return date(target_year, target_month, target_day)


def _add_interval(base_date, interval, unit):
    if unit == RecurrenceUnit.DAY:
        return base_date.fromordinal(base_date.toordinal() + interval)
    if unit == RecurrenceUnit.WEEK:
        return base_date.fromordinal(base_date.toordinal() + interval * 7)
    if unit == RecurrenceUnit.YEAR:
        return _add_months(base_date, interval * 12)
    return _add_months(base_date, interval)


def _scheduled_dates_for_item(item, period_start, period_end):
    interval = item.frequency_interval or 1
    start_date = item.start_date or period_start
    end_date = item.end_date
    if end_date and end_date < period_end:
        period_end = end_date
    if start_date > period_end:
        return []

    current_date = start_date
    while current_date < period_start:
        current_date = _add_interval(current_date, interval, item.frequency_unit)

    dates = []
    while current_date <= period_end:
        dates.append(current_date)
        current_date = _add_interval(current_date, interval, item.frequency_unit)
    return dates


def _materialize_recurring_items(user, year, month, kinds=None):
    period_start = date(year, month, 1)
    period_end = date(year, month, calendar.monthrange(year, month)[1])
    budget = MonthlyBudget.objects.filter(user=user, year=year, month=month).first()
    created_count = 0

    items = RecurringItem.objects.filter(user=user, is_active=True)
    if kinds:
        items = items.filter(kind__in=kinds)
    for item in items:
        for scheduled_date in _scheduled_dates_for_item(item, period_start, period_end):
            if Transaction.objects.filter(
                user=user,
                recurring_item=item,
                transaction_date=scheduled_date,
            ).exists():
                continue
            Transaction.objects.create(
                user=user,
                monthly_budget=budget,
                recurring_item=item,
                transaction_type=item.entry_type,
                amount=item.default_amount,
                description=item.name,
                category=item.category,
                subcategory=item.subcategory,
                transaction_date=scheduled_date,
            )
            created_count += 1
    return created_count


def _subscription_calendar(user, today):
    period_start = date(today.year, today.month, 1)
    period_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    subscriptions = RecurringItem.objects.filter(
        user=user,
        is_active=True,
        kind=RecurrenceKind.SUBSCRIPTION,
        entry_type=EntryType.EXPENSE,
    ).select_related("category", "subcategory")

    events = []
    monthly_total = Decimal("0")
    for subscription in subscriptions:
        due_dates = _scheduled_dates_for_item(subscription, period_start, period_end)
        if not due_dates:
            continue
        for due_date in due_dates:
            monthly_total += subscription.default_amount
            events.append(
                {
                    "name": subscription.name,
                    "amount": subscription.default_amount,
                    "category": subscription.category.name
                    if subscription.category
                    else "Sans catégorie",
                    "day": due_date.day,
                    "date": due_date,
                    "frequency": subscription.frequency_label,
                }
            )

    events.sort(key=lambda event: (event["date"], event["name"]))
    days = []
    events_by_day = {}
    for event in events:
        events_by_day.setdefault(event["day"], []).append(event)
    for day in range(1, period_end.day + 1):
        day_events = events_by_day.get(day, [])
        days.append(
            {
                "day": day,
                "events": day_events,
                "total": sum((event["amount"] for event in day_events), Decimal("0")),
                "is_today": day == today.day,
            }
        )

    return {
        "events": events,
        "days": days,
        "monthly_total": monthly_total,
        "count": subscriptions.count(),
    }


DEMO_SUBSCRIPTION_BLUEPRINTS = [
    {
        "name": "Netflix",
        "amount": Decimal("15.99"),
        "category": "Loisirs & vacances",
        "day": 10,
        "start_offset": 0,
        "end_offset": None,
    },
    {
        "name": "Spotify",
        "amount": Decimal("10.99"),
        "category": "Loisirs & vacances",
        "day": 5,
        "start_offset": 0,
        "end_offset": None,
    },
    {
        "name": "iCloud+",
        "amount": Decimal("2.99"),
        "category": "High Tech",
        "day": 18,
        "start_offset": 0,
        "end_offset": None,
    },
    {
        "name": "Canal+",
        "amount": Decimal("24.99"),
        "category": "Loisirs & vacances",
        "day": 12,
        "start_offset": 1,
        "end_offset": 8,
    },
    {
        "name": "Adobe Photography",
        "amount": Decimal("11.99"),
        "category": "High Tech",
        "day": 22,
        "start_offset": 4,
        "end_offset": 10,
    },
    {
        "name": "Salle de sport",
        "amount": Decimal("29.90"),
        "category": "Santé",
        "day": 3,
        "start_offset": 2,
        "end_offset": 6,
    },
]


def _empty_subscription_calendar(today, count=0):
    period_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    return {
        "events": [],
        "days": [
            {"day": day, "events": [], "total": Decimal("0"), "is_today": day == today.day}
            for day in range(1, period_end.day + 1)
        ],
        "monthly_total": Decimal("0"),
        "count": count,
    }


def _demo_subscription_period(today):
    history_start_year, history_start_month, _ = _history_start(today)
    months = []
    cursor_year, cursor_month = history_start_year, history_start_month
    for offset in range(HISTORY_MONTHS):
        months.append(
            {
                "offset": offset,
                "date": date(cursor_year, cursor_month, 1),
                "year": cursor_year,
                "month": cursor_month,
                "label": date(cursor_year, cursor_month, 1).strftime("%b %Y").capitalize(),
            }
        )
        cursor_month += 1
        if cursor_month > 12:
            cursor_month = 1
            cursor_year += 1
    return months


def _demo_subscription_events_for_month(today, year, month):
    events = []
    for month_info in _demo_subscription_period(today):
        if month_info["year"] != year or month_info["month"] != month:
            continue
        _, last_day = calendar.monthrange(year, month)
        for subscription in DEMO_SUBSCRIPTION_BLUEPRINTS:
            start_offset = subscription["start_offset"]
            end_offset = subscription["end_offset"]
            month_offset = month_info["offset"]
            if month_offset < start_offset:
                continue
            if end_offset is not None and month_offset > end_offset:
                continue
            due_date = date(year, month, min(subscription["day"], last_day))
            events.append(
                {
                    "name": subscription["name"],
                    "amount": subscription["amount"],
                    "category": subscription["category"],
                    "day": due_date.day,
                    "date": due_date,
                    "frequency": "Tous les mois",
                    "is_cancelled": end_offset is not None and month_offset == end_offset,
                }
            )
        break
    return sorted(events, key=lambda event: (event["date"], event["name"]))


def _demo_subscription_calendar(today):
    events = _demo_subscription_events_for_month(today, today.year, today.month)
    if not events:
        return _empty_subscription_calendar(today)
    events_by_day = {}
    for event in events:
        events_by_day.setdefault(event["day"], []).append(event)
    _, last_day = calendar.monthrange(today.year, today.month)
    days = []
    for day in range(1, last_day + 1):
        day_events = events_by_day.get(day, [])
        days.append(
            {
                "day": day,
                "events": day_events,
                "total": sum((event["amount"] for event in day_events), Decimal("0")),
                "is_today": day == today.day,
            }
        )
    return {
        "events": events,
        "days": days,
        "monthly_total": sum((event["amount"] for event in events), Decimal("0")),
        "count": len(events),
    }


def _subscription_history(user, today):
    history_start_year, history_start_month, history_start = _history_start(today)
    monthly_totals = (
        Transaction.objects.filter(
            user=user,
            transaction_date__gte=history_start,
            recurring_item__kind=RecurrenceKind.SUBSCRIPTION,
            transaction_type=EntryType.EXPENSE,
        )
        .annotate(month=TruncMonth("transaction_date"))
        .values("month")
        .annotate(total=Sum("amount"), count=Count("recurring_item", distinct=True))
        .order_by("month")
    )
    totals_by_month = {row["month"]: row for row in monthly_totals}
    rows = []
    cursor_year, cursor_month = history_start_year, history_start_month
    for _ in range(HISTORY_MONTHS):
        key = date(cursor_year, cursor_month, 1)
        row = totals_by_month.get(key)
        rows.append(
            {
                "year": cursor_year,
                "month": cursor_month,
                "label": key.strftime("%b %Y").capitalize(),
                "total": (row["total"] if row else None) or Decimal("0"),
                "count": (row["count"] if row else None) or 0,
            }
        )
        cursor_month += 1
        if cursor_month > 12:
            cursor_month = 1
            cursor_year += 1
    return rows


def _subscription_roster(user):
    subscriptions = RecurringItem.objects.filter(
        user=user,
        kind=RecurrenceKind.SUBSCRIPTION,
        entry_type=EntryType.EXPENSE,
    ).select_related("category")
    rows = []
    today = date.today()
    for subscription in subscriptions:
        is_cancelled = subscription.is_cancelled or (
            subscription.end_date is not None and subscription.end_date <= today
        )
        rows.append(
            {
                "name": subscription.name,
                "amount": subscription.default_amount,
                "category": subscription.category.name
                if subscription.category
                else "Sans catégorie",
                "frequency": subscription.frequency_label,
                "start_date": subscription.start_date,
                "end_date": subscription.end_date,
                "status": "Annulé" if is_cancelled else "Actif",
                "is_cancelled": is_cancelled,
            }
        )
    return sorted(rows, key=lambda row: (row["is_cancelled"], row["name"]))


def _demo_subscription_history(today):
    rows = []
    for month_info in _demo_subscription_period(today):
        events = _demo_subscription_events_for_month(
            today, month_info["year"], month_info["month"]
        )
        rows.append(
            {
                "year": month_info["year"],
                "month": month_info["month"],
                "label": month_info["label"],
                "total": sum((event["amount"] for event in events), Decimal("0")),
                "count": len(events),
            }
        )
    return rows


def _demo_subscription_roster(today):
    period = _demo_subscription_period(today)
    rows = []
    for subscription in DEMO_SUBSCRIPTION_BLUEPRINTS:
        start_info = period[subscription["start_offset"]]
        start_date = date(
            start_info["year"],
            start_info["month"],
            min(
                subscription["day"],
                calendar.monthrange(start_info["year"], start_info["month"])[1],
            ),
        )
        end_date = None
        if subscription["end_offset"] is not None:
            end_info = period[subscription["end_offset"]]
            end_date = date(
                end_info["year"],
                end_info["month"],
                min(
                    subscription["day"],
                    calendar.monthrange(end_info["year"], end_info["month"])[1],
                ),
            )
        rows.append(
            {
                "name": subscription["name"],
                "amount": subscription["amount"],
                "category": subscription["category"],
                "frequency": "Tous les mois",
                "start_date": start_date,
                "end_date": end_date,
                "status": "Annulé" if end_date else "Actif",
                "is_cancelled": end_date is not None,
            }
        )
    return sorted(rows, key=lambda row: (row["is_cancelled"], row["name"]))


TRANSACTION_TONES = 6


def _transaction_row(transaction):
    """Données d'affichage d'une ligne, pour une Transaction ou une ligne de
    démonstration (dict)."""
    if isinstance(transaction, dict):
        is_income = transaction["type_label"] == "Revenu"
        category = transaction["category"] or ""
        subcategory = transaction["subcategory"] or ""
        row = {
            "pk": None,
            "description": transaction["description"],
            "transaction_date": transaction["transaction_date"],
            "amount": transaction["amount"],
            "is_preview": True,
            "is_bank": False,
            "bank_label": "",
        }
    else:
        is_income = transaction.transaction_type == EntryType.INCOME
        category = transaction.category.name if transaction.category else ""
        subcategory = transaction.subcategory.name if transaction.subcategory else ""
        row = {
            "pk": transaction.pk,
            "description": transaction.description,
            "transaction_date": transaction.transaction_date,
            "amount": transaction.amount,
            "is_preview": False,
            "is_bank": transaction.source == TransactionSource.BANK,
            "bank_label": transaction.bank_label,
        }

    description = row["description"] or category or "Sans description"
    row.update(
        {
            "description": description,
            "initial": description[:1].upper(),
            "is_income": is_income,
            "category": category,
            "subcategory": subcategory,
            # Couleur de la pastille : stable pour une même catégorie
            "tone": sum(map(ord, category or "?")) % TRANSACTION_TONES,
        }
    )
    return row


def _transactions_by_day(transactions):
    days = []
    for transaction in transactions:
        row = _transaction_row(transaction)
        if not days or days[-1]["date"] != row["transaction_date"]:
            days.append({"date": row["transaction_date"], "rows": [], "total": Decimal("0")})
        days[-1]["rows"].append(row)
        days[-1]["total"] += row["amount"] if row["is_income"] else -row["amount"]
    for day in days:
        day["total_abs"] = abs(day["total"])
    return days


@login_required
def index(request):
    today = date.today()
    return redirect("finance:month", year=today.year, month=today.month)


@login_required
def month_view(request, year, month):
    today = date.today()
    if not (1 <= month <= 12) or (year, month) > (today.year, today.month):
        return redirect("finance:month", year=today.year, month=today.month)

    _materialize_recurring_items(
        request.user, year, month, kinds=[RecurrenceKind.SUBSCRIPTION]
    )

    budget = MonthlyBudget.objects.filter(
        user=request.user, year=year, month=month
    ).first()

    transactions = Transaction.objects.filter(
        user=request.user,
        transaction_date__year=year,
        transaction_date__month=month,
    ).select_related("category", "subcategory", "recurring_item")
    has_transactions = transactions.exists()

    recurring_transactions = transactions.filter(recurring_item__isnull=False)
    other_transactions = transactions.filter(recurring_item__isnull=True)

    totals = transactions.aggregate(
        income=Sum("amount", filter=Q(transaction_type=EntryType.INCOME)),
        expense=Sum("amount", filter=Q(transaction_type=EntryType.EXPENSE)),
    )
    total_income = totals["income"] or Decimal("0")
    total_expense = totals["expense"] or Decimal("0")
    is_preview_data = False

    if not has_transactions and _active_history_month_count(request.user, today) < 3:
        demo_detail = _demo_month_detail(today, year, month)
        if demo_detail:
            is_preview_data = True
            recurring_transactions = []
            other_transactions = demo_detail["transactions"]
            total_income = demo_detail["totals"]["income"]
            total_expense = demo_detail["totals"]["expense"]

    _bank_connection = BankConnection.objects.filter(user=request.user).first()

    context = {
        **_month_context(year, month),
        "budget": budget,
        "recurring_rows": [_transaction_row(t) for t in recurring_transactions],
        "transaction_days": _transactions_by_day(other_transactions),
        "transaction_count": len(other_transactions),
        "total_income": total_income,
        "total_expense": total_expense,
        "balance": total_income - total_expense,
        "is_preview_data": is_preview_data,
        "transaction_form": TransactionForm(
            user=request.user,
            initial={"transaction_date": date(year, month, min(today.day, calendar.monthrange(year, month)[1]))},
        ),
        "has_recurring_items": RecurringItem.objects.filter(
            user=request.user, is_active=True
        ).exists(),
        "is_current_month": (year, month) == (today.year, today.month),
        "bank_connection": _bank_connection,
        "has_bank_connection": _bank_connection is not None,
        "bank_ready": bool(_bank_connection and _bank_connection.initial_import_done),
        "is_bank_syncable": is_month_syncable(year, month, today),
    }
    return render(request, "finance/month.html", context)


@login_required
@require_POST
def start_month(request, year, month):
    if not (1 <= month <= 12):
        return redirect("finance:index")

    budget, created = MonthlyBudget.objects.get_or_create(
        user=request.user, year=year, month=month
    )

    if created:
        created_count = _materialize_recurring_items(
            request.user,
            year,
            month,
            kinds=[RecurrenceKind.FIXED, RecurrenceKind.SUBSCRIPTION],
        )
        messages.success(
            request,
            f"Le mois a été démarré avec {created_count} ligne(s) récurrente(s).",
        )
    else:
        messages.info(request, "Ce mois est déjà démarré.")

    return redirect("finance:month", year=year, month=month)


@login_required
@require_POST
def add_transaction(request, year, month):
    form = TransactionForm(request.POST, user=request.user)
    if form.is_valid():
        transaction = form.save(commit=False)
        transaction.user = request.user
        transaction.monthly_budget = MonthlyBudget.objects.filter(
            user=request.user, year=year, month=month
        ).first()
        transaction.save()
        if form.cleaned_data.get("create_recurring"):
            recurring_name = transaction.description
            if not recurring_name and transaction.category:
                recurring_name = transaction.category.name
            recurring_item = RecurringItem.objects.create(
                user=request.user,
                name=recurring_name or "Ligne récurrente",
                kind=form.cleaned_data.get("recurring_kind") or RecurrenceKind.SUBSCRIPTION,
                entry_type=transaction.transaction_type,
                default_amount=transaction.amount,
                category=transaction.category,
                subcategory=transaction.subcategory,
                frequency_interval=form.cleaned_data.get("frequency_interval") or 1,
                frequency_unit=form.cleaned_data.get("frequency_unit")
                or RecurrenceUnit.MONTH,
                start_date=transaction.transaction_date,
                is_active=True,
            )
            transaction.recurring_item = recurring_item
            transaction.save(update_fields=["recurring_item"])
        messages.success(request, "Ligne ajoutée.")
    else:
        messages.error(request, "Impossible d'ajouter la ligne : vérifiez le formulaire.")
    return redirect("finance:month", year=year, month=month)


@login_required
@require_POST
def edit_transaction(request, year, month, pk):
    transaction = get_object_or_404(Transaction, pk=pk, user=request.user)
    form = TransactionForm(request.POST, instance=transaction, user=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, "Ligne mise à jour.")
    else:
        messages.error(request, "Impossible de mettre à jour la ligne.")
    return redirect("finance:month", year=year, month=month)


@login_required
@require_POST
def delete_transaction(request, year, month, pk):
    transaction = get_object_or_404(Transaction, pk=pk, user=request.user)
    transaction.delete()
    messages.success(request, "Ligne supprimée.")
    return redirect("finance:month", year=year, month=month)


HISTORY_MONTHS = 12


DEMO_MONTHLY_PROFILES = [
    {"income": Decimal("3280.00"), "expense": Decimal("2615.00")},
    {"income": Decimal("3280.00"), "expense": Decimal("2478.00")},
    {"income": Decimal("3340.00"), "expense": Decimal("2726.00")},
    {"income": Decimal("3280.00"), "expense": Decimal("2554.00")},
    {"income": Decimal("3425.00"), "expense": Decimal("2812.00")},
    {"income": Decimal("3280.00"), "expense": Decimal("2430.00")},
    {"income": Decimal("3280.00"), "expense": Decimal("2668.00")},
    {"income": Decimal("3560.00"), "expense": Decimal("2875.00")},
    {"income": Decimal("3340.00"), "expense": Decimal("2522.00")},
    {"income": Decimal("3280.00"), "expense": Decimal("2748.00")},
    {"income": Decimal("3280.00"), "expense": Decimal("2595.00")},
    {"income": Decimal("3660.00"), "expense": Decimal("2892.00")},
]

DEMO_CATEGORY_BREAKDOWN = [
    {"category__name": "Logement & charges", "total": Decimal("1185.00")},
    {"category__name": "Alimentation & restaurants", "total": Decimal("565.00")},
    {"category__name": "Transport", "total": Decimal("342.00")},
    {"category__name": "Épargne & placements", "total": Decimal("300.00")},
    {"category__name": "Loisirs & vacances", "total": Decimal("218.00")},
    {"category__name": "Santé", "total": Decimal("122.00")},
    {"category__name": "Achats & shopping", "total": Decimal("160.00")},
]

DEMO_EXPENSE_BLUEPRINTS = [
    {
        "description": "Loyer",
        "category": "Logement & charges",
        "subcategory": "Loyer",
        "amount": Decimal("1040.00"),
    },
    {
        "description": "Électricité et gaz",
        "category": "Logement & charges",
        "subcategory": "Eau, électricité, gaz",
        "amount": Decimal("92.00"),
    },
    {
        "description": "Internet et mobile",
        "category": "Logement & charges",
        "subcategory": "Internet, téléphonie",
        "amount": Decimal("53.00"),
    },
    {
        "description": "Courses alimentaires",
        "category": "Alimentation & restaurants",
        "subcategory": "Supermarché, épicerie",
        "amount": Decimal("405.00"),
    },
    {
        "description": "Restaurants et pauses déjeuner",
        "category": "Alimentation & restaurants",
        "subcategory": "Restaurants, snacks",
        "amount": Decimal("160.00"),
    },
    {
        "description": "Transport",
        "category": "Transport",
        "subcategory": "Transport en commun",
        "amount": Decimal("342.00"),
    },
    {
        "description": "Versement épargne",
        "category": "Épargne & placements",
        "subcategory": "Livret",
        "amount": Decimal("300.00"),
    },
    {
        "description": "Loisirs et abonnements",
        "category": "Loisirs & vacances",
        "subcategory": "Sorties culturelles",
        "amount": Decimal("218.00"),
    },
    {
        "description": "Santé",
        "category": "Santé",
        "subcategory": "Pharmacie",
        "amount": Decimal("122.00"),
    },
    {
        "description": "Achats ponctuels",
        "category": "Achats & shopping",
        "subcategory": "Vêtements, chaussures, accessoires",
        "amount": Decimal("160.00"),
    },
]


def _history_start(today):
    history_start_year = today.year
    history_start_month = today.month - (HISTORY_MONTHS - 1)
    while history_start_month <= 0:
        history_start_month += 12
        history_start_year -= 1
    return history_start_year, history_start_month, date(
        history_start_year, history_start_month, 1
    )


def _rolling_history_rows(history_start_year, history_start_month, totals_by_month):
    history = []
    cursor_year, cursor_month = history_start_year, history_start_month
    for _ in range(HISTORY_MONTHS):
        key = date(cursor_year, cursor_month, 1)
        row = totals_by_month.get(key)
        income = (row["income"] if row else None) or Decimal("0")
        expense = (row["expense"] if row else None) or Decimal("0")
        history.append(
            {
                "year": cursor_year,
                "month": cursor_month,
                "label": key.strftime("%b %Y").capitalize(),
                "income": income,
                "expense": expense,
                "balance": income - expense,
            }
        )
        cursor_month += 1
        if cursor_month > 12:
            cursor_month = 1
            cursor_year += 1
    return history


def _demo_history(today):
    history_start_year, history_start_month, _ = _history_start(today)
    totals_by_month = {}
    cursor_year, cursor_month = history_start_year, history_start_month
    for profile in DEMO_MONTHLY_PROFILES:
        key = date(cursor_year, cursor_month, 1)
        totals_by_month[key] = profile
        cursor_month += 1
        if cursor_month > 12:
            cursor_month = 1
            cursor_year += 1
    return _rolling_history_rows(history_start_year, history_start_month, totals_by_month)


def _active_history_month_count(user, today):
    history_start_year, history_start_month, history_start = _history_start(today)
    monthly_totals = (
        Transaction.objects.filter(user=user, transaction_date__gte=history_start)
        .annotate(month=TruncMonth("transaction_date"))
        .values("month")
        .annotate(
            income=Sum("amount", filter=Q(transaction_type=EntryType.INCOME)),
            expense=Sum("amount", filter=Q(transaction_type=EntryType.EXPENSE)),
        )
    )
    totals_by_month = {row["month"]: row for row in monthly_totals}
    history = _rolling_history_rows(
        history_start_year, history_start_month, totals_by_month
    )
    return sum(1 for row in history if row["income"] > 0 or row["expense"] > 0)


def _demo_month_detail(today, year, month):
    demo_rows = _demo_history(today)
    for index, row in enumerate(demo_rows):
        if row["year"] == year and row["month"] == month:
            return {
                "totals": row,
                "transactions": _demo_transactions_for_month(year, month, index, row),
            }
    return None


def _demo_transactions_for_month(year, month, index, totals):
    _, last_day = calendar.monthrange(year, month)
    transactions = [
        {
            "is_preview": True,
            "description": "Salaire net",
            "type_label": "Revenu",
            "category": "Salaire",
            "subcategory": "",
            "transaction_date": date(year, month, min(1, last_day)),
            "amount": min(totals["income"], Decimal("3280.00")),
        }
    ]
    income_extra = totals["income"] - transactions[0]["amount"]
    if income_extra > 0:
        transactions.append(
            {
                "is_preview": True,
                "description": "Prime ou mission ponctuelle",
                "type_label": "Revenu",
                "category": "Freelance, indépendant",
                "subcategory": "",
                "transaction_date": date(year, month, min(15, last_day)),
                "amount": income_extra,
            }
        )

    base_total = sum((item["amount"] for item in DEMO_EXPENSE_BLUEPRINTS), Decimal("0"))
    factor = totals["expense"] / base_total if base_total else Decimal("1")
    expense_dates = [1, 3, 5, 8, 11, 14, 18, 21, 24, 27]
    expense_total = Decimal("0")
    for expense_index, item in enumerate(DEMO_EXPENSE_BLUEPRINTS):
        if expense_index == len(DEMO_EXPENSE_BLUEPRINTS) - 1:
            amount = totals["expense"] - expense_total
        else:
            amount = (item["amount"] * factor).quantize(Decimal("0.01"))
            expense_total += amount
        transactions.append(
            {
                "is_preview": True,
                "description": item["description"],
                "type_label": "Dépense",
                "category": item["category"],
                "subcategory": item["subcategory"],
                "transaction_date": date(
                    year,
                    month,
                    min(expense_dates[expense_index] + (index % 3), last_day),
                ),
                "amount": amount,
            }
        )
    return sorted(transactions, key=lambda item: item["transaction_date"], reverse=True)


@login_required
def dashboard_hub_view(request):
    return render(request, "finance/dashboard_hub.html")


@login_required
def dashboard_budget_view(request):
    today = date.today()
    _materialize_recurring_items(
        request.user,
        today.year,
        today.month,
        kinds=[RecurrenceKind.SUBSCRIPTION],
    )

    history_start_year, history_start_month, history_start = _history_start(today)

    current_transactions = Transaction.objects.filter(
        user=request.user,
        transaction_date__year=today.year,
        transaction_date__month=today.month,
    )
    current_totals = current_transactions.aggregate(
        income=Sum("amount", filter=Q(transaction_type=EntryType.INCOME)),
        expense=Sum("amount", filter=Q(transaction_type=EntryType.EXPENSE)),
    )
    current_income = current_totals["income"] or Decimal("0")
    current_expense = current_totals["expense"] or Decimal("0")

    category_breakdown = list(
        current_transactions.filter(transaction_type=EntryType.EXPENSE)
        .values("category__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )

    monthly_totals = (
        Transaction.objects.filter(
            user=request.user, transaction_date__gte=history_start
        )
        .annotate(month=TruncMonth("transaction_date"))
        .values("month")
        .annotate(
            income=Sum("amount", filter=Q(transaction_type=EntryType.INCOME)),
            expense=Sum("amount", filter=Q(transaction_type=EntryType.EXPENSE)),
        )
        .order_by("month")
    )
    totals_by_month = {row["month"]: row for row in monthly_totals}

    history = _rolling_history_rows(
        history_start_year, history_start_month, totals_by_month
    )
    active_month_count = sum(
        1 for row in history if row["income"] > 0 or row["expense"] > 0
    )
    is_preview_data = active_month_count < 3
    if is_preview_data:
        history = _demo_history(today)
        category_breakdown = DEMO_CATEGORY_BREAKDOWN
        current_income = history[-1]["income"]
        current_expense = history[-1]["expense"]

    subscription_calendar = _subscription_calendar(request.user, today)
    subscription_history = _subscription_history(request.user, today)
    subscription_roster = _subscription_roster(request.user)
    if is_preview_data and subscription_calendar["count"] == 0:
        subscription_calendar = _demo_subscription_calendar(today)
        subscription_history = _demo_subscription_history(today)
        subscription_roster = _demo_subscription_roster(today)

    current_balance = current_income - current_expense
    average_expense = (
        sum((row["expense"] for row in history), Decimal("0")) / len(history)
        if history
        else Decimal("0")
    )
    savings_rate = (
        int((current_balance / current_income) * Decimal("100"))
        if current_income > 0
        else 0
    )

    context = {
        "month_label": today.strftime("%B %Y").capitalize(),
        "current_income": current_income,
        "current_expense": current_expense,
        "current_balance": current_balance,
        "average_expense": average_expense.quantize(Decimal("1")),
        "savings_rate": savings_rate,
        "is_preview_data": is_preview_data,
        "category_breakdown": category_breakdown,
        "subscription_calendar": subscription_calendar,
        "subscription_history": subscription_history,
        "subscription_roster": subscription_roster,
        "subscription_history_labels_json": json.dumps(
            [row["label"] for row in subscription_history]
        ),
        "subscription_history_totals_json": json.dumps(
            [str(row["total"]) for row in subscription_history]
        ),
        "subscription_history_counts_json": json.dumps(
            [row["count"] for row in subscription_history]
        ),
        "history": history,
        "history_labels_json": json.dumps([row["label"] for row in history]),
        "history_income_json": json.dumps([str(row["income"]) for row in history]),
        "history_expense_json": json.dumps([str(row["expense"]) for row in history]),
        "history_balance_json": json.dumps([str(row["balance"]) for row in history]),
        "category_labels_json": json.dumps(
            [row["category__name"] or "Sans catégorie" for row in category_breakdown]
        ),
        "category_values_json": json.dumps(
            [str(row["total"]) for row in category_breakdown]
        ),
    }
    return render(request, "finance/dashboard_budget.html", context)


@login_required
def settings_view(request):
    active_tab = request.GET.get("tab", "compte")
    if active_tab not in {"compte", "banque", "postes-fixes", "categories"}:
        active_tab = "compte"

    items = RecurringItem.objects.filter(user=request.user).select_related(
        "category", "subcategory"
    )
    user_categories = Category.objects.filter(user=request.user).prefetch_related(
        "subcategories"
    )

    context = {
        "active_tab": active_tab,
        "account_form": AccountForm(instance=request.user),
        "password_form": AccountPasswordChangeForm(user=request.user),
        "items": items,
        "recurring_item_form": RecurringItemForm(user=request.user),
        "categories": user_categories,
        "category_form": CategoryForm(user=request.user),
        "subcategory_form": SubcategoryForm(user=request.user),
    }

    bank_connection = BankConnection.objects.filter(user=request.user).first()
    context.update(
        {
            "bank_connection": bank_connection,
            "bank_form": BankConnectionForm(instance=bank_connection),
            "bank_syncs": BankSync.objects.filter(user=request.user)[:5],
        }
    )
    return render(request, "finance/settings.html", context)


@login_required
@require_POST
def update_account(request):
    form = AccountForm(request.POST, instance=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, "Vos informations ont été mises à jour.")
    else:
        messages.error(request, "Impossible de mettre à jour vos informations.")
    return redirect(f"{reverse('finance:settings')}?tab=compte")


@login_required
@require_POST
def update_password(request):
    form = AccountPasswordChangeForm(user=request.user, data=request.POST)
    if form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(request, "Votre mot de passe a été mis à jour.")
    else:
        messages.error(request, "Impossible de mettre à jour le mot de passe.")
    return redirect(f"{reverse('finance:settings')}?tab=compte")


@login_required
@require_POST
def add_recurring_item(request):
    form = RecurringItemForm(request.POST, user=request.user)
    if form.is_valid():
        item = form.save(commit=False)
        item.user = request.user
        if not item.start_date:
            item.start_date = date.today()
        if item.end_date and item.end_date <= date.today():
            item.is_active = False
        item.save()
        messages.success(request, "Élément récurrent ajouté.")
    else:
        messages.error(request, "Impossible d'ajouter l'élément récurrent.")
    return redirect(f"{reverse('finance:settings')}?tab=postes-fixes")


@login_required
@require_POST
def edit_recurring_item(request, pk):
    item = get_object_or_404(RecurringItem, pk=pk, user=request.user)
    form = RecurringItemForm(request.POST, instance=item, user=request.user)
    if form.is_valid():
        item = form.save(commit=False)
        if not item.start_date:
            item.start_date = date.today()
        if item.end_date and item.end_date <= date.today():
            item.is_active = False
        item.save()
        messages.success(request, "Élément récurrent mis à jour.")
    else:
        messages.error(request, "Impossible de mettre à jour l'élément récurrent.")
    return redirect(f"{reverse('finance:settings')}?tab=postes-fixes")


@login_required
@require_POST
def cancel_recurring_item(request, pk):
    item = get_object_or_404(
        RecurringItem,
        pk=pk,
        user=request.user,
        kind=RecurrenceKind.SUBSCRIPTION,
    )
    cancellation_date = request.POST.get("end_date")
    if cancellation_date:
        try:
            year, month, day = [int(part) for part in cancellation_date.split("-")]
            end_date = date(year, month, day)
        except (TypeError, ValueError):
            end_date = date.today()
    else:
        end_date = date.today()

    if item.start_date and end_date < item.start_date:
        end_date = item.start_date
    item.end_date = end_date
    item.is_active = False
    item.save(update_fields=["end_date", "is_active"])
    messages.success(request, "Abonnement annulé. L'historique reste conservé.")
    return redirect(f"{reverse('finance:settings')}?tab=postes-fixes")


@login_required
@require_POST
def delete_recurring_item(request, pk):
    item = get_object_or_404(RecurringItem, pk=pk, user=request.user)
    item.delete()
    messages.success(request, "Élément récurrent supprimé.")
    return redirect(f"{reverse('finance:settings')}?tab=postes-fixes")


@login_required
@require_POST
def add_category(request):
    form = CategoryForm(request.POST, user=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, "Catégorie ajoutée.")
    else:
        messages.error(request, "Impossible d'ajouter la catégorie.")
    return redirect(f"{reverse('finance:settings')}?tab=categories")


@login_required
@require_POST
def delete_category(request, pk):
    category = get_object_or_404(Category, pk=pk, user=request.user)
    category.delete()
    messages.success(request, "Catégorie supprimée.")
    return redirect(f"{reverse('finance:settings')}?tab=categories")


@login_required
@require_POST
def add_subcategory(request):
    form = SubcategoryForm(request.POST, user=request.user)
    if form.is_valid():
        subcategory = form.save(commit=False)
        if subcategory.category.user_id != request.user.id:
            messages.error(request, "Catégorie invalide.")
        else:
            subcategory.save()
            messages.success(request, "Sous-catégorie ajoutée.")
    else:
        messages.error(request, "Impossible d'ajouter la sous-catégorie.")
    return redirect(f"{reverse('finance:settings')}?tab=categories")


@login_required
@require_POST
def delete_subcategory(request, pk):
    subcategory = get_object_or_404(
        Subcategory, pk=pk, category__user=request.user
    )
    subcategory.delete()
    messages.success(request, "Sous-catégorie supprimée.")
    return redirect(f"{reverse('finance:settings')}?tab=categories")


@login_required
@require_POST
def update_bank_connection(request):
    bank_connection = BankConnection.objects.filter(user=request.user).first()
    form = BankConnectionForm(request.POST, instance=bank_connection)
    if form.is_valid():
        bank_connection = form.save(commit=False)
        bank_connection.user = request.user
        bank_connection.save()
        # Première connexion : on enchaîne sur l'import des 12 mois d'historique
        if not bank_connection.initial_import_done:
            return redirect("finance:bank_initial_sync")
        messages.success(request, "Connexion bancaire enregistrée.")
    else:
        errors = " ".join(error for field_errors in form.errors.values() for error in field_errors)
        messages.error(request, f"Impossible d'enregistrer la connexion bancaire. {errors}")
    return redirect(f"{reverse('finance:settings')}?tab=banque")


@login_required
@require_POST
def delete_bank_connection(request):
    BankConnection.objects.filter(user=request.user).delete()
    messages.success(request, "Connexion bancaire supprimée.")
    return redirect(f"{reverse('finance:settings')}?tab=banque")


def _bank_sync_payload(sync):
    return {
        "id": sync.pk,
        "status": sync.status,
        "label": sync.get_status_display(),
        "message": sync.message,
        "created": sync.created_count,
        "skipped": sync.skipped_count,
        "finished": sync.is_finished,
    }


@login_required
@require_POST
def bank_sync_start(request, year, month):
    if not BankConnection.objects.filter(user=request.user).exists():
        return JsonResponse(
            {"error": "Renseignez d'abord votre identifiant bancaire dans les paramètres."},
            status=400,
        )
    if not is_month_syncable(year, month):
        return JsonResponse(
            {"error": "La banque ne donne accès qu'aux 12 derniers mois."},
            status=400,
        )
    sync = start_bank_sync(request.user, year, month)
    return JsonResponse(_bank_sync_payload(sync))


@login_required
def bank_connect(request):
    """Étape de connexion bancaire (à l'inscription ou depuis les paramètres).
    L'utilisateur saisit code postal + identifiant, ou passe l'étape."""
    connection = BankConnection.objects.filter(user=request.user).first()

    # Déjà connecté et historique importé : rien à faire ici
    if connection and connection.initial_import_done:
        return redirect(f"{reverse('finance:settings')}?tab=banque")

    if request.method == "POST":
        form = BankConnectionForm(request.POST, instance=connection)
        if form.is_valid():
            connection = form.save(commit=False)
            connection.user = request.user
            connection.save()
            return redirect("finance:bank_initial_sync")
    else:
        form = BankConnectionForm(instance=connection)

    return render(request, "finance/bank_connect.html", {"bank_form": form})


@login_required
def bank_initial_sync(request):
    """Écran d'import de l'historique (12 mois), après la première connexion."""
    connection = BankConnection.objects.filter(user=request.user).first()
    if not connection:
        return redirect("finance:bank_connect")
    if connection.initial_import_done:
        return redirect("finance:dashboard")
    return render(request, "finance/bank_initial_sync.html")


@login_required
@require_POST
def bank_initial_sync_start(request):
    connection = BankConnection.objects.filter(user=request.user).first()
    if not connection:
        return JsonResponse(
            {"error": "Renseignez d'abord votre identifiant bancaire."}, status=400
        )
    if connection.initial_import_done:
        return JsonResponse(
            {"error": "Votre historique a déjà été importé."}, status=400
        )
    sync = start_bank_sync(request.user, kind=BankSyncKind.INITIAL)
    return JsonResponse(_bank_sync_payload(sync))


@login_required
@require_GET
def bank_sync_status(request, pk):
    sync = get_object_or_404(BankSync, pk=pk, user=request.user)
    if sync.status == BankSyncStatus.DONE and request.GET.get("final"):
        messages.success(
            request,
            f"Synchronisation terminée : {sync.created_count} opération(s) importée(s)"
            + (f", {sync.skipped_count} déjà présente(s)." if sync.skipped_count else "."),
        )
    return JsonResponse(_bank_sync_payload(sync))
