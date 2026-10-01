"""
Calculs du module Budget : budget type, salaire de référence, tickets
restaurant, suivi du mois en cours, projets d'épargne et tendances.

Les catégories sont repérées par leur nom, comme dans l'import bancaire
(voir bank_import.SAVINGS_CATEGORY).
"""

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q, Sum

from .models import (
    BudgetPlan,
    BudgetSection,
    EntryType,
    IncomeMode,
    MealVoucherSettings,
    SavingsProject,
    Transaction,
)

SAVINGS_CATEGORY_NAME = "Épargne & placements"
FOOD_CATEGORY_NAME = "Alimentation & restaurants"
SALARY_CATEGORY_NAME = "Salaire"
HOUSING_CATEGORY_NAME = "Logement & charges"

ZERO = Decimal("0")

SHORT_MONTHS = [
    "Janv.", "Févr.", "Mars", "Avr.", "Mai", "Juin",
    "Juil.", "Août", "Sept.", "Oct.", "Nov.", "Déc.",
]
CENT = Decimal("0.01")

# Sections dont le montant tombe quoi qu'il arrive dans le mois
# (loyer, abonnements...) : on ne les extrapole pas au prorata des jours.
COMMITTED_SECTIONS = {
    BudgetSection.FIXED,
    BudgetSection.SUBSCRIPTION,
    BudgetSection.PROVISION,
}


# ============================================================
# OUTILS DATES
# ============================================================

def month_start(year, month):
    return date(year, month, 1)


def add_months(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def months_between(start, end):
    """Nombre de mois de `start` à `end` inclus (mois calendaires)."""
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def working_days(year, month):
    _, last_day = calendar.monthrange(year, month)
    return sum(
        1 for day in range(1, last_day + 1)
        if date(year, month, day).weekday() < 5
    )


def money(value):
    return (value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


def percent(part, total):
    if not total:
        return 0
    return int((part / total * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ============================================================
# BUDGET TYPE
# ============================================================

def plan_for_month(user, year, month):
    """Version du budget en vigueur pour ce mois.
    Avant le tout premier budget, on compare au premier budget créé."""

    plans = BudgetPlan.objects.filter(user=user).prefetch_related(
        "lines__category", "lines__subcategory"
    )
    current = plans.filter(valid_from__lte=month_start(year, month)).first()
    return current or plans.order_by("valid_from").first()


def average_salary(user, year, month, months):
    """Moyenne des salaires reçus sur les `months` mois complets précédant
    ce mois. Les mois sans salaire sont ignorés. À défaut de catégorie
    Salaire, on prend tous les revenus.

    Renvoie (moyenne, nombre de mois utilisés)."""

    start_year, start_month = add_months(year, month, -months)
    period = Transaction.objects.filter(
        user=user,
        transaction_type=EntryType.INCOME,
        transaction_date__gte=month_start(start_year, start_month),
        transaction_date__lt=month_start(year, month),
    )

    salaries = period.filter(category__name=SALARY_CATEGORY_NAME)
    if not salaries.exists():
        salaries = period

    totals = {}
    for row in salaries.values("transaction_date__year", "transaction_date__month").annotate(
        total=Sum("amount")
    ):
        key = (row["transaction_date__year"], row["transaction_date__month"])
        totals[key] = row["total"]

    values = [total for total in totals.values() if total > 0]
    if not values:
        return ZERO, 0

    return money(sum(values) / len(values)), len(values)


def plan_income(plan, user, year, month):
    """Revenu de référence du budget pour ce mois.
    Renvoie (montant, explication)."""

    if plan is None:
        return ZERO, "Aucun budget"

    if plan.income_mode == IncomeMode.MANUAL:
        return money(plan.manual_income), "Montant fixe"

    value, count = average_salary(user, year, month, plan.income_average_months)
    if count == 0:
        return money(plan.manual_income), "Pas encore d'historique de salaire"

    return value, f"Moyenne sur {count} mois"


# ============================================================
# TICKETS RESTAURANT
# ============================================================

def meal_voucher_settings(user):
    return MealVoucherSettings.objects.filter(user=user, enabled=True).first()


def meal_voucher_month(user, year, month, settings=None):
    """Crédit, retenue sur salaire et consommation des tickets pour un mois."""

    settings = settings if settings is not None else meal_voucher_settings(user)
    if settings is None:
        return None

    days = settings.days_per_month or working_days(year, month)
    credit = money(settings.face_value * days)
    employee_cost = money(credit * settings.employee_share_percent / 100)

    spent = Transaction.objects.filter(
        user=user,
        transaction_type=EntryType.EXPENSE,
        paid_with_meal_voucher=True,
        transaction_date__year=year,
        transaction_date__month=month,
    ).aggregate(total=Sum("amount"))["total"] or ZERO

    return {
        "days": days,
        "days_is_auto": settings.days_per_month is None,
        "face_value": settings.face_value,
        "credit": credit,
        "employee_cost": employee_cost,
        "employer_gift": credit - employee_cost,
        "spent": money(spent),
        "remaining": money(credit - spent),
        "spent_percent": percent(spent, credit),
    }


# ============================================================
# RÉSUMÉ D'UN BUDGET (page "Mon budget")
# ============================================================

def section_totals(lines):
    totals = {section: ZERO for section in BudgetSection.values}
    for line in lines:
        totals[line.section] += line.amount
    return totals


def plan_summary(plan, user, year, month):
    """Ressources, totaux par section, reste à vivre et indicateurs clés."""

    income, income_source = plan_income(plan, user, year, month)
    vouchers = meal_voucher_month(user, year, month)
    voucher_credit = vouchers["credit"] if vouchers else ZERO

    lines = list(plan.lines.all()) if plan else []
    totals = section_totals(lines)
    savings = plan.savings_target_monthly if plan else ZERO
    spending = sum(totals.values(), ZERO)
    resources = income + voucher_credit
    left_to_live = resources - spending - savings

    housing = sum(
        (line.amount for line in lines if line.category.name == HOUSING_CATEGORY_NAME),
        ZERO,
    )

    def indicator(label, value, threshold_label, ok, ok_text, ko_text):
        return {
            "label": label,
            "value": value,
            "threshold": threshold_label,
            "ok": ok,
            "diagnostic": ok_text if ok else ko_text,
        }

    indicators = []
    if income > 0:
        housing_rate = percent(housing, income)
        savings_rate = percent(savings, income)
        fixed_rate = percent(totals[BudgetSection.FIXED], income)
        variable_rate = percent(totals[BudgetSection.VARIABLE], income)
        free_rate = percent(left_to_live, income)
        indicators = [
            indicator("Taux d'effort logement", housing_rate, "≤ 33 %", housing_rate <= 33,
                      "Logement soutenable", "Logement trop lourd"),
            indicator("Taux d'épargne", savings_rate, "≥ 10 %", savings_rate >= 10,
                      "Épargne satisfaisante", "Épargne faible"),
            indicator("Charges fixes", fixed_rate, "≤ 55 %", fixed_rate <= 55,
                      "Charges fixes maîtrisées", "Charges fixes élevées"),
            indicator("Charges variables", variable_rate, "≤ 25 %", variable_rate <= 25,
                      "Variables sous contrôle", "Variables élevées"),
            indicator("Reste à vivre", free_rate, "≥ 5 %", free_rate >= 5,
                      "Marge confortable", "Marge très faible"),
        ]

    return {
        "income": income,
        "income_source": income_source,
        "vouchers": vouchers,
        "resources": resources,
        "totals": totals,
        "sections": [
            {"key": key, "label": label, "total": totals[key]}
            for key, label in BudgetSection.choices
        ],
        "spending": spending,
        "savings": savings,
        "left_to_live": left_to_live,
        "is_balanced": left_to_live >= 0,
        "max_rent": money(income * Decimal("0.33")),
        "indicators": indicators,
    }


# ============================================================
# SUIVI D'UN MOIS (dashboard)
# ============================================================

def _line_index(lines):
    """Une dépense va à la ligne la plus précise : sous-catégorie d'abord,
    puis catégorie entière."""

    by_subcategory = {}
    by_category = {}
    for line in lines:
        if line.subcategory_id:
            by_subcategory.setdefault(line.subcategory_id, line)
        else:
            by_category.setdefault(line.category_id, line)
    return by_subcategory, by_category


def _match_line(transaction, index):
    by_subcategory, by_category = index
    if transaction.subcategory_id and transaction.subcategory_id in by_subcategory:
        return by_subcategory[transaction.subcategory_id]
    return by_category.get(transaction.category_id)


def _month_expenses(user, year, month):
    return Transaction.objects.filter(
        user=user,
        transaction_type=EntryType.EXPENSE,
        transaction_date__year=year,
        transaction_date__month=month,
    ).select_related("category")


def month_tracking(user, year, month, today=None):
    """Budget prévu / dépensé / projeté pour un mois, ligne par ligne."""

    today = today or date.today()
    plan = plan_for_month(user, year, month)
    if plan is None:
        return None

    _, days_in_month = calendar.monthrange(year, month)
    if (year, month) < (today.year, today.month):
        elapsed = Decimal("1")
        day = days_in_month
    elif (year, month) == (today.year, today.month):
        day = today.day
        elapsed = Decimal(day) / Decimal(days_in_month)
    else:
        day = 0
        elapsed = ZERO

    lines = list(plan.lines.all())
    index = _line_index(lines)
    actual = {line.id: ZERO for line in lines}
    unbudgeted = {}
    savings_actual = ZERO

    for transaction in _month_expenses(user, year, month):
        category_name = transaction.category.name if transaction.category else None
        if category_name == SAVINGS_CATEGORY_NAME:
            savings_actual += transaction.amount
            continue
        line = _match_line(transaction, index)
        if line:
            actual[line.id] += transaction.amount
        else:
            key = category_name or "Sans catégorie"
            unbudgeted[key] = unbudgeted.get(key, ZERO) + transaction.amount

    rows = []
    for line in lines:
        spent = actual[line.id]
        if line.section in COMMITTED_SECTIONS:
            projected = max(spent, line.amount)
            expected = line.amount if elapsed >= 1 else spent
        else:
            # Réalisé + le reste du mois au rythme prévu par le budget
            # (une extrapolation au prorata s'emballe en début de mois)
            expected = line.amount * elapsed
            projected = spent + line.amount * (1 - elapsed)

        # Au-dessus du rythme, avec une tolérance de 10 % du budget
        tolerance = expected * Decimal("1.2") + line.amount * Decimal("0.1")
        if spent > line.amount:
            status = "over"
        elif line.section not in COMMITTED_SECTIONS and spent > tolerance:
            status = "warning"
        else:
            status = "ok"

        rows.append({
            "line": line,
            "label": line.display_label,
            "section": line.section,
            "section_label": line.get_section_display(),
            "budget": line.amount,
            "spent": money(spent),
            "projected": money(projected),
            "expected": money(expected),
            "remaining": money(line.amount - spent),
            "overspent": money(max(ZERO, spent - line.amount)),
            "percent": percent(spent, line.amount) if line.amount else (100 if spent else 0),
            "pace_percent": min(100, percent(expected, line.amount)) if line.amount else 0,
            "status": status,
        })

    budget_total = sum((line.amount for line in lines), ZERO)
    spent_total = sum(actual.values(), ZERO)
    unbudgeted_total = sum(unbudgeted.values(), ZERO)
    projected_total = sum((row["projected"] for row in rows), ZERO) + unbudgeted_total

    income, income_source = plan_income(plan, user, year, month)
    vouchers = meal_voucher_month(user, year, month)
    resources = income + (vouchers["credit"] if vouchers else ZERO)
    projected_left = resources - projected_total - plan.savings_target_monthly

    over_count = sum(1 for row in rows if row["status"] == "over")
    warning_count = sum(1 for row in rows if row["status"] == "warning")
    if projected_total > budget_total * Decimal("1.05") or over_count >= 2:
        verdict = ("off", "Budget en danger")
    elif over_count or warning_count or unbudgeted_total > 0:
        verdict = ("watch", "À surveiller")
    else:
        verdict = ("on", "Sur la bonne voie")

    return {
        "plan": plan,
        "year": year,
        "month": month,
        "day": day,
        "days_in_month": days_in_month,
        "elapsed_percent": percent(elapsed, Decimal("1")),
        "rows": rows,
        "sections": [
            {
                "key": key,
                "label": label,
                "rows": [row for row in rows if row["section"] == key],
            }
            for key, label in BudgetSection.choices
            if any(row["section"] == key for row in rows)
        ],
        "unbudgeted": sorted(
            ({"label": label, "spent": money(total)} for label, total in unbudgeted.items()),
            key=lambda item: -item["spent"],
        ),
        "unbudgeted_total": money(unbudgeted_total),
        "budget_total": money(budget_total),
        "spent_total": money(spent_total + unbudgeted_total),
        "spent_percent": percent(spent_total + unbudgeted_total, budget_total),
        "projected_total": money(projected_total),
        "projected_gap": money(budget_total - projected_total),
        "projected_overrun": money(max(ZERO, projected_total - budget_total)),
        "savings_target": plan.savings_target_monthly,
        "savings_actual": money(savings_actual),
        "savings_percent": percent(savings_actual, plan.savings_target_monthly),
        "income": income,
        "income_source": income_source,
        "vouchers": vouchers,
        "resources": resources,
        "projected_left": money(projected_left),
        "verdict": verdict[0],
        "verdict_label": verdict[1],
        "over_count": over_count,
        "warning_count": warning_count,
    }


# ============================================================
# TENDANCES SUR PLUSIEURS MOIS
# ============================================================

def budget_trend(user, today=None, months=6):
    """Budget vs réalisé sur les `months` derniers mois (mois en cours inclus),
    et lignes qui dépassent régulièrement."""

    today = today or date.today()
    history = []
    line_overruns = {}

    for offset in range(months - 1, -1, -1):
        year, month = add_months(today.year, today.month, -offset)
        tracking = month_tracking(user, year, month, today)
        if tracking is None:
            continue
        is_current = (year, month) == (today.year, today.month)
        history.append({
            "year": year,
            "month": month,
            "label": f"{SHORT_MONTHS[month - 1]} {year}",
            "is_current": is_current,
            "budget": tracking["budget_total"],
            "spent": tracking["spent_total"],
            "gap": tracking["budget_total"] - tracking["spent_total"],
            "savings_target": tracking["savings_target"],
            "savings_actual": tracking["savings_actual"],
        })
        if not is_current:
            for row in tracking["rows"]:
                stats = line_overruns.setdefault(
                    row["label"], {"label": row["label"], "budget": row["budget"], "total": ZERO, "months": 0, "over": 0}
                )
                stats["total"] += row["spent"]
                stats["months"] += 1
                stats["over"] += 1 if row["spent"] > row["budget"] else 0

    recurring_overruns = []
    for stats in line_overruns.values():
        if stats["months"] == 0:
            continue
        average = money(stats["total"] / stats["months"])
        if stats["over"] >= 2 and average > stats["budget"]:
            recurring_overruns.append({
                "label": stats["label"],
                "budget": stats["budget"],
                "average": average,
                "gap": average - stats["budget"],
                "over": stats["over"],
                "months": stats["months"],
            })
    recurring_overruns.sort(key=lambda item: -item["gap"])

    closed = [row for row in history if not row["is_current"]]
    on_budget_months = sum(1 for row in closed if row["spent"] <= row["budget"])
    savings_months = sum(1 for row in closed if row["savings_actual"] >= row["savings_target"])

    return {
        "history": history,
        "recurring_overruns": recurring_overruns,
        "closed_count": len(closed),
        "on_budget_months": on_budget_months,
        "savings_months": savings_months,
    }


# ============================================================
# PROJETS D'ÉPARGNE
# ============================================================

def project_progress(project, today=None):
    today = today or date.today()

    contributed = project.contributions.filter(
        transaction_type=EntryType.EXPENSE
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    saved = money(project.initial_amount + contributed)
    remaining = max(ZERO, project.target_amount - saved)

    this_month = today.replace(day=1)
    target_month = project.target_date.replace(day=1)
    months_left = max(1, months_between(this_month, target_month))
    is_late = project.target_date < today and remaining > 0
    monthly_needed = money(remaining / months_left) if remaining > 0 and not is_late else ZERO

    # Rythme attendu : progression linéaire entre la création et l'échéance
    start = project.created_at.date().replace(day=1)
    total_months = max(1, months_between(start, target_month))
    elapsed_months = min(total_months, max(0, months_between(start, this_month) - 1))
    expected = money(
        project.initial_amount
        + (project.target_amount - project.initial_amount) * Decimal(elapsed_months) / Decimal(total_months)
    )

    if remaining == 0:
        status, status_label = "done", "Objectif atteint"
    elif is_late:
        status, status_label = "off", "Échéance dépassée"
    elif saved >= expected:
        status, status_label = "on", "Dans les temps"
    else:
        status, status_label = "watch", f"En retard de {money(expected - saved)} €"

    contributed_this_month = project.contributions.filter(
        transaction_type=EntryType.EXPENSE,
        transaction_date__year=today.year,
        transaction_date__month=today.month,
    ).aggregate(total=Sum("amount"))["total"] or ZERO

    return {
        "project": project,
        "saved": saved,
        "remaining": money(remaining),
        "percent": min(100, percent(saved, project.target_amount)),
        "months_left": months_left,
        "monthly_needed": monthly_needed,
        "contributed_this_month": money(contributed_this_month),
        "status": status,
        "status_label": status_label,
    }


def projects_overview(user, today=None):
    projects = SavingsProject.objects.filter(user=user)
    progress = [project_progress(project, today) for project in projects]
    active = [item for item in progress if not item["project"].is_done]
    return {
        "projects": progress,
        "active_count": len(active),
        "monthly_needed": sum((item["monthly_needed"] for item in active), ZERO),
    }


def savings_split(user, year, month):
    """Épargne du mois : part affectée aux projets et épargne libre."""

    savings = Transaction.objects.filter(
        user=user,
        transaction_type=EntryType.EXPENSE,
        category__name=SAVINGS_CATEGORY_NAME,
        transaction_date__year=year,
        transaction_date__month=month,
    ).aggregate(
        total=Sum("amount"),
        projects=Sum("amount", filter=Q(savings_project__isnull=False)),
    )
    total = savings["total"] or ZERO
    projects = savings["projects"] or ZERO
    return {"total": money(total), "projects": money(projects), "free": money(total - projects)}


# ============================================================
# TOTAUX : REVENUS / CHARGES / ÉPARGNE
# ============================================================

def totals_annotations():
    """Agrégats d'un ensemble d'opérations. Les virements vers l'épargne
    ne sont pas des charges : ils sont comptés à part."""

    is_savings = Q(category__name=SAVINGS_CATEGORY_NAME)
    return {
        "income": Sum("amount", filter=Q(transaction_type=EntryType.INCOME)),
        "expense": Sum("amount", filter=Q(transaction_type=EntryType.EXPENSE) & ~is_savings),
        "savings": Sum("amount", filter=Q(transaction_type=EntryType.EXPENSE) & is_savings),
    }


def split_totals(row):
    """{income, expense, savings, balance, after_savings} arrondis.
    balance = revenus − charges (ce que tu as dégagé ce mois) ;
    after_savings = ce qui reste sur le compte après l'épargne."""

    row = row or {}
    income = money(row.get("income"))
    expense = money(row.get("expense"))
    savings = money(row.get("savings"))
    return {
        "income": income,
        "expense": expense,
        "savings": savings,
        "balance": income - expense,
        "after_savings": income - expense - savings,
    }


# ============================================================
# GRAPHIQUES DE LA PAGE MOIS
# ============================================================

def is_savings_transaction(transaction):
    return (
        transaction.transaction_type == EntryType.EXPENSE
        and transaction.category is not None
        and transaction.category.name == SAVINGS_CATEGORY_NAME
    )


def category_tree(transactions):
    """Charges (hors épargne) par catégorie puis sous-catégorie, triées."""

    tree = {}
    for transaction in transactions:
        if transaction.transaction_type != EntryType.EXPENSE or is_savings_transaction(transaction):
            continue
        category = transaction.category.name if transaction.category else "À classer"
        subcategory = transaction.subcategory.name if transaction.subcategory else "Non précisé"
        node = tree.setdefault(category, {"name": category, "total": ZERO, "children": {}})
        node["total"] += transaction.amount
        node["children"][subcategory] = node["children"].get(subcategory, ZERO) + transaction.amount

    result = []
    for node in sorted(tree.values(), key=lambda n: -n["total"]):
        children = sorted(node["children"].items(), key=lambda item: -item[1])
        result.append({
            "name": node["name"],
            "total": str(money(node["total"])),
            "children": [{"name": name, "total": str(money(total))} for name, total in children],
        })
    return result


def daily_cumulative(transactions, year, month, today=None, budget_total=None):
    """Cumuls jour par jour : revenus, charges, épargne, et rythme du budget."""

    today = today or date.today()
    _, last_day = calendar.monthrange(year, month)
    if (year, month) == (today.year, today.month):
        visible_days = today.day
    elif (year, month) > (today.year, today.month):
        visible_days = 0
    else:
        visible_days = last_day

    per_day = {day: {"income": ZERO, "expense": ZERO, "savings": ZERO} for day in range(1, last_day + 1)}
    for transaction in transactions:
        day = transaction.transaction_date.day
        if transaction.transaction_type == EntryType.INCOME:
            per_day[day]["income"] += transaction.amount
        elif is_savings_transaction(transaction):
            per_day[day]["savings"] += transaction.amount
        else:
            per_day[day]["expense"] += transaction.amount

    labels, income, expense, savings, pace = [], [], [], [], []
    running = {"income": ZERO, "expense": ZERO, "savings": ZERO}
    for day in range(1, last_day + 1):
        for key in running:
            running[key] += per_day[day][key]
        labels.append(day)
        visible = day <= visible_days
        income.append(str(money(running["income"])) if visible else None)
        expense.append(str(money(running["expense"])) if visible else None)
        savings.append(str(money(running["savings"])) if visible else None)
        if budget_total:
            pace.append(str(money(budget_total * day / last_day)))

    return {
        "labels": labels,
        "income": income,
        "expense": expense,
        "savings": savings,
        "pace": pace,
    }
