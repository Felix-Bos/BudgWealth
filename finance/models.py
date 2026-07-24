from django.conf import settings
from django.db import models


class EntryType(models.TextChoices):
    EXPENSE = "EXPENSE", "Dépense"
    INCOME = "INCOME", "Revenu"


class Category(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="categories",
    )
    name = models.CharField(max_length=100)
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["entry_type", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name", "entry_type"],
                name="unique_category_per_user",
            ),
        ]

    def __str__(self):
        return self.name


class Subcategory(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="subcategories",
    )
    name = models.CharField(max_length=100)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "name"],
                name="unique_subcategory_per_category",
            ),
        ]

    def __str__(self):
        return self.name


class RecurringItem(models.Model):
    """A fixed monthly item (rent, salary, subscription...) with a default
    amount. Its amount can be overridden for a single month without
    changing the template itself."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recurring_items",
    )
    name = models.CharField(max_length=150)
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        related_name="recurring_items",
        null=True,
        blank=True,
    )
    subcategory = models.ForeignKey(
        Subcategory,
        on_delete=models.SET_NULL,
        related_name="recurring_items",
        null=True,
        blank=True,
    )
    default_amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["entry_type", "name"]

    def __str__(self):
        return f"{self.name} ({self.default_amount})"


class MonthlyBudget(models.Model):
    """One row per user per calendar month. Existence of this row means
    the month has been started and recurring items generated."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="monthly_budgets",
    )
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "year", "month"],
                name="unique_monthly_budget_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.user} - {self.month:02d}/{self.year}"


class Transaction(models.Model):
    TransactionType = EntryType

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    monthly_budget = models.ForeignKey(
        MonthlyBudget,
        on_delete=models.CASCADE,
        related_name="transactions",
        null=True,
        blank=True,
    )
    recurring_item = models.ForeignKey(
        RecurringItem,
        on_delete=models.SET_NULL,
        related_name="transactions",
        null=True,
        blank=True,
    )
    transaction_type = models.CharField(
        max_length=20,
        choices=EntryType.choices,
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        related_name="transactions",
        null=True,
        blank=True,
    )
    subcategory = models.ForeignKey(
        Subcategory,
        on_delete=models.SET_NULL,
        related_name="transactions",
        null=True,
        blank=True,
    )
    transaction_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-transaction_date", "-created_at"]
        indexes = [
            models.Index(fields=["user", "transaction_date"]),
            models.Index(fields=["user", "transaction_type"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.transaction_type} - {self.amount}"
