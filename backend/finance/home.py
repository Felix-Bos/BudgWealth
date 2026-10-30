"""
Page d'accueil : patrimoine, mois en cours et conseils personnalisés.

Rien n'est calculé ici de zéro : on rassemble ce que produisent les modules
Budget (budget.py), Épargne (wealth.py) et Abonnements (subscriptions.py),
et on le traduit en quelques phrases simples, triées par importance.
"""

import calendar
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse

from . import budget as budget_calc
from . import subscriptions as subscription_detection
from . import wealth
from .models import (
    BankConnection,
    BudgetSection,
    EntryType,
    SavingsAccount,
    Transaction,
)

ZERO = Decimal("0")

# Matelas de sécurité conseillé : 3 à 6 mois de dépenses sur des livrets
SAFETY_MIN_MONTHS = 3
SAFETY_MAX_MONTHS = 6

# Une catégorie « s'emballe » si elle dépasse sa moyenne de 30 % et de 30 €
# (et qu'elle pèse au moins 25 € par mois d'habitude)
SPIKE_RATIO = Decimal("1.3")
SPIKE_MIN_GAP = Decimal("30")
SPIKE_MIN_AVERAGE = Decimal("25")

# Taux d'épargne : 20 % est le repère usuel, sous 10 % c'est fragile
SAVINGS_RATE_GOOD = 20
SAVINGS_RATE_LOW = 10

MAX_INSIGHTS = 6
TONE_ORDER = {"bad": 0, "warn": 1, "info": 2, "good": 3}


def _insight(tone, icon, title, text, url=None, cta=None):
    return {"tone": tone, "icon": icon, "title": title, "text": text, "url": url, "cta": cta}


def _euros(value):
    """1234.5 -> « 1 235 € » : les conseils parlent en euros ronds."""
    rounded = int(Decimal(value).quantize(Decimal("1")))
    return f"{rounded:,}".replace(",", " ") + " €"


def _number(value):
    """22.3 -> « 22,3 »"""
    return str(value).replace(".", ",")


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
    _, days_in_month = calendar.monthrange(today.year, today.month)
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
        rows.append({"name": "Autres", "total": budget_calc.money(others), "percent": budget_calc.percent(others, total)})
    return {"rows": rows, "total": budget_calc.money(total)}


def _category_spikes(user, today, closed_months):
    """Catégories du mois déjà au-dessus de leur moyenne des mois passés."""

    if not closed_months:
        return []
    recent = closed_months[-3:]
    start = budget_calc.month_start(recent[0]["year"], recent[0]["month"])
    end = budget_calc.month_start(today.year, today.month)

    def by_category(queryset):
        return {
            row["category__name"]: row["total"]
            for row in queryset.filter(transaction_type=EntryType.EXPENSE, category__isnull=False)
            .exclude(category__name=budget_calc.SAVINGS_CATEGORY_NAME)
            .values("category__name")
            .annotate(total=Sum("amount"))
        }

    base = Transaction.objects.filter(user=user)
    past = by_category(base.filter(transaction_date__gte=start, transaction_date__lt=end))
    current = by_category(base.filter(transaction_date__gte=end))

    spikes = []
    for name, amount in current.items():
        average = past.get(name, ZERO) / len(recent)
        if average >= SPIKE_MIN_AVERAGE and amount > average * SPIKE_RATIO and amount - average > SPIKE_MIN_GAP:
            spikes.append({
                "name": name,
                "amount": amount,
                "average": average,
                "percent": budget_calc.percent(amount - average, average),
            })
    spikes.sort(key=lambda item: -(item["amount"] - item["average"]))
    return spikes


# ============================================================
# CONSEILS
# ============================================================

def build_insights(user, today, month, wealth_info, projects, subscription_summary, closed_months, savings_rate):
    insights = []
    month_url = reverse("finance:month", args=[today.year, today.month])
    tracking = month["tracking"]

    # Budget du mois
    if tracking:
        if tracking["verdict"] == "off":
            insights.append(_insight(
                "bad", "alert", "Budget en danger",
                f"À ce rythme, tu finis le mois avec {_euros(tracking['projected_overrun'])} de dépassement.",
                month_url, "Voir le détail",
            ))
        elif tracking["verdict"] == "watch":
            text = (
                f"{tracking['over_count']} poste{'s' if tracking['over_count'] > 1 else ''} déjà dépassé{'s' if tracking['over_count'] > 1 else ''}."
                if tracking["over_count"]
                else "Quelques dépenses vont plus vite que prévu ou sortent du budget."
            )
            insights.append(_insight("warn", "eye", "Budget à surveiller", text, month_url, "Voir le détail"))
        elif month["days_left"] > 1:
            insights.append(_insight(
                "good", "check", "Budget sur la bonne voie",
                f"Tu peux encore dépenser {_euros(month['daily_allowance'])} par jour en dépenses variables.",
            ))

    # Catégories qui s'emballent
    for spike in _category_spikes(user, today, closed_months)[:2]:
        insights.append(_insight(
            "warn", "trend-up", f"{spike['name']} : +{spike['percent']} %",
            f"{_euros(spike['amount'])} ce mois-ci, contre {_euros(spike['average'])} en moyenne.",
            month_url, "Voir le mois",
        ))

    # Objectif d'épargne du mois
    if tracking and tracking["savings_target"] > 0 and tracking["savings_actual"] >= tracking["savings_target"]:
        insights.append(_insight(
            "good", "piggy", "Objectif d'épargne atteint",
            f"{_euros(tracking['savings_actual'])} mis de côté ce mois-ci, pour un objectif de {_euros(tracking['savings_target'])}.",
        ))

    # Taux d'épargne sur l'année
    if closed_months:
        if savings_rate >= SAVINGS_RATE_GOOD:
            insights.append(_insight(
                "good", "piggy", f"Tu épargnes {savings_rate} % de tes revenus",
                f"Sur les {len(closed_months)} derniers mois. Le repère usuel est de {SAVINGS_RATE_GOOD} % : bravo.",
            ))
        elif savings_rate < SAVINGS_RATE_LOW:
            insights.append(_insight(
                "warn", "piggy", f"Taux d'épargne de {savings_rate} %",
                f"Viser {SAVINGS_RATE_LOW} à {SAVINGS_RATE_GOOD} % de tes revenus te donnerait de la marge. "
                "Un virement automatique en début de mois aide beaucoup.",
                reverse("finance:budget_goals"), "Fixer un objectif",
            ))

    # Matelas de sécurité
    safety = wealth_info["safety_months"]
    if safety is not None:
        if safety < SAFETY_MIN_MONTHS:
            insights.append(_insight(
                "warn", "shield", "Matelas de sécurité à renforcer",
                f"Tes livrets couvrent {_number(safety)} mois de dépenses. Vise {SAFETY_MIN_MONTHS} à {SAFETY_MAX_MONTHS} mois avant d'investir.",
                reverse("finance:savings_balances"), "Mes livrets",
            ))
        elif safety > SAFETY_MAX_MONTHS * 2 and wealth_info["investments"] < wealth_info["livrets"]:
            insights.append(_insight(
                "info", "sprout", "Ton épargne pourrait travailler davantage",
                f"Tes livrets couvrent {_number(safety)} mois de dépenses. Au-delà de {SAFETY_MAX_MONTHS} mois, "
                "une partie peut viser le long terme (PEA, assurance vie).",
                reverse("finance:savings_portfolio"), "Mon portefeuille",
            ))
        else:
            insights.append(_insight(
                "good", "shield", "Matelas de sécurité solide",
                f"Tes livrets couvrent {_number(safety)} mois de dépenses.",
            ))

    # Livrets presque pleins
    balance_accounts = [a for a in SavingsAccount.objects.filter(user=user).prefetch_related("snapshots") if not a.is_portfolio]
    for row in wealth.balance_account_rows(balance_accounts, today):
        if row["fill"] is not None and row["fill"] >= 90:
            insights.append(_insight(
                "info", "layers", f"{row['account'].name} presque plein",
                f"Rempli à {row['fill']} % : il reste {_euros(row['room'])} avant le plafond.",
                reverse("finance:savings_balances"), "Mes livrets",
            ))
            break

    # Projets en retard
    late = [item for item in projects["projects"] if item["status"] in {"watch", "off"} and not item["project"].is_done]
    if late:
        item = late[0]
        insights.append(_insight(
            "warn", "flag", f"Projet « {item['project'].name} »",
            f"{item['status_label']}. Il faudrait {_euros(item['monthly_needed'])} par mois pour tenir l'échéance."
            if item["monthly_needed"] else f"{item['status_label']}.",
            reverse("finance:budget_goals"), "Mes projets",
        ))

    # Abonnements
    if subscription_summary["stopped_count"]:
        count = subscription_summary["stopped_count"]
        insights.append(_insight(
            "warn", "repeat", f"{count} abonnement{'s' if count > 1 else ''} plus payé{'s' if count > 1 else ''}",
            "Vérifie s'il s'agit d'une résiliation ou d'un oubli de paiement.",
            reverse("finance:budget_subscriptions"), "Vérifier",
        ))
    if subscription_summary["suggestion_count"]:
        count = subscription_summary["suggestion_count"]
        insights.append(_insight(
            "info", "repeat", f"{count} abonnement{'s' if count > 1 else ''} détecté{'s' if count > 1 else ''}",
            "Valide-les pour suivre leurs prélèvements chaque mois.",
            reverse("finance:budget_subscriptions"), "Valider",
        ))

    # Opérations à classer
    unclassified = Transaction.objects.filter(
        user=user,
        transaction_type=EntryType.EXPENSE,
        category__isnull=True,
        transaction_date__year=today.year,
        transaction_date__month=today.month,
    ).count()
    if unclassified >= 3:
        insights.append(_insight(
            "info", "tag", f"{unclassified} opérations à classer",
            "Classe-les pour des analyses et un suivi du budget plus justes.",
            month_url, "Les classer",
        ))

    # Premiers pas
    if not BankConnection.objects.filter(user=user).exists():
        insights.append(_insight(
            "info", "bank", "Connecte ta banque",
            "Tes opérations Crédit Agricole s'importent et se classent toutes seules.",
            reverse("finance:bank_connect"), "Connecter",
        ))
    if not wealth_info["has_accounts"]:
        insights.append(_insight(
            "info", "piggy", "Ajoute ton épargne",
            "Livret A, PEA, assurance vie… pour suivre tout ton patrimoine au même endroit.",
            reverse("finance:savings_balances"), "Ajouter",
        ))

    insights.sort(key=lambda item: TONE_ORDER[item["tone"]])
    return insights[:MAX_INSIGHTS]


# ============================================================
# ASSEMBLAGE
# ============================================================

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
        "insights": build_insights(
            user, today, current_month, wealth_info, projects, subscription_summary, closed, savings_rate
        ),
        "has_data": bool(closed) or bool(current["income"] or current["expense"] or current["savings"]),
    }
