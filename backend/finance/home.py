"""
Page d'accueil : patrimoine, mois en cours, trésorerie et objectifs.

Rien n'est calculé ici de zéro : on rassemble ce que produisent les modules
Budget (budget.py), Épargne (wealth.py) et Abonnements (subscriptions.py),
pour les afficher ensemble.
"""

import calendar
from datetime import timedelta
from decimal import Decimal


from . import budget as budget_calc
from . import forecast as forecast_calc
from . import savings_goal as savings_goal_calc
from . import subscriptions as subscription_detection
from . import wealth
from .models import (
    BudgetSection,
    SavingsGoal,
    Transaction,
)
from django.utils.translation import gettext as _

ZERO = Decimal("0")

# Matelas de sécurité conseillé : 3 à 6 mois de dépenses sur des livrets
SAFETY_MIN_MONTHS = 3
SAFETY_MAX_MONTHS = 6

# ============================================================
# PATRIMOINE
# ============================================================

def _value_at(history, day):
    """Patrimoine au `day` d'après l'historique de la vue Épargne."""
    value = None
    for iso, balances, securities in zip(history["dates"], history["balances"], history["securities"]):
        if iso > day.isoformat():
            break
        value = Decimal(balances) + Decimal(securities)
    return value


def wealth_summary(user, today, avg_expense):
    overview = wealth.savings_overview(user, today)
    portfolio = overview["portfolio"]
    livrets = overview["balances"]["balance"]
    investments = overview["total"] - livrets

    change = None
    start = _value_at(overview["history"], today - timedelta(days=365))
    if start is not None and len(overview["history"]["dates"]) > 1:
        change = {
            "amount": budget_calc.money(overview["total"] - start),
            "percent": budget_calc.percent(overview["total"] - start, start) if start else None,
        }

    safety_months = None
    if avg_expense > 0 and overview["has_accounts"]:
        safety_months = (livrets / avg_expense).quantize(Decimal("0.1"))

    return {
        "overview": overview,
        "has_accounts": overview["has_accounts"],
        "total": overview["total"],
        "livrets": budget_calc.money(livrets),
        "investments": budget_calc.money(investments),
        "livrets_percent": budget_calc.percent(livrets, overview["total"]),
        "latent": portfolio["totals"].get("latent") if portfolio else None,
        "interest_year": overview["balances"]["interest_year"],
        "contributions": overview["contributions"],
        "change": change,
        "safety_months": safety_months,
        "safety_percent": min(100, int(safety_months / SAFETY_MAX_MONTHS * 100)) if safety_months is not None else 0,
        "supports": overview["supports"][:4],
        "history": overview["history"],
    }


# ============================================================
# MOIS EN COURS
# ============================================================

def month_summary(user, today, current_row, avg_expense):
    tracking = budget_calc.month_tracking(user, today.year, today.month, today)
    _ignored, days_in_month = calendar.monthrange(today.year, today.month)
    days_left = days_in_month - today.day + 1  # aujourd'hui compris

    summary = {
        "row": current_row,
        "tracking": tracking,
        "days_left": days_left,
        "days_in_month": days_in_month,
        "elapsed_percent": budget_calc.percent(Decimal(today.day), Decimal(days_in_month)),
        "avg_expense": avg_expense,
        "expense_vs_avg": budget_calc.percent(current_row["expense"], avg_expense) if avg_expense else None,
    }

    if tracking:
        # Ce qu'il reste à dépenser sur les postes variables, jour par jour
        variable_left = sum(
            (max(ZERO, row["budget"] - row["spent"]) for row in tracking["rows"]
             if row["section"] == BudgetSection.VARIABLE),
            ZERO,
        )
        summary.update({
            "remaining": budget_calc.money(max(ZERO, tracking["budget_total"] - tracking["spent_total"])),
            "variable_left": budget_calc.money(variable_left),
            "daily_allowance": budget_calc.money(variable_left / days_left),
            "spent_bar": min(100, tracking["spent_percent"]),
        })
    return summary


def category_breakdown(user, today, limit=5):
    transactions = Transaction.objects.filter(
        user=user,
        transaction_date__year=today.year,
        transaction_date__month=today.month,
    ).select_related("category", "subcategory")
    tree = budget_calc.category_tree(transactions)
    total = sum((Decimal(node["total"]) for node in tree), ZERO)
    rows = [
        {"name": node["name"], "total": Decimal(node["total"]), "percent": budget_calc.percent(Decimal(node["total"]), total)}
        for node in tree[:limit]
    ]
    others = total - sum((row["total"] for row in rows), ZERO)
    if others > 0:
        rows.append({"name": _("Autres"), "total": budget_calc.money(others), "percent": budget_calc.percent(others, total)})
    return {"rows": rows, "total": budget_calc.money(total)}


# ============================================================
# ASSEMBLAGE
# ============================================================

def _global_goal(user, today, wealth_info, plan):
    goal = SavingsGoal.objects.filter(user=user).first()
    if goal is None:
        return None
    return savings_goal_calc.goal_overview(
        goal, user, wealth_info["total"], today,
        monthly_target=plan.savings_target_monthly if plan else None,
    )


def home_summary(user, today, history):
    """`history` : les 12 derniers mois (voir views._monthly_history),
    le mois en cours en dernier."""

    current = history[-1]
    closed = [
        row for row in history[:-1]
        if row["income"] or row["expense"] or row["savings"]
    ]
    months = len(closed) or 1
    avg_income = budget_calc.money(sum((row["income"] for row in closed), ZERO) / months)
    avg_expense = budget_calc.money(sum((row["expense"] for row in closed), ZERO) / months)
    total_income = sum((row["income"] for row in closed), ZERO)
    total_savings = sum((row["savings"] for row in closed), ZERO)

    wealth_info = wealth_summary(user, today, avg_expense)
    current_month = month_summary(user, today, current, avg_expense)
    projects = budget_calc.projects_overview(user, today)
    subscription_summary = subscription_detection.subscriptions_summary(user, today)
    plan = budget_calc.plan_for_month(user, today.year, today.month)
    savings_rate = budget_calc.percent(total_savings, total_income)

    return {
        "wealth": wealth_info,
        "current": current_month,
        "categories": category_breakdown(user, today),
        "projects": projects,
        "subscriptions": subscription_summary,
        "month_subscriptions": subscription_detection.month_subscriptions(user, today.year, today.month, today),
        "plan": plan,
        "kpis": {
            "months": len(closed),
            "avg_income": avg_income,
            "avg_expense": avg_expense,
            "savings_rate": savings_rate,
            "target_rate": budget_calc.percent(plan.savings_target_monthly, avg_income) if plan and avg_income else None,
        },
        "cashflow": forecast_calc.forecast(user, today),
        "global_goal": _global_goal(user, today, wealth_info, plan),
        "has_data": bool(closed) or bool(current["income"] or current["expense"] or current["savings"]),
    }
