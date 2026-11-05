from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext, gettext_lazy, ngettext


SALARY_CATEGORY_NAME = "Salaire"
SAVINGS_CATEGORY_NAME = "Épargne & placements"

# Salaire versé en fin de mois (ex. le 30/10) : il finance le mois suivant,
# il est donc compté le 1er de ce mois. Un salaire reçu en tout début de mois
# (versement décalé au 1er/2 à cause d'un week-end) reste dans son mois.
# L'épargne mise de côté le jour du salaire ou après, jusqu'à la fin du mois,
# suit le salaire ; celle d'avant le salaire reste dans son mois.
SALARY_NEXT_MONTH_FROM_DAY = 15


def salary_budget_date(day):
    """Date à laquelle un salaire reçu le `day` est compté dans le budget."""

    if day.day < SALARY_NEXT_MONTH_FROM_DAY:
        return day
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


class EntryType(models.TextChoices):
    EXPENSE = "EXPENSE", gettext_lazy("Dépense")
    INCOME = "INCOME", gettext_lazy("Revenu")


class TransactionSource(models.TextChoices):
    MANUAL = "MANUAL", gettext_lazy("Saisie manuelle")
    BANK = "BANK", gettext_lazy("Import bancaire")


class RecurrenceKind(models.TextChoices):
    FIXED = "FIXED", gettext_lazy("Poste fixe")
    SUBSCRIPTION = "SUBSCRIPTION", gettext_lazy("Abonnement")


class RecurrenceUnit(models.TextChoices):
    DAY = "DAY", gettext_lazy("jour")
    WEEK = "WEEK", gettext_lazy("semaine")
    MONTH = "MONTH", gettext_lazy("mois")
    YEAR = "YEAR", gettext_lazy("an")


class BudgetSection(models.TextChoices):
    FIXED = "FIXED", gettext_lazy("Charges fixes")
    VARIABLE = "VARIABLE", gettext_lazy("Charges variables")
    SUBSCRIPTION = "SUBSCRIPTION", gettext_lazy("Abonnements")
    PROVISION = "PROVISION", gettext_lazy("Provisions annuelles")


# Sections du budget rangées dans le groupe « charges fixes » (Paramètres)
FIXED_GROUP_SECTIONS = (BudgetSection.FIXED, BudgetSection.SUBSCRIPTION, BudgetSection.PROVISION)


class Category(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="categories",
    )
    name = models.CharField(max_length=100)
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    # Dépenses : section du budget de la catégorie quand elle n'a pas de
    # sous-catégorie, et type proposé pour ses nouvelles sous-catégories
    budget_section = models.CharField(
        max_length=20, choices=BudgetSection.choices, default=BudgetSection.VARIABLE
    )
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
        # Les catégories par défaut sont traduites à l'affichage
        return gettext(self.name)

    def budget_sections(self):
        """Sections du budget où la catégorie peut être choisie : celles de
        ses sous-catégories, ou la sienne si elle n'en a pas."""
        subcategories = list(self.subcategories.all())
        if subcategories:
            return {sub.budget_section for sub in subcategories}
        return {self.budget_section}


class Subcategory(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="subcategories",
    )
    name = models.CharField(max_length=100)
    # Section du budget où cette sous-catégorie peut être choisie
    budget_section = models.CharField(
        max_length=20, choices=BudgetSection.choices, default=BudgetSection.VARIABLE
    )
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
        # Les catégories par défaut sont traduites à l'affichage
        return gettext(self.name)

    @property
    def is_fixed(self):
        return self.budget_section in FIXED_GROUP_SECTIONS


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
    # Abonnement détecté sur le relevé bancaire : clé du commerçant utilisée
    # pour rattacher les paiements importés. Ces postes ne génèrent pas de
    # ligne automatique (la banque fournit déjà le paiement réel).
    match_key = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["entry_type", "kind", "name"]

    def __str__(self):
        return f"{self.name} ({self.default_amount})"

    @property
    def frequency_label(self):
        interval = self.frequency_interval or 1
        labels = {
            RecurrenceUnit.DAY: ngettext("Tous les jours", "Tous les %(count)s jours", interval),
            RecurrenceUnit.WEEK: ngettext("Toutes les semaines", "Toutes les %(count)s semaines", interval),
            RecurrenceUnit.MONTH: ngettext("Tous les mois", "Tous les %(count)s mois", interval),
            RecurrenceUnit.YEAR: ngettext("Tous les ans", "Tous les %(count)s ans", interval),
        }
        return labels.get(self.frequency_unit, labels[RecurrenceUnit.MONTH]) % {"count": interval}

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
    # Dépense alimentaire réglée avec la carte tickets restaurant :
    # elle consomme le solde de tickets, pas le compte bancaire.
    paid_with_meal_voucher = models.BooleanField(default=False)
    # Versement d'épargne affecté à un projet (ex. achat d'un vélo).
    savings_project = models.ForeignKey(
        "SavingsProject",
        on_delete=models.SET_NULL,
        related_name="contributions",
        null=True,
        blank=True,
    )
    # Date réelle de l'opération quand le budget la compte un autre jour
    # (salaire de fin de mois et épargne qui le suit, comptés le 1er du mois suivant).
    actual_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @staticmethod
    def budget_date(entry_type, category, day):
        """Date retenue dans le budget pour un salaire : celle de l'opération,
        sauf en fin de mois, reportée au 1er du mois suivant."""

        if entry_type == EntryType.INCOME and category is not None and category.name == SALARY_CATEGORY_NAME:
            return salary_budget_date(day)
        return day

    @property
    def is_salary(self):
        return (
            self.transaction_type == EntryType.INCOME
            and self.category is not None
            and self.category.name == SALARY_CATEGORY_NAME
        )

    @property
    def is_savings_transfer(self):
        return (
            self.transaction_type == EntryType.EXPENSE
            and self.category is not None
            and self.category.name == SAVINGS_CATEGORY_NAME
        )

    def _budget_date_for(self, real_date):
        """Date de budget d'une opération réellement faite le `real_date`."""

        if self.is_salary:
            return salary_budget_date(real_date)

        if self.is_savings_transfer:
            # Salaire de fin de mois déjà reçu ce mois-ci, avant ou le jour même
            salary = (
                Transaction.objects.filter(
                    user_id=self.user_id,
                    transaction_type=EntryType.INCOME,
                    category__name=SALARY_CATEGORY_NAME,
                    actual_date__year=real_date.year,
                    actual_date__month=real_date.month,
                    actual_date__lte=real_date,
                )
                .exclude(pk=self.pk)
                .order_by("actual_date")
                .first()
            )
            if salary:
                return salary.transaction_date

        return real_date

    def save(self, *args, **kwargs):
        if isinstance(self.transaction_date, date):
            # Ligne déjà décalée : on repart de sa date réelle, sauf si la
            # date a été changée depuis (édition à la main)
            real_date = self.transaction_date
            if self.actual_date and self._budget_date_for(self.actual_date) == self.transaction_date:
                real_date = self.actual_date

            target = self._budget_date_for(real_date)
            moved = target != self.transaction_date
            self.actual_date = real_date if target != real_date else None
            self.transaction_date = target
            if moved:
                self.monthly_budget = MonthlyBudget.objects.filter(
                    user_id=self.user_id, year=target.year, month=target.month
                ).first()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {
                    *kwargs["update_fields"], "transaction_date", "actual_date", "monthly_budget",
                }

        super().save(*args, **kwargs)

        if self.is_salary and self.actual_date:
            self._attach_savings_to_salary()

    def _attach_savings_to_salary(self):
        """L'épargne versée après ce salaire, dans le même mois, le suit."""

        # transaction_date du salaire = 1er du mois suivant sa réception
        savings = Transaction.objects.filter(
            user_id=self.user_id,
            transaction_type=EntryType.EXPENSE,
            category__name=SAVINGS_CATEGORY_NAME,
            transaction_date__gte=self.actual_date,
            transaction_date__lt=self.transaction_date,
        ).select_related("category")
        for saving in savings:
            saving.save(update_fields=["updated_at"])

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
    PENDING = "PENDING", gettext_lazy("En attente")
    CONNECTING = "CONNECTING", gettext_lazy("Connexion à la banque")
    WAITING_PASSWORD = "WAITING_PASSWORD", gettext_lazy("En attente du code personnel")
    DOWNLOADING = "DOWNLOADING", gettext_lazy("Téléchargement des opérations")
    IMPORTING = "IMPORTING", gettext_lazy("Import des opérations")
    DONE = "DONE", gettext_lazy("Terminé")
    ERROR = "ERROR", gettext_lazy("Erreur")


class BankSyncKind(models.TextChoices):
    MONTH = "MONTH", gettext_lazy("Un mois")
    INITIAL = "INITIAL", gettext_lazy("Import initial (12 mois)")


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


class IncomeMode(models.TextChoices):
    AUTO = "AUTO", gettext_lazy("Moyenne de mes salaires")
    MANUAL = "MANUAL", gettext_lazy("Montant fixe")


class BudgetPlan(models.Model):
    """Budget type : il s'applique à chaque mois à partir de `valid_from`,
    jusqu'à la version suivante. Les mois passés gardent la version qui
    était en vigueur, ce qui permet de les analyser."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="budget_plans",
    )
    # Toujours le 1er du mois
    valid_from = models.DateField()
    income_mode = models.CharField(
        max_length=10,
        choices=IncomeMode.choices,
        default=IncomeMode.AUTO,
    )
    manual_income = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    # Nombre de mois de salaire utilisés pour la moyenne (mode AUTO)
    income_average_months = models.PositiveSmallIntegerField(default=6)
    savings_target_monthly = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-valid_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "valid_from"],
                name="unique_budget_plan_per_month",
            ),
        ]

    def __str__(self):
        return f"{self.user} - budget depuis {self.valid_from:%m/%Y}"


class BudgetLine(models.Model):
    """Objectif de dépense mensuel pour une catégorie (ou sous-catégorie)."""

    plan = models.ForeignKey(
        BudgetPlan,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    section = models.CharField(max_length=20, choices=BudgetSection.choices)
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="budget_lines",
    )
    subcategory = models.ForeignKey(
        Subcategory,
        on_delete=models.CASCADE,
        related_name="budget_lines",
        null=True,
        blank=True,
    )
    label = models.CharField(max_length=150, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["section", "order", "id"]

    def __str__(self):
        return f"{self.display_label} ({self.amount})"

    @property
    def display_label(self):
        if self.label:
            return self.label
        if self.subcategory:
            return str(self.subcategory)
        return str(self.category)


class SavingsProject(models.Model):
    """Projet d'épargne : un montant à réunir pour une date donnée."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_projects",
    )
    name = models.CharField(max_length=150)
    target_amount = models.DecimalField(max_digits=12, decimal_places=2)
    target_date = models.DateField()
    # Ce qui était déjà de côté avant de suivre le projet dans l'app
    initial_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["is_done", "target_date", "name"]

    def __str__(self):
        # Les catégories par défaut sont traduites à l'affichage
        return gettext(self.name)


class SavingsGoal(models.Model):
    """Objectif d'épargne global : le total que l'on veut avoir de côté
    (livrets, PEA, assurance vie...) à une date donnée."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_goal",
    )
    target_amount = models.DecimalField(max_digits=12, decimal_places=2)
    target_date = models.DateField()
    # Rendement annuel moyen espéré de l'épargne (%), 0 = sans rendement
    expected_return = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("2"))
    # Épargne non suivie dans l'application (autre banque, espèces...)
    extra_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} - {self.target_amount} € au {self.target_date}"


class MealVoucherSettings(models.Model):
    """Tickets restaurant sur une carte dédiée (Swile, Edenred...)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meal_voucher_settings",
    )
    enabled = models.BooleanField(default=True)
    face_value = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("9.50"))
    # Part payée par le salarié (retenue sur le salaire), en % de la valeur faciale
    employee_share_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("50")
    )
    # Vide = jours ouvrés du mois (lundi-vendredi)
    days_per_month = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} - tickets restaurant {self.face_value} €"


class DismissedSubscription(models.Model):
    """Suggestion d'abonnement que l'utilisateur a choisi d'ignorer."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dismissed_subscriptions",
    )
    key = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_dismissed_subscription"),
        ]


# ============================================================
# ÉPARGNE : SUPPORTS, LIVRETS ET PORTEFEUILLES (PEA, CTO)
# ============================================================

class SavingsKind(models.TextChoices):
    LIVRET_A = "LIVRET_A", gettext_lazy("Livret A")
    LDDS = "LDDS", "LDDS"
    LEP = "LEP", "LEP"
    LIVRET_JEUNE = "LIVRET_JEUNE", gettext_lazy("Livret Jeune")
    CEL = "CEL", "CEL"
    PEL = "PEL", "PEL"
    LIVRET = "LIVRET", gettext_lazy("Autre livret")
    ASSURANCE_VIE = "ASSURANCE_VIE", gettext_lazy("Assurance vie")
    PER = "PER", "PER"
    PEA = "PEA", "PEA"
    CTO = "CTO", "Compte-titres"


# Supports suivis ligne par ligne (cours de bourse) ; les autres sont
# suivis par leur solde.
PORTFOLIO_KINDS = {SavingsKind.PEA, SavingsKind.CTO}


class SavingsAccount(models.Model):
    """Un support d'épargne : livret (suivi par son solde) ou portefeuille
    de titres (PEA, compte-titres), suivi ligne par ligne."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_accounts",
    )
    kind = models.CharField(max_length=20, choices=SavingsKind.choices)
    name = models.CharField(max_length=100)
    institution = models.CharField(max_length=100, blank=True)
    # Livrets : taux annuel en %
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # Plafond des versements (vide = pas de plafond)
    ceiling = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # PEA : l'ancienneté compte pour la fiscalité (5 ans)
    opened_on = models.DateField(null=True, blank=True)
    # PEA / CTO : espèces non investies
    cash_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Numéro du compte à la banque : rattache les soldes de l'import bancaire
    bank_account_number = models.CharField(max_length=30, blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "kind", "name"]

    def __str__(self):
        # Les catégories par défaut sont traduites à l'affichage
        return gettext(self.name)

    @property
    def is_portfolio(self):
        return self.kind in PORTFOLIO_KINDS


class BalanceSource(models.TextChoices):
    MANUAL = "MANUAL", gettext_lazy("Saisie manuelle")
    BANK = "BANK", gettext_lazy("Import bancaire")


class BalanceSnapshot(models.Model):
    """Solde d'un livret à une date : le dernier relevé est le solde actuel,
    l'ensemble trace son évolution."""

    account = models.ForeignKey(
        SavingsAccount,
        on_delete=models.CASCADE,
        related_name="snapshots",
    )
    date = models.DateField()
    balance = models.DecimalField(max_digits=12, decimal_places=2)
    source = models.CharField(
        max_length=10, choices=BalanceSource.choices, default=BalanceSource.MANUAL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["account", "date"], name="unique_balance_per_day"),
        ]

    def __str__(self):
        return f"{self.account} - {self.date} : {self.balance}"


class CurrentAccount(models.Model):
    """Compte courant : son solde sert de point de départ à la prévision de
    trésorerie. Rempli par la synchronisation bancaire, ou saisi à la main."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="current_account",
    )
    name = models.CharField(max_length=100, default="Compte courant")
    bank_account_number = models.CharField(max_length=30, blank=True, default="")
    # Seuil sous lequel on ne veut pas descendre (marge de sécurité)
    safety_buffer = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("200"))
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} - {self.name}"

    @property
    def latest_balance(self):
        return self.balances.first()


class CurrentAccountBalance(models.Model):
    account = models.ForeignKey(CurrentAccount, on_delete=models.CASCADE, related_name="balances")
    date = models.DateField()
    balance = models.DecimalField(max_digits=12, decimal_places=2)
    source = models.CharField(max_length=10, choices=BalanceSource.choices, default=BalanceSource.MANUAL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["account", "date"], name="unique_current_balance_per_day"),
        ]

    def __str__(self):
        return f"{self.account.name} {self.date} : {self.balance}"


class AssetClass(models.TextChoices):
    EQUITY = "EQUITY", gettext_lazy("Actions")
    BOND = "BOND", gettext_lazy("Obligations")
    MONEY = "MONEY", gettext_lazy("Monétaire")
    REAL_ESTATE = "REAL_ESTATE", gettext_lazy("Immobilier")
    COMMODITY = "COMMODITY", gettext_lazy("Or & matières premières")
    CRYPTO = "CRYPTO", gettext_lazy("Crypto")
    OTHER = "OTHER", gettext_lazy("Autre")


class Zone(models.TextChoices):
    WORLD = "WORLD", gettext_lazy("Monde")
    US = "US", gettext_lazy("États-Unis")
    EUROPE = "EUROPE", gettext_lazy("Europe")
    FRANCE = "FRANCE", gettext_lazy("France")
    EMERGING = "EMERGING", gettext_lazy("Pays émergents")
    ASIA = "ASIA", gettext_lazy("Japon & Asie")
    OTHER = "OTHER", gettext_lazy("Autre")


class Instrument(models.Model):
    """Titre coté (ETF, action...) identifié par son ticker Yahoo Finance."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="instruments",
    )
    symbol = models.CharField(max_length=32)
    isin = models.CharField(max_length=12, blank=True)
    name = models.CharField(max_length=200)
    exchange = models.CharField(max_length=50, blank=True)
    currency = models.CharField(max_length=8, default="EUR")
    quote_type = models.CharField(max_length=20, blank=True)
    asset_class = models.CharField(
        max_length=20, choices=AssetClass.choices, default=AssetClass.EQUITY
    )
    zone = models.CharField(max_length=20, choices=Zone.choices, default=Zone.WORLD)
    # Frais courants annuels de l'ETF / du fonds, en % (TER)
    ter = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True)
    last_price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    last_price_date = models.DateField(null=True, blank=True)
    prices_updated_at = models.DateTimeField(null=True, blank=True)
    price_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "symbol"], name="unique_instrument_per_user"),
        ]

    def __str__(self):
        return f"{self.name} ({self.symbol})"


class PricePoint(models.Model):
    """Cours de clôture quotidien, dans la devise de cotation."""

    instrument = models.ForeignKey(
        Instrument,
        on_delete=models.CASCADE,
        related_name="prices",
    )
    date = models.DateField()
    close = models.DecimalField(max_digits=16, decimal_places=4)

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["instrument", "date"], name="unique_price_per_day"),
        ]


class ExchangeRate(models.Model):
    """Valeur en euros d'une unité de devise (ex. 1 USD = 0,89 €)."""

    currency = models.CharField(max_length=8)
    date = models.DateField()
    rate = models.DecimalField(max_digits=16, decimal_places=8)

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["currency", "date"], name="unique_rate_per_day"),
        ]


class OperationKind(models.TextChoices):
    BUY = "BUY", gettext_lazy("Achat")
    # Titres déjà en portefeuille, repris avec leur prix de revient unitaire
    # (PRU) : ni frais, ni ordre de bourse ; valorisés au PRU à leur date,
    # puis au cours.
    POSITION = "POSITION", gettext_lazy("Déjà détenu")
    SELL = "SELL", gettext_lazy("Vente")
    DIVIDEND = "DIVIDEND", gettext_lazy("Dividende")
    FEE = "FEE", gettext_lazy("Frais de compte")


class PortfolioOperation(models.Model):
    """Une ligne de l'historique d'un PEA / compte-titres. Achats et ventes
    portent sur un titre (quantité, prix unitaire en euros, frais de
    courtage) ; dividendes et frais de compte (droits de garde...) n'ont
    qu'un montant."""

    account = models.ForeignKey(
        SavingsAccount,
        on_delete=models.CASCADE,
        related_name="operations",
    )
    instrument = models.ForeignKey(
        Instrument,
        on_delete=models.CASCADE,
        related_name="operations",
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=10, choices=OperationKind.choices)
    date = models.DateField()
    quantity = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    fees = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.instrument or ''} {self.date}"

    @property
    def gross(self):
        if self.quantity is None or self.unit_price is None:
            return self.amount or Decimal("0")
        return self.quantity * self.unit_price

    @property
    def total(self):
        """Montant débité (achat, frais) ou crédité (vente, dividende).
        Pour des titres déjà détenus : le montant investi (quantité × PRU)."""
        if self.kind == OperationKind.BUY:
            return self.gross + self.fees
        if self.kind == OperationKind.POSITION:
            return self.gross
        if self.kind == OperationKind.SELL:
            return self.gross - self.fees
        return self.amount or Decimal("0")
