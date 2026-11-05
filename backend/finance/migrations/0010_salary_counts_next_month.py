"""
Salaire de fin de mois compté pour le mois suivant (voir models.salary_budget_date) :
le salaire reçu le 30/10 finance novembre, comme l'épargne versée le jour même
ou après, jusqu'à la fin du mois. La date réelle est gardée dans `actual_date`.
"""

from django.db import migrations, models

from finance.models import SALARY_CATEGORY_NAME, SAVINGS_CATEGORY_NAME, salary_budget_date


def shift_salaries(apps, schema_editor):
    Transaction = apps.get_model("finance", "Transaction")
    MonthlyBudget = apps.get_model("finance", "MonthlyBudget")

    def move(transaction, target):
        transaction.actual_date = transaction.transaction_date
        transaction.transaction_date = target
        transaction.monthly_budget = MonthlyBudget.objects.filter(
            user_id=transaction.user_id, year=target.year, month=target.month
        ).first()
        transaction.save(update_fields=["transaction_date", "actual_date", "monthly_budget"])

    salaries = Transaction.objects.filter(
        transaction_type="INCOME", category__name=SALARY_CATEGORY_NAME, actual_date__isnull=True
    )
    for salary in salaries:
        target = salary_budget_date(salary.transaction_date)
        if target == salary.transaction_date:
            continue
        received = salary.transaction_date
        move(salary, target)

        savings = Transaction.objects.filter(
            user_id=salary.user_id,
            transaction_type="EXPENSE",
            category__name=SAVINGS_CATEGORY_NAME,
            transaction_date__gte=received,
            transaction_date__lt=target,
        )
        for saving in savings:
            move(saving, target)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0009_savings"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="actual_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(shift_salaries, migrations.RunPython.noop),
    ]
