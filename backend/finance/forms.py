import re

from django import forms
from django.utils.formats import date_format

from datetime import date

from .budget import FOOD_CATEGORY_NAME, SAVINGS_CATEGORY_NAME, add_months
from .models import (
    PORTFOLIO_KINDS,
    BalanceSnapshot,
    BankConnection,
    BudgetPlan,
    BudgetSection,
    SavingsGoal,
    Category,
    EntryType,
    IncomeMode,
    Instrument,
    MealVoucherSettings,
    OperationKind,
    PortfolioOperation,
    RecurrenceKind,
    RecurrenceUnit,
    RecurringItem,
    SavingsAccount,
    SavingsKind,
    SavingsProject,
    Subcategory,
    Transaction,
)
from .wealth import BALANCE_KIND_CHOICES, DEFAULT_CEILINGS, PORTFOLIO_KIND_CHOICES
from django.utils.translation import gettext as _, gettext_lazy

def month_label(value):
    return date_format(value, "F Y")


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
        label=gettext_lazy("Rendre cette ligne récurrente"),
    )
    recurring_kind = forms.ChoiceField(
        choices=RecurrenceKind.choices,
        initial=RecurrenceKind.SUBSCRIPTION,
        required=False,
        label=gettext_lazy("Type de récurrence"),
    )
    frequency_interval = forms.IntegerField(
        min_value=1,
        initial=1,
        required=False,
        label=gettext_lazy("Tous les"),
    )
    frequency_unit = forms.ChoiceField(
        choices=RecurrenceUnit.choices,
        initial=RecurrenceUnit.MONTH,
        required=False,
        label=gettext_lazy("Période"),
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
            "paid_with_meal_voucher": gettext_lazy("Payé en tickets restaurant"),
            "savings_project": gettext_lazy("Projet d'épargne"),
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
        self.fields["savings_project"].empty_label = _("Épargne libre (aucun projet)")
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
                _("Les tickets restaurant ne peuvent payer que des dépenses d'alimentation."),
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
        fields = ["name", "entry_type", "budget_section"]
        labels = {"budget_section": gettext_lazy("Type dans le budget")}
        help_texts = {"budget_section": gettext_lazy("Dépenses uniquement : type de la catégorie tant qu'elle n'a pas de sous-catégorie.")}

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
        fields = ["category", "name", "budget_section"]
        labels = {"budget_section": gettext_lazy("Type dans le budget")}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(user=user)
        self._style_fields()


class BankConnectionForm(BootstrapFormMixin, forms.ModelForm):
    # Plus long que le champ du modèle : on accepte "123 456 789 01", nettoyé ensuite
    login_id = forms.CharField(
        label=gettext_lazy("Identifiant Crédit Agricole"),
        max_length=20,
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "off", "placeholder": gettext_lazy("11 chiffres")}
        ),
    )

    class Meta:
        model = BankConnection
        fields = ["postal_code", "login_id"]
        labels = {"postal_code": gettext_lazy("Code postal de l'agence")}
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
            raise forms.ValidationError(_("Le code postal doit contenir 5 chiffres."))
        return value

    def clean_login_id(self):
        value = re.sub(r"\s", "", self.cleaned_data["login_id"])
        if not re.fullmatch(r"\d{11}", value):
            raise forms.ValidationError(_("L'identifiant doit contenir 11 chiffres."))
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
            label += _(" (ce mois-ci)")
        choices.append((value.isoformat(), label))
    return choices


class BudgetPlanForm(BootstrapFormMixin, forms.ModelForm):
    valid_from = forms.ChoiceField(label=gettext_lazy("Appliquer à partir de"))

    class Meta:
        model = BudgetPlan
        fields = ["income_mode", "manual_income", "income_average_months", "savings_target_monthly"]
        labels = {
            "income_mode": gettext_lazy("Revenu de référence"),
            "manual_income": gettext_lazy("Montant (€)"),
            "income_average_months": gettext_lazy("Moyenne sur (mois)"),
            "savings_target_monthly": gettext_lazy("Épargne visée par mois (€)"),
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
            self.add_error("manual_income", _("Indique ton revenu mensuel."))
        return cleaned_data


class BudgetLineForm(BootstrapFormMixin, forms.Form):
    section = forms.ChoiceField(choices=BudgetSection.choices, label=gettext_lazy("Section"))
    category = forms.ModelChoiceField(queryset=Category.objects.none(), label=gettext_lazy("Catégorie"))
    subcategory = forms.ModelChoiceField(
        queryset=Subcategory.objects.none(), required=False, label="Sous-catégorie"
    )
    label = forms.CharField(max_length=150, required=False, label=gettext_lazy("Libellé"))
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        label=gettext_lazy("Montant / mois"),
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
        self.fields["label"].widget.attrs["placeholder"] = _("Facultatif")

        # Chaque section ne propose que les catégories / sous-catégories de son type
        subcategories = {
            sub_id: (category_id, section)
            for sub_id, category_id, section in Subcategory.objects.filter(category__user=user)
            .values_list("id", "category_id", "budget_section")
        }
        self.category_sections = {
            category.id: category.budget_sections()
            for category in self.fields["category"].queryset.prefetch_related("subcategories")
        }

        def tag_options(widget, tag):
            original_create_option = widget.create_option

            def create_option(name, value, label, selected, index, subindex=None, attrs=None):
                option = original_create_option(name, value, label, selected, index, subindex, attrs)
                if value:
                    tag(option["attrs"], int(str(value)))
                return option

            widget.create_option = create_option

        def tag_category(attrs, pk):
            attrs["data-sections"] = " ".join(sorted(self.category_sections.get(pk, ())))

        def tag_subcategory(attrs, pk):
            category_id, section = subcategories.get(pk, (None, None))
            attrs["data-category"] = category_id
            attrs["data-section"] = section

        tag_options(self.fields["category"].widget, tag_category)
        tag_options(self.fields["subcategory"].widget, tag_subcategory)

    def clean(self):
        cleaned_data = super().clean()
        section = cleaned_data.get("section")
        category = cleaned_data.get("category")
        subcategory = cleaned_data.get("subcategory")
        if category and subcategory and subcategory.category_id != category.id:
            cleaned_data["subcategory"] = subcategory = None
        label = dict(BudgetSection.choices).get(section, "")
        if subcategory and section and subcategory.budget_section != section:
            self.add_error("subcategory", _("« %(name)s » est classée en %(type)s, pas en %(section)s (modifiable dans Paramètres → Catégories).") % {
                "name": subcategory, "type": subcategory.get_budget_section_display().lower(), "section": str(label).lower(),
            })
        elif category and not subcategory and section and section not in self.category_sections.get(category.id, ()):
            self.add_error("category", _("« %(name)s » n'a rien à mettre en %(section)s : choisis une autre catégorie ou change le type de ses sous-catégories dans Paramètres.") % {
                "name": category, "section": str(label).lower(),
            })
        return cleaned_data


BudgetLineFormSet = forms.formset_factory(BudgetLineForm, extra=0, can_delete=True)


class SavingsGoalForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = BudgetPlan
        fields = ["savings_target_monthly"]
        labels = {"savings_target_monthly": gettext_lazy("Épargne visée chaque mois (€)")}
        widgets = {
            "savings_target_monthly": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class CurrentBalanceForm(BootstrapFormMixin, forms.Form):
    date = forms.DateField(
        label=gettext_lazy("Date du solde"),
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    balance = forms.DecimalField(
        label=gettext_lazy("Solde du compte courant (€)"), max_digits=12, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )
    safety_buffer = forms.DecimalField(
        label=gettext_lazy("Marge de sécurité (€)"), max_digits=12, decimal_places=2, min_value=0,
        help_text=gettext_lazy("Le solde sous lequel tu ne veux pas descendre."),
        widget=forms.NumberInput(attrs={"step": "10", "min": "0"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > date.today():
            raise forms.ValidationError(_("Le solde ne peut pas être daté dans le futur."))
        return value


class GlobalSavingsGoalForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SavingsGoal
        fields = ["target_amount", "target_date", "expected_return", "extra_amount"]
        labels = {
            "target_amount": gettext_lazy("Montant visé (€)"),
            "target_date": gettext_lazy("Pour le"),
            "expected_return": gettext_lazy("Rendement espéré (%/an)"),
            "extra_amount": gettext_lazy("Épargne hors app (€)"),
        }
        help_texts = {
            "expected_return": gettext_lazy("Livret A ≈ 2,4 %, fonds euros ≈ 2,5 %, actions ≈ 5–7 % sur longue durée. 0 = sans rendement."),
            "extra_amount": gettext_lazy("Ce que tu as ailleurs : autre banque, espèces…"),
        }
        widgets = {
            "target_amount": forms.NumberInput(attrs={"step": "100", "min": "0"}),
            "target_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "expected_return": forms.NumberInput(attrs={"step": "0.1", "min": "0", "max": "20"}),
            "extra_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()

    def clean_target_date(self):
        value = self.cleaned_data["target_date"]
        if value <= date.today():
            raise forms.ValidationError(_("Choisis une date dans le futur."))
        return value

    def clean_expected_return(self):
        value = self.cleaned_data["expected_return"]
        if value < 0 or value > 20:
            raise forms.ValidationError(_("Entre 0 et 20 %."))
        return value


class SavingsProjectForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SavingsProject
        fields = ["name", "target_amount", "target_date", "initial_amount", "is_done"]
        labels = {
            "name": gettext_lazy("Projet"),
            "target_amount": gettext_lazy("Montant à réunir (€)"),
            "target_date": gettext_lazy("Pour quand ?"),
            "initial_amount": gettext_lazy("Déjà de côté (€)"),
            "is_done": gettext_lazy("Projet terminé"),
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": gettext_lazy("Ex. Vélo électrique")}),
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
            raise forms.ValidationError(_("Le montant doit être positif."))
        return value


class MealVoucherSettingsForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = MealVoucherSettings
        fields = ["enabled", "face_value", "employee_share_percent", "days_per_month"]
        labels = {
            "enabled": gettext_lazy("J'ai des tickets restaurant"),
            "face_value": gettext_lazy("Valeur d'un ticket (€)"),
            "employee_share_percent": gettext_lazy("Part payée par moi (%)"),
            "days_per_month": gettext_lazy("Tickets par mois"),
        }
        help_texts = {
            "employee_share_percent": gettext_lazy("Retenue sur ton salaire, souvent 40 à 50 %."),
            "days_per_month": gettext_lazy("Laisse vide pour compter les jours ouvrés du mois."),
        }
        widgets = {
            "face_value": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "employee_share_percent": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "days_per_month": forms.NumberInput(attrs={"min": "0", "max": "31", "placeholder": gettext_lazy("Auto")}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


# ============================================================
# ÉPARGNE
# ============================================================

class BalanceAccountForm(BootstrapFormMixin, forms.ModelForm):
    """Livret, PEL, assurance vie... : un support suivi par son solde."""

    balance = forms.DecimalField(
        label=gettext_lazy("Solde actuel (€)"),
        min_value=0,
        max_digits=12,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0", "placeholder": "0,00"}),
    )

    class Meta:
        model = SavingsAccount
        fields = ["kind", "name", "institution", "interest_rate", "ceiling"]
        labels = {
            "kind": gettext_lazy("Type"),
            "name": gettext_lazy("Nom"),
            "institution": gettext_lazy("Banque"),
            "interest_rate": gettext_lazy("Taux annuel (%)"),
            "ceiling": gettext_lazy("Plafond (€)"),
        }
        help_texts = {
            "ceiling": gettext_lazy("Laisse vide pour le plafond réglementaire."),
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": gettext_lazy("Ex. Livret A")}),
            "institution": forms.TextInput(attrs={"placeholder": gettext_lazy("Ex. Crédit Agricole")}),
            "interest_rate": forms.NumberInput(attrs={"step": "0.01", "min": "0", "placeholder": gettext_lazy("Ex. 1,70")}),
            "ceiling": forms.NumberInput(attrs={"step": "1", "min": "0", "placeholder": gettext_lazy("Auto")}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "livret_%s")
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = BALANCE_KIND_CHOICES
        self.fields["name"].required = False
        if self.instance.pk:
            del self.fields["balance"]
            self.fields["ceiling"].help_text = _("Laisse vide s'il n'y a pas de plafond.")
        self._style_fields()

    def clean(self):
        cleaned_data = super().clean()
        kind = cleaned_data.get("kind")
        if kind and not cleaned_data.get("name"):
            cleaned_data["name"] = SavingsKind(kind).label
        if kind and not self.instance.pk and cleaned_data.get("ceiling") is None:
            cleaned_data["ceiling"] = DEFAULT_CEILINGS.get(kind)
        return cleaned_data


class PortfolioAccountForm(BootstrapFormMixin, forms.ModelForm):
    """PEA ou compte-titres, suivi ligne par ligne."""

    class Meta:
        model = SavingsAccount
        fields = ["kind", "name", "institution", "opened_on", "cash_balance"]
        labels = {
            "kind": gettext_lazy("Type"),
            "name": gettext_lazy("Nom"),
            "institution": gettext_lazy("Courtier / banque"),
            "opened_on": gettext_lazy("Date d'ouverture"),
            "cash_balance": gettext_lazy("Liquidités (€)"),
        }
        help_texts = {
            "opened_on": gettext_lazy("Après 5 ans, le PEA garde son avantage fiscal."),
            "cash_balance": gettext_lazy("Espèces non investies sur le compte."),
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": gettext_lazy("Ex. PEA Boursorama")}),
            "institution": forms.TextInput(attrs={"placeholder": gettext_lazy("Ex. Boursorama, Fortuneo, Trade Republic")}),
            "opened_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "cash_balance": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "account_%s")
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = PORTFOLIO_KIND_CHOICES
        self.fields["name"].required = False
        self._style_fields()

    def clean(self):
        cleaned_data = super().clean()
        kind = cleaned_data.get("kind")
        if kind and not cleaned_data.get("name"):
            cleaned_data["name"] = SavingsKind(kind).label
        if cleaned_data.get("cash_balance") is None:
            cleaned_data["cash_balance"] = 0
        return cleaned_data

    def save(self, commit=True):
        account = super().save(commit=False)
        if account.kind == SavingsKind.PEA and account.ceiling is None:
            account.ceiling = DEFAULT_CEILINGS[SavingsKind.PEA]
        if commit:
            account.save()
        return account


class BalanceSnapshotForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = BalanceSnapshot
        fields = ["date", "balance"]
        labels = {"date": gettext_lazy("Date du solde"), "balance": gettext_lazy("Nouveau solde (€)")}
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "balance": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > date.today():
            raise forms.ValidationError(_("La date ne peut pas être dans le futur."))
        return value


class PortfolioOperationForm(BootstrapFormMixin, forms.ModelForm):
    """Ajout d'une ligne : le titre est choisi par la recherche (ISIN, nom
    ou ticker) et transmis par son ticker Yahoo Finance."""

    symbol = forms.CharField(max_length=32, required=False, widget=forms.HiddenInput)
    instrument_name = forms.CharField(max_length=200, required=False, widget=forms.HiddenInput)
    isin = forms.CharField(max_length=12, required=False, widget=forms.HiddenInput)
    exchange = forms.CharField(max_length=50, required=False, widget=forms.HiddenInput)
    quote_type = forms.CharField(max_length=20, required=False, widget=forms.HiddenInput)

    class Meta:
        model = PortfolioOperation
        fields = ["account", "kind", "date", "quantity", "unit_price", "fees", "amount", "note"]
        labels = {
            "account": gettext_lazy("Compte"),
            "kind": gettext_lazy("Opération"),
            "date": gettext_lazy("Date"),
            "quantity": gettext_lazy("Quantité"),
            "unit_price": gettext_lazy("Prix unitaire (€)"),
            "fees": gettext_lazy("Frais (€)"),
            "amount": gettext_lazy("Montant (€)"),
            "note": gettext_lazy("Libellé"),
        }
        widgets = {
            "kind": forms.RadioSelect,
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "quantity": forms.NumberInput(attrs={"step": "any", "min": "0", "placeholder": gettext_lazy("Ex. 10")}),
            "unit_price": forms.NumberInput(attrs={"step": "any", "min": "0", "placeholder": "0,00"}),
            "fees": forms.NumberInput(attrs={"step": "0.01", "min": "0", "placeholder": "0,00"}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0", "placeholder": "0,00"}),
            "note": forms.TextInput(attrs={"placeholder": gettext_lazy("Ex. Droits de garde 2026")}),
        }

    def __init__(self, *args, user=None, **kwargs):
        # Identifiants distincts de ceux de « + Nouvelle opération » (présent sur toutes les pages)
        kwargs.setdefault("auto_id", "op_%s")
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["account"].queryset = SavingsAccount.objects.filter(
            user=user, kind__in=PORTFOLIO_KINDS
        )
        self.fields["account"].empty_label = None
        self.fields["kind"].choices = [
            (OperationKind.BUY, _("Achat")),
            (OperationKind.POSITION, _("Déjà détenu")),
            (OperationKind.SELL, _("Vente")),
            (OperationKind.DIVIDEND, _("Dividende")),
            (OperationKind.FEE, _("Frais")),
        ]
        self.fields["fees"].required = False
        for name, field in self.fields.items():
            if not isinstance(field.widget, (forms.RadioSelect, forms.HiddenInput)):
                field.widget.attrs["class"] = (field.widget.attrs.get("class", "") + " form-control").strip()

    def clean(self):
        cleaned_data = super().clean()
        kind = cleaned_data.get("kind")
        day = cleaned_data.get("date")
        if day and day > date.today():
            self.add_error("date", _("La date ne peut pas être dans le futur."))
        if cleaned_data.get("fees") is None:
            cleaned_data["fees"] = 0

        if kind in {OperationKind.BUY, OperationKind.POSITION, OperationKind.SELL, OperationKind.DIVIDEND} and not cleaned_data.get("symbol"):
            raise forms.ValidationError(_("Choisis un titre dans la recherche (ISIN, nom ou ticker)."))

        if kind in {OperationKind.BUY, OperationKind.POSITION, OperationKind.SELL}:
            for name in ("quantity", "unit_price"):
                value = cleaned_data.get(name)
                if value is None or value <= 0:
                    self.add_error(name, _("Indique un nombre positif."))
            cleaned_data["amount"] = None
            if kind == OperationKind.POSITION:
                # Le PRU inclut déjà les frais payés à l'époque
                cleaned_data["fees"] = 0
        elif kind in {OperationKind.DIVIDEND, OperationKind.FEE}:
            amount = cleaned_data.get("amount")
            if amount is None or amount <= 0:
                self.add_error("amount", _("Indique un montant positif."))
            cleaned_data["quantity"] = None
            cleaned_data["unit_price"] = None
            cleaned_data["fees"] = 0
        if kind == OperationKind.FEE:
            cleaned_data["symbol"] = ""
        return cleaned_data


class PortfolioOperationEditForm(BootstrapFormMixin, forms.ModelForm):
    """Correction d'une ligne existante (le titre et le type ne changent pas)."""

    class Meta:
        model = PortfolioOperation
        fields = ["date", "quantity", "unit_price", "fees", "amount", "note"]
        labels = PortfolioOperationForm.Meta.labels
        widgets = {
            name: widget
            for name, widget in PortfolioOperationForm.Meta.widgets.items()
            if name in {"date", "quantity", "unit_price", "fees", "amount", "note"}
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.kind in {OperationKind.BUY, OperationKind.SELL}:
            del self.fields["amount"]
        elif self.instance.kind == OperationKind.POSITION:
            del self.fields["amount"]
            del self.fields["fees"]
            self.fields["unit_price"].label = _("PRU (€)")
            self.fields["date"].label = _("Détenu au")
        else:
            for name in ("quantity", "unit_price", "fees"):
                del self.fields[name]
        self._style_fields()

    def clean(self):
        cleaned_data = super().clean()
        day = cleaned_data.get("date")
        if day and day > date.today():
            self.add_error("date", _("La date ne peut pas être dans le futur."))
        for name in ("quantity", "unit_price", "amount"):
            if name in self.fields:
                value = cleaned_data.get(name)
                if value is None or value <= 0:
                    self.add_error(name, _("Indique un nombre positif."))
        if "fees" in self.fields and cleaned_data.get("fees") is None:
            cleaned_data["fees"] = 0
        return cleaned_data


class InstrumentForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Instrument
        fields = ["name", "isin", "asset_class", "zone", "ter"]
        labels = {
            "name": gettext_lazy("Nom"),
            "isin": "ISIN",
            "asset_class": gettext_lazy("Classe d'actifs"),
            "zone": gettext_lazy("Zone géographique"),
            "ter": gettext_lazy("Frais courants (% par an)"),
        }
        help_texts = {
            "ter": gettext_lazy("Le TER de l'ETF, indiqué sur sa fiche (ex. 0,38)."),
        }
        widgets = {
            "ter": forms.NumberInput(attrs={"step": "0.001", "min": "0", "max": "10", "placeholder": gettext_lazy("Ex. 0,38")}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()

    def clean_isin(self):
        return (self.cleaned_data.get("isin") or "").strip().upper()


class SimulationForm(forms.Form):
    """Choix de la simulation ; rendements, volatilités, corrélations et
    inflation sont calibrés sur l'historique."""

    years = forms.IntegerField(label=gettext_lazy("Horizon (années)"), min_value=1, max_value=40, initial=10)
    monthly_contribution = forms.DecimalField(
        label=gettext_lazy("Versement mensuel (€)"), min_value=0, max_digits=10, decimal_places=2, initial=0, required=False,
    )
    seed = forms.IntegerField(label=gettext_lazy("Tirage n°"), min_value=0, max_value=10**9, initial=1)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "sim_%s")
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-control", "step": "any"})

    def assumptions(self):
        from .simulation import Assumptions

        data = self.cleaned_data if self.is_bound and self.is_valid() else {
            name: field.initial for name, field in self.fields.items()
        }
        return Assumptions(
            years=int(data.get("years") or 10),
            monthly_contribution=float(data.get("monthly_contribution") or 0),
            seed=int(data.get("seed") or 0),
        )
