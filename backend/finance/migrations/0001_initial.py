# Generated for the initial BudgWealth project base.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Transaction",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "transaction_type",
                    models.CharField(
                        choices=[("EXPENSE", "Dépense"), ("INCOME", "Revenu")],
                        max_length=20,
                    ),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("description", models.CharField(blank=True, max_length=255)),
                ("category", models.CharField(blank=True, max_length=100)),
                ("transaction_date", models.DateField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-transaction_date", "-created_at"],
                "indexes": [
                    models.Index(fields=["user", "transaction_date"], name="finance_tra_user_id_bed389_idx"),
                    models.Index(fields=["user", "transaction_type"], name="finance_tra_user_id_75a566_idx"),
                ],
            },
        ),
    ]
