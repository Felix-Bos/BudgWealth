from decimal import Decimal

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


class IncomeMode(models.TextChoices):
    AUTO = "AUTO", "Moyenne de mes salaires"
    MANUAL = "MANUAL", "Montant fixe"


class BudgetSection(models.TextChoices):
    FIXED = "FIXED", "Charges fixes"
    VARIABLE = "VARIABLE", "Charges variables"
    SUBSCRIPTION = "SUBSCRIPTION", "Abonnements"
    PROVISION = "PROVISION", "Provisions annuelles"


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
            return self.subcategory.name
        return self.category.name


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
        return self.name


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
    LIVRET_A = "LIVRET_A", "Livret A"
    LDDS = "LDDS", "LDDS"
    LEP = "LEP", "LEP"
    LIVRET_JEUNE = "LIVRET_JEUNE", "Livret Jeune"
    CEL = "CEL", "CEL"
    PEL = "PEL", "PEL"
    LIVRET = "LIVRET", "Autre livret"
    ASSURANCE_VIE = "ASSURANCE_VIE", "Assurance vie"
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
        return self.name

    @property
    def is_portfolio(self):
        return self.kind in PORTFOLIO_KINDS


class BalanceSource(models.TextChoices):
    MANUAL = "MANUAL", "Saisie manuelle"
    BANK = "BANK", "Import bancaire"


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


class AssetClass(models.TextChoices):
    EQUITY = "EQUITY", "Actions"
    BOND = "BOND", "Obligations"
    MONEY = "MONEY", "Monétaire"
    REAL_ESTATE = "REAL_ESTATE", "Immobilier"
    COMMODITY = "COMMODITY", "Or & matières premières"
    CRYPTO = "CRYPTO", "Crypto"
    OTHER = "OTHER", "Autre"


class Zone(models.TextChoices):
    WORLD = "WORLD", "Monde"
    US = "US", "États-Unis"
    EUROPE = "EUROPE", "Europe"
    FRANCE = "FRANCE", "France"
    EMERGING = "EMERGING", "Pays émergents"
    ASIA = "ASIA", "Japon & Asie"
    OTHER = "OTHER", "Autre"


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
    BUY = "BUY", "Achat"
    SELL = "SELL", "Vente"
    DIVIDEND = "DIVIDEND", "Dividende"
    FEE = "FEE", "Frais de compte"


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
        """Montant débité (achat, frais) ou crédité (vente, dividende)."""
        if self.kind == OperationKind.BUY:
            return self.gross + self.fees
        if self.kind == OperationKind.SELL:
            return self.gross - self.fees
        return self.amount or Decimal("0")
