from django import forms

from .models import Category, EntryType, RecurringItem, Subcategory, Transaction


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


class RecurringItemForm(BootstrapFormMixin, EntryTypeAwareFormMixin, forms.ModelForm):
    class Meta:
        model = RecurringItem
        fields = [
            "name",
            "entry_type",
            "category",
            "subcategory",
            "default_amount",
            "is_active",
        ]
        widgets = {
            "default_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
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
