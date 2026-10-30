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

from . import budget as budget_calc
from . import home
from . import subscriptions as subscription_detection
from .bank_sync import is_month_syncable, start_bank_sync
from .models import BankSyncKind
from .forms import (
    BankConnectionForm,
    BudgetLineFormSet,
    BudgetPlanForm,
    CategoryForm,
    MealVoucherSettingsForm,
    RecurringItemForm,
    SavingsGoalForm,
    SavingsProjectForm,
    SubcategoryForm,
    TransactionForm,
    month_label,
)
from .models import (
    BankConnection,
    BankSync,
    BankSyncStatus,
    BudgetLine,
    BudgetPlan,
    BudgetSection,
    Category,
    EntryType,
    MealVoucherSettings,
    MonthlyBudget,
    SavingsProject,
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

    # Les abonnements détectés sur le relevé ne génèrent pas de ligne :
    # le paiement réel arrive par l'import bancaire.
    items = RecurringItem.objects.filter(user=user, is_active=True, match_key="")
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
                "label": f"{budget_calc.SHORT_MONTHS[cursor_month - 1]} {cursor_year}",
                "total": budget_calc.money(row["total"] if row else None),
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
        is_savings = budget_calc.is_savings_transaction(transaction)
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
            "is_savings": is_savings,
            "raw_description": transaction.description,
            "subscription": transaction.recurring_item.name
            if transaction.recurring_item_id and transaction.recurring_item.kind == RecurrenceKind.SUBSCRIPTION
            else "",
            "category_id": transaction.category_id or "",
            "subcategory_id": transaction.subcategory_id or "",
            "meal_voucher": transaction.paid_with_meal_voucher,
            "project": transaction.savings_project.name if transaction.savings_project_id else "",
        }

    description = row["description"] or category or "Sans description"
    row.update(
        {
            "description": description,
            "initial": description[:1].upper(),
            "is_income": is_income,
            "kind": "income" if is_income else ("savings" if row.get("is_savings") else "expense"),
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
    ).select_related("category", "subcategory", "recurring_item", "savings_project")
    has_transactions = transactions.exists()

    recurring_transactions = transactions.filter(recurring_item__isnull=False)
    other_transactions = transactions.filter(recurring_item__isnull=True)

    totals = budget_calc.split_totals(
        transactions.aggregate(**budget_calc.totals_annotations())
    )
    total_income = totals["income"]
    total_expense = totals["expense"]
    is_preview_data = False

    _bank_connection = BankConnection.objects.filter(user=request.user).first()

    subscription_detection.sync_subscriptions(request.user, today)
    tracking = budget_calc.month_tracking(request.user, year, month, today)
    all_transactions = list(transactions)
    daily = budget_calc.daily_cumulative(
        all_transactions, year, month, today,
        budget_total=tracking["budget_total"] if tracking else None,
    )
    kind_totals = {
        "income": {"count": 0, "total": Decimal("0")},
        "expense": {"count": 0, "total": Decimal("0")},
        "savings": {"count": 0, "total": Decimal("0")},
    }
    for transaction in all_transactions:
        kind = _transaction_row(transaction)["kind"]
        kind_totals[kind]["count"] += 1
        kind_totals[kind]["total"] += transaction.amount

    context = {
        **_month_context(year, month),
        "tracking": tracking,
        "kind_totals": kind_totals,
        "month_subscriptions": subscription_detection.month_subscriptions(request.user, year, month, today),
        "category_tree_data": budget_calc.category_tree(all_transactions),
        "editor_categories": [
            {
                "id": category.pk,
                "name": category.name,
                "type": category.entry_type,
                "subs": [{"id": sub.pk, "name": sub.name} for sub in category.subcategories.all()],
            }
            for category in Category.objects.filter(user=request.user).prefetch_related("subcategories")
        ],
        "daily_data": daily,
        "has_charges": total_expense > 0,
        "budget": budget,
        "recurring_rows": [_transaction_row(t) for t in recurring_transactions],
        "transaction_days": _transactions_by_day(other_transactions),
        "transaction_count": len(other_transactions),
        "total_income": total_income,
        "total_expense": total_expense,
        "total_savings": totals["savings"],
        "balance": totals["balance"],
        "after_savings": totals["after_savings"],
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


def _wants_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _month_live_payload(user, year, month, today=None):
    """Totaux et données des graphiques d'un mois, renvoyés après une
    modification faite sans recharger la page."""

    today = today or date.today()
    transactions = list(
        Transaction.objects.filter(
            user=user, transaction_date__year=year, transaction_date__month=month
        ).select_related("category", "subcategory", "savings_project")
    )
    totals = {"income": Decimal("0"), "expense": Decimal("0"), "savings": Decimal("0")}
    counts = {"income": 0, "expense": 0, "savings": 0}
    for transaction in transactions:
        kind = _transaction_row(transaction)["kind"]
        totals[kind] += transaction.amount
        counts[kind] += 1
    tracking = budget_calc.month_tracking(user, year, month, today)
    return {
        "totals": {key: str(budget_calc.money(value)) for key, value in totals.items()},
        "balance": str(budget_calc.money(totals["income"] - totals["expense"])),
        "counts": counts,
        "categoryTree": budget_calc.category_tree(transactions),
        "daily": budget_calc.daily_cumulative(
            transactions, year, month, today,
            budget_total=tracking["budget_total"] if tracking else None,
        ),
    }


def _row_payload(transaction):
    row = _transaction_row(transaction)
    return {
        "pk": transaction.pk,
        "description": row["description"],
        "rawDescription": transaction.description,
        "initial": row["initial"],
        "kind": row["kind"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "categoryId": transaction.category_id,
        "subcategoryId": transaction.subcategory_id,
        "tone": row["tone"],
    }


@login_required
@require_POST
def update_transaction_details(request, year, month, pk):
    """Intitulé, catégorie et sous-catégorie, modifiés depuis la liste."""

    transaction = get_object_or_404(Transaction, pk=pk, user=request.user)

    category = None
    category_id = request.POST.get("category") or ""
    if category_id:
        category = Category.objects.filter(
            pk=category_id, user=request.user, entry_type=transaction.transaction_type
        ).first()
        if category is None:
            return JsonResponse({"ok": False, "error": "Catégorie invalide."}, status=400)

    subcategory = None
    subcategory_id = request.POST.get("subcategory") or ""
    if subcategory_id and category:
        subcategory = Subcategory.objects.filter(pk=subcategory_id, category=category).first()

    transaction.description = (request.POST.get("description") or "").strip()[:255]
    transaction.category = category
    transaction.subcategory = subcategory
    # Plus une dépense alimentaire / un versement d'épargne : on retire les marqueurs
    if not category or category.name != budget_calc.FOOD_CATEGORY_NAME:
        transaction.paid_with_meal_voucher = False
    if not category or category.name != budget_calc.SAVINGS_CATEGORY_NAME:
        transaction.savings_project = None
    transaction.save()

    if not _wants_json(request):
        messages.success(request, "Opération mise à jour.")
        return redirect("finance:month", year=year, month=month)

    return JsonResponse({
        "ok": True,
        "row": _row_payload(transaction),
        "month": _month_live_payload(request.user, year, month),
    })


@login_required
@require_POST
def track_transaction_subscription(request, year, month, pk):
    """Déclare une opération comme abonnement depuis la liste du mois."""

    transaction = get_object_or_404(
        Transaction, pk=pk, user=request.user, transaction_type=EntryType.EXPENSE
    )
    unit = RecurrenceUnit.YEAR if request.POST.get("unit") == RecurrenceUnit.YEAR else RecurrenceUnit.MONTH
    item, count = subscription_detection.track_transaction(transaction, unit)

    message = f"« {item.name} » est suivi comme abonnement ({count} paiement{'s' if count > 1 else ''} rattaché{'s' if count > 1 else ''})."
    if not _wants_json(request):
        messages.success(request, message)
        return redirect("finance:month", year=year, month=month)
    return JsonResponse({"ok": True, "message": message, "name": item.name})


@login_required
@require_POST
def delete_transaction(request, year, month, pk):
    transaction = get_object_or_404(Transaction, pk=pk, user=request.user)
    transaction.delete()
    if _wants_json(request):
        return JsonResponse({"ok": True, "month": _month_live_payload(request.user, year, month)})
    messages.success(request, "Ligne supprimée.")
    return redirect("finance:month", year=year, month=month)


HISTORY_MONTHS = 12


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
        history.append(
            {
                "year": cursor_year,
                "month": cursor_month,
                "label": f"{budget_calc.SHORT_MONTHS[cursor_month - 1]} {cursor_year}",
                **budget_calc.split_totals(row),
            }
        )
        cursor_month += 1
        if cursor_month > 12:
            cursor_month = 1
            cursor_year += 1
    return history


def _active_history_month_count(user, today):
    history_start_year, history_start_month, history_start = _history_start(today)
    monthly_totals = (
        Transaction.objects.filter(user=user, transaction_date__gte=history_start)
        .annotate(month=TruncMonth("transaction_date"))
        .values("month")
        .annotate(**budget_calc.totals_annotations())
    )
    totals_by_month = {row["month"]: row for row in monthly_totals}
    history = _rolling_history_rows(
        history_start_year, history_start_month, totals_by_month
    )
    return sum(1 for row in history if row["income"] > 0 or row["expense"] > 0)


def _monthly_history(user, today):
    """Revenus, charges, épargne et taux d'épargne des 12 derniers mois,
    le mois en cours en dernier."""

    history_start_year, history_start_month, history_start = _history_start(today)
    monthly_totals = (
        Transaction.objects.filter(user=user, transaction_date__gte=history_start)
        .annotate(month=TruncMonth("transaction_date"))
        .values("month")
        .annotate(**budget_calc.totals_annotations())
        .order_by("month")
    )
    history = _rolling_history_rows(
        history_start_year, history_start_month, {row["month"]: row for row in monthly_totals}
    )
    for row in history:
        row["savings_rate"] = budget_calc.percent(row["savings"], row["income"])
        row["is_current"] = (row["year"], row["month"]) == (today.year, today.month)
    return history


@login_required
def dashboard_hub_view(request):
    """Accueil : patrimoine, mois en cours et conseils personnalisés."""

    today = date.today()
    _materialize_recurring_items(
        request.user, today.year, today.month, kinds=[RecurrenceKind.SUBSCRIPTION]
    )
    subscription_detection.sync_subscriptions(request.user, today)

    history = _monthly_history(request.user, today)
    summary = home.home_summary(request.user, today, history)
    recent = (
        Transaction.objects.filter(user=request.user, transaction_date__lte=today)
        .select_related("category", "subcategory", "recurring_item", "savings_project")[:6]
    )
    last_months = history[-6:]

    context = {
        **summary,
        **_month_context(today.year, today.month),
        "today": today,
        "recent_rows": [_transaction_row(transaction) for transaction in recent],
        "home_data": {
            "history": {
                "labels": [row["label"] for row in last_months],
                "income": [str(row["income"]) for row in last_months],
                "expense": [str(row["expense"]) for row in last_months],
                "savings": [str(row["savings"]) for row in last_months],
                "months": [[row["year"], row["month"]] for row in last_months],
            },
            "wealth": summary["wealth"]["history"],
            "monthUrl": reverse("finance:month", args=[2000, 1]).replace("2000/1/", ""),
        },
    }
    return render(request, "finance/dashboard_hub.html", context)


@login_required
def dashboard_budget_view(request):
    """Vision globale : tendances sur l'historique, réussite du budget et du
    plan d'épargne. Le détail d'un mois est sur la page du mois."""

    today = date.today()
    _materialize_recurring_items(
        request.user, today.year, today.month, kinds=[RecurrenceKind.SUBSCRIPTION]
    )
    subscription_detection.sync_subscriptions(request.user, today)

    _, _, history_start = _history_start(today)
    history_transactions = Transaction.objects.filter(
        user=request.user, transaction_date__gte=history_start
    )
    history = _monthly_history(request.user, today)

    # Indicateurs sur les mois terminés qui ont des opérations
    closed = [
        row for row in history
        if not row["is_current"] and (row["income"] or row["expense"] or row["savings"])
    ]
    months = len(closed) or 1
    total_income = sum((row["income"] for row in closed), Decimal("0"))
    total_expense = sum((row["expense"] for row in closed), Decimal("0"))
    total_savings = sum((row["savings"] for row in closed), Decimal("0"))
    kpis = {
        "months": len(closed),
        "avg_income": budget_calc.money(total_income / months),
        "avg_expense": budget_calc.money(total_expense / months),
        "avg_savings": budget_calc.money(total_savings / months),
        "total_savings": budget_calc.money(total_savings),
        "savings_rate": budget_calc.percent(total_savings, total_income),
        "positive_months": sum(1 for row in closed if row["balance"] >= 0),
    }

    # Catégories de charges mois par mois (6 principales + Autres)
    by_category = (
        history_transactions.filter(transaction_type=EntryType.EXPENSE)
        .exclude(category__name=budget_calc.SAVINGS_CATEGORY_NAME)
        .annotate(month=TruncMonth("transaction_date"))
        .values("month", "category__name")
        .annotate(total=Sum("amount"))
    )
    category_totals = {}
    cells = {}
    for row in by_category:
        name = row["category__name"] or "À classer"
        category_totals[name] = category_totals.get(name, Decimal("0")) + row["total"]
        cells[(row["month"], name)] = cells.get((row["month"], name), Decimal("0")) + row["total"]
    top = [name for name, _ in sorted(category_totals.items(), key=lambda item: -item[1])[:6]]
    month_keys = [date(row["year"], row["month"], 1) for row in history]
    category_series = [
        {"name": name, "values": [str(budget_calc.money(cells.get((key, name)))) for key in month_keys]}
        for name in top
    ]
    others = [
        str(budget_calc.money(sum(
            (total for (key2, name), total in cells.items() if key2 == key and name not in top),
            Decimal("0"),
        )))
        for key in month_keys
    ]
    if any(value != "0.00" for value in others):
        category_series.append({"name": "Autres", "values": others})

    plan = budget_calc.plan_for_month(request.user, today.year, today.month)
    trend = budget_calc.budget_trend(request.user, today, months=12) if plan else None
    trend_history = trend["history"] if trend else []

    subscription_history = _subscription_history(request.user, today)

    context = {
        "budget_tab": "analyse",
        "kpis": kpis,
        "plan": plan,
        "trend": trend,
        "history": history,
        "projects": budget_calc.projects_overview(request.user, today),
        "subscription_summary": subscription_detection.subscriptions_summary(request.user, today),
        "subscription_history": subscription_history,
        "subscription_roster": _subscription_roster(request.user),
        "today": today,
        "charts_data": {
            "history": {
                "labels": [row["label"] for row in history],
                "income": [str(row["income"]) for row in history],
                "expense": [str(row["expense"]) for row in history],
                "savings": [str(row["savings"]) for row in history],
                "balance": [str(row["balance"]) for row in history],
                "savingsRate": [row["savings_rate"] for row in history],
                "months": [[row["year"], row["month"]] for row in history],
            },
            "categories": category_series,
            "trend": {
                "labels": [row["label"] for row in trend_history],
                "budget": [str(row["budget"]) for row in trend_history],
                "spent": [str(row["spent"]) for row in trend_history],
                "savings": [str(row["savings_actual"]) for row in trend_history],
                "savingsTarget": [str(row["savings_target"]) for row in trend_history],
            },
            "subscriptions": {
                "labels": [row["label"] for row in subscription_history],
                "totals": [str(row["total"]) for row in subscription_history],
                "counts": [row["count"] for row in subscription_history],
            },
            "monthUrl": reverse("finance:month", args=[2000, 1]).replace("2000/1/", ""),
            "savingsTargetRate": budget_calc.percent(
                plan.savings_target_monthly, kpis["avg_income"]
            ) if plan else None,
        },
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
            + (f", {sync.skipped_count} déjà présente(s)." if sync.skipped_count else ".")
            + (f" {sync.message}" if sync.message else ""),
        )
    return JsonResponse(_bank_sync_payload(sync))


# ============================================================
# BUDGET : MON BUDGET / OBJECTIFS
# ============================================================

def _suggested_budget_lines(user, today):
    """Premier budget : on part des postes fixes / abonnements actifs et
    de la moyenne des 3 derniers mois pour les autres dépenses."""

    lines = []
    covered_categories = set()
    covered_subcategories = set()

    for item in RecurringItem.objects.filter(
        user=user, is_active=True, entry_type=EntryType.EXPENSE, category__isnull=False
    ).exclude(category__name=budget_calc.SAVINGS_CATEGORY_NAME).select_related("category", "subcategory"):
        monthly = item.default_amount
        if item.frequency_unit == RecurrenceUnit.YEAR:
            monthly = item.default_amount / (12 * (item.frequency_interval or 1))
        elif item.frequency_unit == RecurrenceUnit.MONTH and item.frequency_interval > 1:
            monthly = item.default_amount / item.frequency_interval
        lines.append({
            "section": BudgetSection.SUBSCRIPTION if item.kind == RecurrenceKind.SUBSCRIPTION else BudgetSection.FIXED,
            "category": item.category_id,
            "subcategory": item.subcategory_id,
            "label": item.name,
            "amount": budget_calc.money(monthly),
        })
        if item.subcategory_id:
            covered_subcategories.add(item.subcategory_id)
        else:
            covered_categories.add(item.category_id)

    start_year, start_month = budget_calc.add_months(today.year, today.month, -3)
    averages = (
        Transaction.objects.filter(
            user=user,
            transaction_type=EntryType.EXPENSE,
            recurring_item__isnull=True,
            category__isnull=False,
            transaction_date__gte=date(start_year, start_month, 1),
            transaction_date__lt=date(today.year, today.month, 1),
        )
        .exclude(category__name=budget_calc.SAVINGS_CATEGORY_NAME)
        .exclude(category_id__in=covered_categories)
        .exclude(subcategory_id__in=covered_subcategories)
        .values("category_id", "category__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )
    for row in averages:
        amount = (row["total"] / 3).quantize(Decimal("1"))
        if amount >= 5:
            lines.append({
                "section": BudgetSection.VARIABLE,
                "category": row["category_id"],
                "subcategory": None,
                "label": "",
                "amount": amount,
            })

    return lines


def _plan_lines_initial(plan):
    return [
        {
            "section": line.section,
            "category": line.category_id,
            "subcategory": line.subcategory_id,
            "label": line.label,
            "amount": line.amount,
        }
        for line in plan.lines.all()
    ]


def _save_plan(user, plan_form, formset):
    valid_from = plan_form.cleaned_data["valid_from"]
    plan, _ = BudgetPlan.objects.get_or_create(user=user, valid_from=valid_from)
    for field in plan_form.Meta.fields:
        setattr(plan, field, plan_form.cleaned_data[field])
    plan.save()

    plan.lines.all().delete()
    BudgetLine.objects.bulk_create([
        BudgetLine(
            plan=plan,
            section=data["section"],
            category=data["category"],
            subcategory=data.get("subcategory"),
            label=data.get("label") or "",
            amount=data["amount"],
            order=order,
        )
        for order, data in enumerate(
            form.cleaned_data
            for form in formset.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        )
    ])
    return plan


def _meal_voucher_form(user, data=None):
    instance = MealVoucherSettings.objects.filter(user=user).first()
    if instance is None:
        instance = MealVoucherSettings(user=user, enabled=False)
    return MealVoucherSettingsForm(data, instance=instance)


@login_required
def budget_plan_view(request):
    today = date.today()
    plan = budget_calc.plan_for_month(request.user, today.year, today.month)

    if request.method == "POST":
        plan_form = BudgetPlanForm(request.POST, instance=plan, today=today)
        formset = BudgetLineFormSet(request.POST, prefix="lines", form_kwargs={"user": request.user})
        if plan_form.is_valid() and formset.is_valid():
            saved = _save_plan(request.user, plan_form, formset)
            messages.success(
                request,
                f"Budget enregistré. Il s'applique à partir de {month_label(saved.valid_from)}.",
            )
            return redirect("finance:budget_plan")
        messages.error(request, "Le budget n'a pas été enregistré : vérifie les champs en rouge.")
    else:
        current_month = date(today.year, today.month, 1)
        initial_from = plan.valid_from if plan and plan.valid_from > current_month else current_month
        plan_form = BudgetPlanForm(
            instance=plan,
            today=today,
            initial={"valid_from": initial_from.isoformat()},
        )
        lines_initial = (
            _plan_lines_initial(plan) if plan else _suggested_budget_lines(request.user, today)
        )
        formset = BudgetLineFormSet(
            prefix="lines", initial=lines_initial, form_kwargs={"user": request.user}
        )

    salary_average, salary_months = budget_calc.average_salary(
        request.user, today.year, today.month, plan.income_average_months if plan else 6
    )

    context = {
        "budget_tab": "plan",
        "plan": plan,
        "is_new_plan": plan is None,
        "plan_form": plan_form,
        "formset": formset,
        "sections": BudgetSection.choices,
        "summary": budget_calc.plan_summary(plan, request.user, today.year, today.month) if plan else None,
        "salary_average": salary_average,
        "salary_months": salary_months,
        "meal_voucher_form": _meal_voucher_form(request.user),
        "meal_vouchers": budget_calc.meal_voucher_month(request.user, today.year, today.month),
        "versions": BudgetPlan.objects.filter(user=request.user).order_by("-valid_from"),
        "projects": budget_calc.projects_overview(request.user, today),
    }
    return render(request, "finance/budget_plan.html", context)


@login_required
@require_POST
def update_meal_vouchers(request):
    form = _meal_voucher_form(request.user, request.POST)
    if form.is_valid():
        form.save()
        messages.success(request, "Tickets restaurant mis à jour.")
    else:
        messages.error(request, "Impossible d'enregistrer les tickets restaurant.")
    return redirect(reverse("finance:budget_plan") + "#tickets")


def _current_plan_or_new(user, today):
    plan = budget_calc.plan_for_month(user, today.year, today.month)
    if plan is None:
        plan = BudgetPlan(user=user, valid_from=date(today.year, today.month, 1))
    return plan


@login_required
def budget_goals_view(request):
    today = date.today()
    plan = budget_calc.plan_for_month(request.user, today.year, today.month)
    overview = budget_calc.projects_overview(request.user, today)
    for item in overview["projects"]:
        item["edit_form"] = SavingsProjectForm(
            instance=item["project"], prefix=f"p{item['project'].pk}"
        )
    savings_target = plan.savings_target_monthly if plan else Decimal("0")

    context = {
        "budget_tab": "objectifs",
        "plan": plan,
        "savings_goal_form": SavingsGoalForm(instance=_current_plan_or_new(request.user, today)),
        "savings_target": savings_target,
        "savings_month": budget_calc.savings_split(request.user, today.year, today.month),
        "projects": overview,
        "free_savings_target": max(Decimal("0"), savings_target - overview["monthly_needed"]),
        "projects_exceed_target": overview["monthly_needed"] > savings_target,
        "savings_percent": min(100, budget_calc.percent(
            budget_calc.savings_split(request.user, today.year, today.month)["total"], savings_target
        )),
        "project_form": SavingsProjectForm(),
        "month_label": month_label(today).capitalize(),
    }
    return render(request, "finance/budget_goals.html", context)


@login_required
@require_POST
def update_savings_goal(request):
    today = date.today()
    plan = _current_plan_or_new(request.user, today)
    form = SavingsGoalForm(request.POST, instance=plan)
    if form.is_valid():
        form.save()
        messages.success(request, "Objectif d'épargne mis à jour.")
    else:
        messages.error(request, "Montant invalide.")
    return redirect("finance:budget_goals")


@login_required
@require_POST
def add_savings_project(request):
    form = SavingsProjectForm(request.POST)
    if form.is_valid():
        project = form.save(commit=False)
        project.user = request.user
        project.save()
        messages.success(request, f"Projet « {project.name} » créé.")
    else:
        messages.error(request, "Impossible de créer le projet : vérifie le formulaire.")
    return redirect("finance:budget_goals")


@login_required
@require_POST
def edit_savings_project(request, pk):
    project = get_object_or_404(SavingsProject, pk=pk, user=request.user)
    form = SavingsProjectForm(request.POST, instance=project, prefix=f"p{pk}")
    if form.is_valid():
        form.save()
        messages.success(request, "Projet mis à jour.")
    else:
        messages.error(request, "Impossible de mettre à jour le projet.")
    return redirect("finance:budget_goals")


@login_required
@require_POST
def delete_savings_project(request, pk):
    project = get_object_or_404(SavingsProject, pk=pk, user=request.user)
    project.delete()
    messages.success(request, "Projet supprimé.")
    return redirect("finance:budget_goals")


# ============================================================
# BUDGET : ABONNEMENTS DÉTECTÉS
# ============================================================

@login_required
def subscriptions_view(request):
    today = date.today()
    changes = subscription_detection.sync_subscriptions(request.user, today)
    for change, item in changes:
        if change == "stopped":
            messages.info(request, f"« {item.name} » n'a pas été payé : abonnement marqué comme arrêté.")
        else:
            messages.info(request, f"« {item.name} » a de nouveau été payé : abonnement réactivé.")

    tracked = subscription_detection.tracked_subscriptions(request.user, today)

    # Calendrier des paiements : ?mois=2026-10 (mois en cours par défaut)
    try:
        cal_year, cal_month = (int(part) for part in request.GET.get("mois", "").split("-"))
        date(cal_year, cal_month, 1)
    except ValueError:
        cal_year, cal_month = today.year, today.month
    month_subs = subscription_detection.month_subscriptions(request.user, cal_year, cal_month, today)
    by_day = {}
    for row in month_subs["paid"] + month_subs["remaining"] + month_subs["problems"]:
        by_day.setdefault(row["date"].day, []).append(row)
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(cal_year, cal_month):
        weeks.append([
            {
                "day": day,
                "events": by_day.get(day, []),
                "total": sum((row["amount"] for row in by_day.get(day, [])), Decimal("0")),
                "is_today": (cal_year, cal_month, day) == (today.year, today.month, today.day),
            } if day else None
            for day in week
        ])
    prev_year, prev_month = budget_calc.add_months(cal_year, cal_month, -1)
    next_year, next_month = budget_calc.add_months(cal_year, cal_month, 1)

    context = {
        "budget_tab": "abonnements",
        "calendar": {
            "label": month_label(date(cal_year, cal_month, 1)).capitalize(),
            "weeks": weeks,
            "subs": month_subs,
            "prev": f"{prev_year}-{prev_month:02d}",
            "next": f"{next_year}-{next_month:02d}",
            "is_current": (cal_year, cal_month) == (today.year, today.month),
            "current": f"{today.year}-{today.month:02d}",
        },
        "suggestions": subscription_detection.subscription_suggestions(request.user, today),
        "tracked_active": [row for row in tracked if not row["status"] or row["status"]["status"] != "stopped"],
        "tracked_stopped": [row for row in tracked if row["status"] and row["status"]["status"] == "stopped"],
        "summary": subscription_detection.subscriptions_summary(request.user, today),
        "manual_subscriptions": RecurringItem.objects.filter(
            user=request.user, kind=RecurrenceKind.SUBSCRIPTION, match_key="", is_active=True
        ),
        "history_months": _active_history_month_count(request.user, today),
        "bank_connection": BankConnection.objects.filter(user=request.user).first(),
    }
    return render(request, "finance/budget_subscriptions.html", context)


@login_required
@require_POST
def follow_subscription(request):
    candidate = subscription_detection.find_candidate(request.user, request.POST.get("key", ""))
    if candidate is None:
        messages.error(request, "Cet abonnement n'a pas été retrouvé dans tes opérations.")
    else:
        item = subscription_detection.follow_candidate(request.user, candidate)
        messages.success(
            request,
            f"« {item.name} » est maintenant suivi ({candidate.count} paiements rattachés).",
        )
    return redirect("finance:budget_subscriptions")


@login_required
@require_POST
def dismiss_subscription(request):
    key = request.POST.get("key", "")
    if key:
        subscription_detection.dismiss_candidate(request.user, key)
        messages.info(request, "Suggestion ignorée.")
    return redirect("finance:budget_subscriptions")


@login_required
@require_POST
def untrack_subscription(request, pk):
    item = get_object_or_404(RecurringItem, pk=pk, user=request.user)
    name = item.name
    # Les paiements restent dans l'historique, simplement détachés
    item.delete()
    messages.success(request, f"« {name} » n'est plus suivi.")
    return redirect("finance:budget_subscriptions")
