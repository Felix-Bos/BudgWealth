import re

from django import forms

from .models import (
    BankConnection,
    Category,
    EntryType,
    RecurrenceKind,
    RecurrenceUnit,
    RecurringItem,
    Subcategory,
    Transaction,
)


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
        category_by_subcategory = dict(
            Subcategory.objects.filter(category__user=user).values_list(
                "id", "category_id"
            )
        )

        def category_attrs(value):
            entry_type = category_entry_types.get(int(str(value)))
            return {"data-entry-type": entry_type} if entry_type else {}

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
        ]
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
        self._style_fields()
        self._setup_entry_type_awareness(user)
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
