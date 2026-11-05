"""
Objectif d'épargne global : combien mettre de côté chaque mois pour avoir
un montant donné à une date donnée, et est-ce réaliste ?

  - point de départ : épargne suivie dans l'app (livrets, PEA, CTO...) plus
    l'épargne déclarée à part ;
  - rendement : taux annuel espéré, capitalisé chaque mois ;
  - versement nécessaire P pour n mois au taux mensuel i :
        cible = départ × (1 + i)^n + P × ((1 + i)^n − 1) / i
  - réalisme : P comparé à ce que tu épargnes vraiment (moyenne des 6
    derniers mois complets) et à ce qu'il te reste une fois les charges
    payées (revenus − charges, même période).
"""

from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.utils.translation import gettext as _

from .budget import (
    ZERO,
    add_months,
    money,
    months_between,
    percent,
    totals_annotations,
)
from .models import Transaction

HISTORY_MONTHS = 6
MAX_MONTHS = 12 * 60


def monthly_rate(expected_return):
    return (1 + float(expected_return) / 100) ** (1 / 12) - 1


def future_value(start, monthly, rate, months):
    """Valeur après `months` versements mensuels (en fin de mois)."""
    if rate == 0:
        return start + monthly * months
    growth = (1 + rate) ** months
    return start * growth + monthly * (growth - 1) / rate


def required_monthly(start, target, rate, months):
    if months <= 0:
        return max(0.0, target - start)
    if rate == 0:
        return max(0.0, (target - start) / months)
    growth = (1 + rate) ** months
    return max(0.0, (target - start * growth) * rate / (growth - 1))


def months_to_reach(start, target, monthly, rate):
    """Mois nécessaires au rythme `monthly`, ou None au-delà de 60 ans."""
    value = start
    for month in range(0, MAX_MONTHS + 1):
        if value >= target:
            return month
        value = value * (1 + rate) + monthly
    return None


def saving_habits(user, today, months=HISTORY_MONTHS):
    """Moyennes mensuelles des derniers mois complets : épargne versée et
    reste après charges (revenus − charges)."""

    end_year, end_month = today.year, today.month
    start_year, start_month = add_months(end_year, end_month, -months)
    rows = (
        Transaction.objects.filter(
            user=user,
            transaction_date__gte=date(start_year, start_month, 1),
            transaction_date__lt=date(end_year, end_month, 1),
        )
        .values("transaction_date__year", "transaction_date__month")
        .annotate(**totals_annotations())
    )
    rows = list(rows)
    count = len(rows)
    if not count:
        return {"months": 0, "savings": ZERO, "capacity": ZERO, "income": ZERO}
    total = lambda key: sum((row[key] or ZERO for row in rows), ZERO)
    income, expense, savings = total("income"), total("expense"), total("savings")
    return {
        "months": count,
        "income": money(income / count),
        "savings": money(savings / count),
        "capacity": money(max(ZERO, income - expense) / count),
    }


def _month_label(year, month):
    from django.utils.formats import date_format
    return date_format(date(year, month, 1), "F Y")


def goal_overview(goal, user, current_savings, today=None, monthly_target=None):
    today = today or date.today()
    start = float(current_savings) + float(goal.extra_amount)
    target = float(goal.target_amount)
    rate = monthly_rate(goal.expected_return)
    this_month = today.replace(day=1)
    months = max(0, months_between(this_month, goal.target_date.replace(day=1)) - 1)
    habits = saving_habits(user, today)
    pace = float(habits["savings"])
    capacity = float(habits["capacity"])

    needed = required_monthly(start, target, rate, months)
    passed = goal.target_date < today
    reached = start >= target

    if reached:
        status, label = "done", _("Objectif déjà atteint")
        message = _("Tu as déjà de côté le montant visé : tu peux viser plus haut ou plus tôt.")
    elif passed or months == 0:
        status, label = "off", _("Date trop proche")
        message = _("Il ne reste pas de mois complet avant la date : repousse-la pour avoir un plan.")
    elif needed <= pace:
        status, label = "on", _("Réalisable à ton rythme actuel")
        message = _("Tu épargnes déjà assez chaque mois : continue comme ça.")
    elif needed <= capacity:
        status, label = "watch", _("Réalisable en épargnant plus")
        message = _("Il faut épargner plus qu'aujourd'hui, mais ça reste dans ce qu'il te reste une fois tes charges payées.")
    else:
        status, label = "off", _("Pas réaliste à cette date")
        message = _("Le versement nécessaire dépasse ce qu'il te reste chaque mois après tes charges.")

    # Quand l'atteindrait-on ? au rythme actuel et en épargnant tout le reste
    def reach(monthly):
        if monthly <= 0 and rate <= 0:
            return None
        n = months_to_reach(start, target, monthly, rate)
        if n is None:
            return None
        year, month = add_months(today.year, today.month, n)
        return {"months": n, "label": _month_label(year, month)}

    pace_reach = reach(pace) if not reached else None
    capacity_reach = reach(capacity) if status == "off" and not reached else None
    reachable_by_date = future_value(start, capacity, rate, months) if status == "off" and months else None

    # Projection mois par mois jusqu'à la date (ou jusqu'à l'atteinte au rythme actuel)
    horizon = max(months, min(pace_reach["months"], MAX_MONTHS) if pace_reach else 0, 12)
    horizon = min(horizon, max(months, 12) * 2)
    labels, plan_path, pace_path = [], [], []
    plan_value = pace_value = start
    for step in range(horizon + 1):
        year, month = add_months(today.year, today.month, step)
        labels.append(f"{year}-{month:02d}")
        plan_path.append(round(plan_value) if step <= months else None)
        pace_path.append(round(pace_value))
        plan_value = plan_value * (1 + rate) + needed
        pace_value = pace_value * (1 + rate) + pace

    contributions = needed * months
    final_plan = future_value(start, needed, rate, months)
    return {
        "goal": goal,
        "start": money(Decimal(str(start))),
        "tracked": money(current_savings),
        "remaining": money(Decimal(str(max(0.0, target - start)))),
        "percent": min(100, percent(Decimal(str(start)), goal.target_amount)),
        "months": months,
        "needed": money(Decimal(str(needed))),
        "contributions": money(Decimal(str(contributions))),
        "interest": money(Decimal(str(max(0.0, final_plan - start - contributions)))),
        "habits": habits,
        "pace_percent": min(100, round(pace / needed * 100)) if needed else 100,
        "capacity_percent": min(100, round(capacity / needed * 100)) if needed else 100,
        "needed_share_income": percent(Decimal(str(needed)), habits["income"]) if habits["income"] else None,
        "status": status,
        "status_label": label,
        "message": message,
        "pace_reach": pace_reach,
        "capacity_reach": capacity_reach,
        "reachable_by_date": money(Decimal(str(reachable_by_date))) if reachable_by_date is not None else None,
        "monthly_target": monthly_target,
        "chart": {
            "labels": labels,
            "plan": plan_path,
            "pace": pace_path,
            "target": target,
            "targetIndex": months,
        },
    }
