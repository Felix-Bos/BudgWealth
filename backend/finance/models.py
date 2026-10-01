from django.conf import settings
from django.db import models


class EntryType(models.TextChoices):
    EXPENSE = "EXPENSE", "Dépense"
    INCOME = "INCOME", "Revenu"


class TransactionSource(models.TextChoices):
    MANUAL = "MANUAL", "Saisie manuelle"
    BANK = "BANK", "Import bancaire"


class RecurrenceKind(models.TextChoices):
    FIXED = "FIXED", "Poste fixe"
    SUBSCRIPTION = "SUBSCRIPTION", "Abonnement"


class RecurrenceUnit(models.TextChoices):
    DAY = "DAY", "jour"
    WEEK = "WEEK", "semaine"
    MONTH = "MONTH", "mois"
    YEAR = "YEAR", "an"


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
    kind = models.CharField(
        max_length=20,
        choices=RecurrenceKind.choices,
        default=RecurrenceKind.FIXED,
    )
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
    frequency_interval = models.PositiveSmallIntegerField(default=1)
    frequency_unit = models.CharField(
        max_length=20,
        choices=RecurrenceUnit.choices,
        default=RecurrenceUnit.MONTH,
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["entry_type", "kind", "name"]

    def __str__(self):
        return f"{self.name} ({self.default_amount})"

    @property
    def frequency_label(self):
        interval = self.frequency_interval or 1
        unit = self.get_frequency_unit_display()
        if interval == 1:
            return f"Tous les {unit}"
        plural = "mois" if self.frequency_unit == RecurrenceUnit.MONTH else f"{unit}s"
        return f"Tous les {interval} {plural}"

    @property
    def is_cancelled(self):
        return not self.is_active and self.end_date is not None


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
    source = models.CharField(
        max_length=20,
        choices=TransactionSource.choices,
        default=TransactionSource.MANUAL,
    )
    # Empreinte de l'opération bancaire : évite les doublons quand on
    # resynchronise un mois déjà importé.
    external_id = models.CharField(max_length=64, blank=True, default="")
    # Libellé brut de la banque (utile pour la catégorisation automatique).
    bank_label = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-transaction_date", "-created_at"]
        indexes = [
            models.Index(fields=["user", "transaction_date"]),
            models.Index(fields=["user", "transaction_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "external_id"],
                condition=~models.Q(external_id=""),
                name="unique_bank_operation_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.user} - {self.transaction_type} - {self.amount}"


class BankConnection(models.Model):
    """Paramètres de connexion à la banque. Le code personnel n'est jamais
    stocké : l'utilisateur le tape lui-même sur le site de la banque."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bank_connection",
    )
    postal_code = models.CharField(max_length=5)
    login_id = models.CharField(max_length=11)
    # L'import initial (12 mois d'historique) a-t-il déjà été fait ?
    initial_import_done = models.BooleanField(default=False)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} - Crédit Agricole ({self.postal_code})"


class BankSyncStatus(models.TextChoices):
    PENDING = "PENDING", "En attente"
    CONNECTING = "CONNECTING", "Connexion à la banque"
    WAITING_PASSWORD = "WAITING_PASSWORD", "En attente du code personnel"
    DOWNLOADING = "DOWNLOADING", "Téléchargement des opérations"
    IMPORTING = "IMPORTING", "Import des opérations"
    DONE = "DONE", "Terminé"
    ERROR = "ERROR", "Erreur"


class BankSyncKind(models.TextChoices):
    MONTH = "MONTH", "Un mois"
    INITIAL = "INITIAL", "Import initial (12 mois)"


class BankSync(models.Model):
    """Une synchronisation avec la banque, exécutée en arrière-plan.
    Soit un seul mois (MONTH), soit l'historique complet des 12 derniers mois
    (INITIAL), déclenché à la première connexion bancaire."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bank_syncs",
    )
    kind = models.CharField(
        max_length=10,
        choices=BankSyncKind.choices,
        default=BankSyncKind.MONTH,
    )
    # Pour un import mensuel : le mois visé. Pour l'import initial : le mois
    # de fin (le mois en cours) ; la période couvre les 12 mois précédents.
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=20,
        choices=BankSyncStatus.choices,
        default=BankSyncStatus.PENDING,
    )
    message = models.CharField(max_length=255, blank=True)
    created_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.user} - {self.get_kind_display()} - {self.month:02d}/{self.year} - {self.status}"

    @property
    def is_finished(self):
        return self.status in {BankSyncStatus.DONE, BankSyncStatus.ERROR}
