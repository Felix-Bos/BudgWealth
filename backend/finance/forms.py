import re

from django import forms

from datetime import date

from .budget import FOOD_CATEGORY_NAME, SAVINGS_CATEGORY_NAME, add_months
from .models import (
    BankConnection,
    BudgetPlan,
    BudgetSection,
    Category,
    EntryType,
    IncomeMode,
    MealVoucherSettings,
    RecurrenceKind,
    RecurrenceUnit,
    RecurringItem,
    SavingsProject,
    Subcategory,
    Transaction,
)

MONTH_NAMES = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def month_label(value):
    return f"{MONTH_NAMES[value.month - 1]} {value.year}"


class BootstrapFormMixin:
    def _style_fields(self):
        for name, field in self.fields.items():
            existing = field.widget.attrs.get("class", "")
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = (existing + " form-check-input").strip()
            else:
                field.widget.attrs["class"] = (existing + " form-control").strip()


class EntryTypeAwareFormMixin:
    """Adds data-entry-type/data-category attrs to category/subcategory
    options and JS hooks so the template can filter category by type and
    hide the subcategory field for income entries (income categories have
    no subcategories)."""

    type_field_name = "entry_type"

    def _setup_entry_type_awareness(self, user, type_field_name=None):
        type_field_name = type_field_name or self.type_field_name
        self.fields[type_field_name].widget.attrs["class"] += " js-transaction-type"
        self.fields["category"].widget.attrs["class"] += " js-category-select"
        self.fields["subcategory"].widget.attrs["class"] += " js-subcategory-select"

        category_entry_types = dict(
            Category.objects.filter(user=user).values_list("id", "entry_type")
        )
        category_names = dict(Category.objects.filter(user=user).values_list("id", "name"))
        category_by_subcategory = dict(
            Subcategory.objects.filter(category__user=user).values_list(
                "id", "category_id"
            )
        )

        def category_attrs(value):
            category_id = int(str(value))
            entry_type = category_entry_types.get(category_id)
            attrs = {"data-entry-type": entry_type} if entry_type else {}
            # Repères pour afficher "payé en tickets resto" / "projet d'épargne"
            if category_names.get(category_id) == FOOD_CATEGORY_NAME:
                attrs["data-food"] = "1"
            if category_names.get(category_id) == SAVINGS_CATEGORY_NAME:
                attrs["data-savings"] = "1"
            return attrs

        def subcategory_attrs(value):
            category_id = category_by_subcategory.get(int(str(value)))
            entry_type = category_entry_types.get(category_id)
            attrs = {}
            if entry_type:
                attrs["data-entry-type"] = entry_type
            if category_id:
                attrs["data-category"] = category_id
            return attrs

        self._annotate_option_attrs("category", category_attrs)
        self._annotate_option_attrs("subcategory", subcategory_attrs)

    def _annotate_option_attrs(self, field_name, get_extra_attrs):
        widget = self.fields[field_name].widget
        original_create_option = widget.create_option

        def create_option(name, value, label, selected, index, subindex=None, attrs=None):
            option = original_create_option(
                name, value, label, selected, index, subindex, attrs
            )
            if value:
                option["attrs"].update(get_extra_attrs(value))
            return option

        widget.create_option = create_option


class TransactionForm(BootstrapFormMixin, EntryTypeAwareFormMixin, forms.ModelForm):
    type_field_name = "transaction_type"
    create_recurring = forms.BooleanField(
        required=False,
        label="Rendre cette ligne récurrente",
    )
    recurring_kind = forms.ChoiceField(
        choices=RecurrenceKind.choices,
        initial=RecurrenceKind.SUBSCRIPTION,
        required=False,
        label="Type de récurrence",
    )
    frequency_interval = forms.IntegerField(
        min_value=1,
        initial=1,
        required=False,
        label="Tous les",
    )
    frequency_unit = forms.ChoiceField(
        choices=RecurrenceUnit.choices,
        initial=RecurrenceUnit.MONTH,
        required=False,
        label="Période",
    )

    class Meta:
        model = Transaction
        fields = [
            "transaction_type",
            "amount",
            "category",
            "subcategory",
            "description",
            "transaction_date",
            "paid_with_meal_voucher",
            "savings_project",
        ]
        labels = {
            "paid_with_meal_voucher": "Payé en tickets restaurant",
            "savings_project": "Projet d'épargne",
        }
        widgets = {
            "transaction_date": forms.DateInput(attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["category"].queryset = Category.objects.filter(user=user)
        self.fields["subcategory"].queryset = Subcategory.objects.filter(
            category__user=user
        )
        self.fields["subcategory"].required = False
        self.fields["savings_project"].queryset = SavingsProject.objects.filter(
            user=user, is_done=False
        )
        self.fields["savings_project"].required = False
        self.fields["savings_project"].empty_label = "Épargne libre (aucun projet)"
        self._style_fields()
        self._setup_entry_type_awareness(user)
        self.fields["paid_with_meal_voucher"].widget.attrs["class"] += " js-meal-voucher"
        self.fields["savings_project"].widget.attrs["class"] += " js-savings-project"
        self.fields["create_recurring"].widget.attrs[
            "class"
        ] = "form-check-input js-create-recurring"
        self.fields["recurring_kind"].widget.attrs["class"] += " js-recurring-kind"
        self.fields["frequency_interval"].widget.attrs[
            "class"
        ] += " js-recurring-frequency"
        self.fields["frequency_unit"].widget.attrs[
            "class"
        ] += " js-recurring-frequency-unit"

    def clean(self):
        cleaned_data = super().clean()
        category = cleaned_data.get("category")
        is_expense = cleaned_data.get("transaction_type") == EntryType.EXPENSE

        # Les tickets restaurant ne servent qu'à l'alimentation
        if cleaned_data.get("paid_with_meal_voucher") and not (
            is_expense and category and category.name == FOOD_CATEGORY_NAME
        ):
            self.add_error(
                "paid_with_meal_voucher",
                "Les tickets restaurant ne peuvent payer que des dépenses d'alimentation.",
            )

        # Un projet ne reçoit que des versements d'épargne
        if cleaned_data.get("savings_project") and not (
            is_expense and category and category.name == SAVINGS_CATEGORY_NAME
        ):
            cleaned_data["savings_project"] = None

        if cleaned_data.get("create_recurring"):
            cleaned_data["recurring_kind"] = (
                cleaned_data.get("recurring_kind") or RecurrenceKind.SUBSCRIPTION
            )
            cleaned_data["frequency_interval"] = (
                cleaned_data.get("frequency_interval") or 1
            )
            cleaned_data["frequency_unit"] = (
                cleaned_data.get("frequency_unit") or RecurrenceUnit.MONTH
            )
        return cleaned_data


class RecurringItemForm(BootstrapFormMixin, EntryTypeAwareFormMixin, forms.ModelForm):
    class Meta:
        model = RecurringItem
        fields = [
            "name",
            "kind",
            "entry_type",
            "category",
            "subcategory",
            "default_amount",
            "frequency_interval",
            "frequency_unit",
            "start_date",
            "end_date",
            "is_active",
        ]
        widgets = {
            "default_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "frequency_interval": forms.NumberInput(attrs={"min": "1"}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["category"].queryset = Category.objects.filter(user=user)
        self.fields["subcategory"].queryset = Subcategory.objects.filter(
            category__user=user
        )
        self.fields["category"].required = False
        self.fields["subcategory"].required = False
        self.fields["start_date"].required = False
        self.fields["end_date"].required = False
        self.fields["frequency_interval"].initial = (
            self.fields["frequency_interval"].initial or 1
        )
        self._style_fields()
        self._setup_entry_type_awareness(user)


class MonthlyAmountForm(BootstrapFormMixin, forms.Form):
    """Used to tweak a single transaction's amount inline for the current month."""

    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class CategoryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "entry_type"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self._style_fields()

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.user = self.user
        if commit:
            instance.save()
        return instance


class SubcategoryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Subcategory
        fields = ["category", "name"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(user=user)
        self._style_fields()


class BankConnectionForm(BootstrapFormMixin, forms.ModelForm):
    # Plus long que le champ du modèle : on accepte "123 456 789 01", nettoyé ensuite
    login_id = forms.CharField(
        label="Identifiant Crédit Agricole",
        max_length=20,
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "off", "placeholder": "11 chiffres"}
        ),
    )

    class Meta:
        model = BankConnection
        fields = ["postal_code", "login_id"]
        labels = {"postal_code": "Code postal de l'agence"}
        widgets = {
            "postal_code": forms.TextInput(
                attrs={"inputmode": "numeric", "autocomplete": "postal-code", "placeholder": "75001"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()

    def clean_postal_code(self):
        value = self.cleaned_data["postal_code"].strip()
        if not re.fullmatch(r"\d{5}", value):
            raise forms.ValidationError("Le code postal doit contenir 5 chiffres.")
        return value

    def clean_login_id(self):
        value = re.sub(r"\s", "", self.cleaned_data["login_id"])
        if not re.fullmatch(r"\d{11}", value):
            raise forms.ValidationError("L'identifiant doit contenir 11 chiffres.")
        return value


# ============================================================
# BUDGET
# ============================================================

def valid_from_choices(today=None):
    """Mois à partir duquel un budget s'applique : ce mois-ci ou les
    suivants, et les 12 mois précédents pour comparer l'historique."""

    today = today or date.today()
    choices = []
    for delta in range(-12, 4):
        year, month = add_months(today.year, today.month, delta)
        value = date(year, month, 1)
        label = month_label(value)
        if delta == 0:
            label += " (ce mois-ci)"
        choices.append((value.isoformat(), label))
    return choices


class BudgetPlanForm(BootstrapFormMixin, forms.ModelForm):
    valid_from = forms.ChoiceField(label="Appliquer à partir de")

    class Meta:
        model = BudgetPlan
        fields = ["income_mode", "manual_income", "income_average_months", "savings_target_monthly"]
        labels = {
            "income_mode": "Revenu de référence",
            "manual_income": "Montant (€)",
            "income_average_months": "Moyenne sur (mois)",
            "savings_target_monthly": "Épargne visée par mois (€)",
        }
        widgets = {
            "manual_income": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "income_average_months": forms.NumberInput(attrs={"min": "1", "max": "24"}),
            "savings_target_monthly": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, today=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["valid_from"].choices = valid_from_choices(today)
        self.fields["income_mode"].widget = forms.RadioSelect(choices=IncomeMode.choices)
        self._style_fields()
        self.fields["income_mode"].widget.attrs["class"] = "js-income-mode"

    def clean_valid_from(self):
        return date.fromisoformat(self.cleaned_data["valid_from"])

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("income_mode") == IncomeMode.MANUAL and not cleaned_data.get("manual_income"):
            self.add_error("manual_income", "Indique ton revenu mensuel.")
        return cleaned_data


class BudgetLineForm(BootstrapFormMixin, forms.Form):
    section = forms.ChoiceField(choices=BudgetSection.choices, label="Section")
    category = forms.ModelChoiceField(queryset=Category.objects.none(), label="Catégorie")
    subcategory = forms.ModelChoiceField(
        queryset=Subcategory.objects.none(), required=False, label="Sous-catégorie"
    )
    label = forms.CharField(max_length=150, required=False, label="Libellé")
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        label="Montant / mois",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # L'épargne a son propre objectif : on ne la budgète pas comme une dépense
        self.fields["category"].queryset = Category.objects.filter(
            user=user, entry_type=EntryType.EXPENSE
        ).exclude(name=SAVINGS_CATEGORY_NAME)
        self.fields["subcategory"].queryset = Subcategory.objects.filter(
            category__user=user, category__entry_type=EntryType.EXPENSE
        )
        self._style_fields()
        self.fields["section"].widget.attrs["class"] += " js-line-section"
        self.fields["category"].widget.attrs["class"] += " js-category-select"
        self.fields["subcategory"].widget.attrs["class"] += " js-subcategory-select"
        self.fields["amount"].widget.attrs["class"] += " js-line-amount"
        self.fields["label"].widget.attrs["placeholder"] = "Facultatif"

        category_by_subcategory = dict(
            Subcategory.objects.filter(category__user=user).values_list("id", "category_id")
        )
        widget = self.fields["subcategory"].widget
        original_create_option = widget.create_option

        def create_option(name, value, label, selected, index, subindex=None, attrs=None):
            option = original_create_option(name, value, label, selected, index, subindex, attrs)
            if value:
                option["attrs"]["data-category"] = category_by_subcategory.get(int(str(value)))
            return option

        widget.create_option = create_option

    def clean(self):
        cleaned_data = super().clean()
        category = cleaned_data.get("category")
        subcategory = cleaned_data.get("subcategory")
        if category and subcategory and subcategory.category_id != category.id:
            cleaned_data["subcategory"] = None
        return cleaned_data


BudgetLineFormSet = forms.formset_factory(BudgetLineForm, extra=0, can_delete=True)


class SavingsGoalForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = BudgetPlan
        fields = ["savings_target_monthly"]
        labels = {"savings_target_monthly": "Épargne visée chaque mois (€)"}
        widgets = {
            "savings_target_monthly": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class SavingsProjectForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SavingsProject
        fields = ["name", "target_amount", "target_date", "initial_amount", "is_done"]
        labels = {
            "name": "Projet",
            "target_amount": "Montant à réunir (€)",
            "target_date": "Pour quand ?",
            "initial_amount": "Déjà de côté (€)",
            "is_done": "Projet terminé",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Ex. Vélo électrique"}),
            "target_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "target_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "initial_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()

    def clean_target_amount(self):
        value = self.cleaned_data["target_amount"]
        if value <= 0:
            raise forms.ValidationError("Le montant doit être positif.")
        return value


class MealVoucherSettingsForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = MealVoucherSettings
        fields = ["enabled", "face_value", "employee_share_percent", "days_per_month"]
        labels = {
            "enabled": "J'ai des tickets restaurant",
            "face_value": "Valeur d'un ticket (€)",
            "employee_share_percent": "Part payée par moi (%)",
            "days_per_month": "Tickets par mois",
        }
        help_texts = {
            "employee_share_percent": "Retenue sur ton salaire, souvent 40 à 50 %.",
            "days_per_month": "Laisse vide pour compter les jours ouvrés du mois.",
        }
        widgets = {
            "face_value": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "employee_share_percent": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "days_per_month": forms.NumberInput(attrs={"min": "0", "max": "31", "placeholder": "Auto"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
