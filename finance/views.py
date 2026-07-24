import calendar
import json
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.forms import AccountForm, AccountPasswordChangeForm

from .forms import CategoryForm, RecurringItemForm, SubcategoryForm, TransactionForm
from .models import (
    Category,
    EntryType,
    MonthlyBudget,
    RecurringItem,
    Subcategory,
    Transaction,
)


def _month_context(year, month):
    first_day = date(year, month, 1)
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return {
        "year": year,
        "month": month,
        "month_label": first_day.strftime("%B %Y").capitalize(),
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
    }


@login_required
def index(request):
    today = date.today()
    return redirect("finance:month", year=today.year, month=today.month)


@login_required
def month_view(request, year, month):
    today = date.today()
    if not (1 <= month <= 12) or (year, month) > (today.year, today.month):
        return redirect("finance:month", year=today.year, month=today.month)

    budget = MonthlyBudget.objects.filter(
        user=request.user, year=year, month=month
    ).first()

    transactions = Transaction.objects.filter(
        user=request.user,
        transaction_date__year=year,
        transaction_date__month=month,
    ).select_related("category", "subcategory", "recurring_item")

    recurring_transactions = transactions.filter(recurring_item__isnull=False)
    other_transactions = transactions.filter(recurring_item__isnull=True)

    totals = transactions.aggregate(
        income=Sum("amount", filter=Q(transaction_type=EntryType.INCOME)),
        expense=Sum("amount", filter=Q(transaction_type=EntryType.EXPENSE)),
    )
    total_income = totals["income"] or Decimal("0")
    total_expense = totals["expense"] or Decimal("0")

    context = {
        **_month_context(year, month),
        "budget": budget,
        "recurring_transactions": recurring_transactions,
        "other_transactions": other_transactions,
        "total_income": total_income,
        "total_expense": total_expense,
        "balance": total_income - total_expense,
        "transaction_form": TransactionForm(
            user=request.user,
            initial={"transaction_date": date(year, month, min(today.day, calendar.monthrange(year, month)[1]))},
        ),
        "has_recurring_items": RecurringItem.objects.filter(
            user=request.user, is_active=True
        ).exists(),
        "is_current_month": (year, month) == (today.year, today.month),
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
        last_day = calendar.monthrange(year, month)[1]
        entry_date = date(year, month, min(date.today().day, last_day))
        items = RecurringItem.objects.filter(user=request.user, is_active=True)
        Transaction.objects.bulk_create(
            [
                Transaction(
                    user=request.user,
                    monthly_budget=budget,
                    recurring_item=item,
                    transaction_type=item.entry_type,
                    amount=item.default_amount,
                    description=item.name,
                    category=item.category,
                    subcategory=item.subcategory,
                    transaction_date=entry_date,
                )
                for item in items
            ]
        )
        messages.success(request, "Le mois a été démarré avec vos postes fixes.")
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


@login_required
def dashboard_hub_view(request):
    return render(request, "finance/dashboard_hub.html")


@login_required
def dashboard_budget_view(request):
    today = date.today()

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

    history_start_year = today.year
    history_start_month = today.month - (HISTORY_MONTHS - 1)
    while history_start_month <= 0:
        history_start_month += 12
        history_start_year -= 1
    history_start = date(history_start_year, history_start_month, 1)

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

    context = {
        "month_label": today.strftime("%B %Y").capitalize(),
        "current_income": current_income,
        "current_expense": current_expense,
        "current_balance": current_income - current_expense,
        "category_breakdown": category_breakdown,
        "history": history,
        "history_labels_json": json.dumps([row["label"] for row in history]),
        "history_income_json": json.dumps([str(row["income"]) for row in history]),
        "history_expense_json": json.dumps([str(row["expense"]) for row in history]),
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
    if active_tab not in {"compte", "postes-fixes", "categories"}:
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
        item.save()
        messages.success(request, "Poste fixe ajouté.")
    else:
        messages.error(request, "Impossible d'ajouter le poste fixe.")
    return redirect(f"{reverse('finance:settings')}?tab=postes-fixes")


@login_required
@require_POST
def edit_recurring_item(request, pk):
    item = get_object_or_404(RecurringItem, pk=pk, user=request.user)
    form = RecurringItemForm(request.POST, instance=item, user=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, "Poste fixe mis à jour.")
    else:
        messages.error(request, "Impossible de mettre à jour le poste fixe.")
    return redirect(f"{reverse('finance:settings')}?tab=postes-fixes")


@login_required
@require_POST
def delete_recurring_item(request, pk):
    item = get_object_or_404(RecurringItem, pk=pk, user=request.user)
    item.delete()
    messages.success(request, "Poste fixe supprimé.")
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
