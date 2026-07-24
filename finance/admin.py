from django.contrib import admin

from .models import Category, MonthlyBudget, RecurringItem, Subcategory, Transaction


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "entry_type", "user", "order")
    list_filter = ("entry_type",)
    search_fields = ("name", "user__username", "user__email")


@admin.register(Subcategory)
class SubcategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "order")
    list_filter = ("category",)
    search_fields = ("name", "category__name")


@admin.register(RecurringItem)
class RecurringItemAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "entry_type",
        "default_amount",
        "category",
        "user",
        "is_active",
    )
    list_filter = ("entry_type", "is_active", "category")
    search_fields = ("name", "user__username", "user__email")


@admin.register(MonthlyBudget)
class MonthlyBudgetAdmin(admin.ModelAdmin):
    list_display = ("user", "year", "month", "started_at")
    list_filter = ("year", "month")
    search_fields = ("user__username", "user__email")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "transaction_type",
        "amount",
        "category",
        "subcategory",
        "transaction_date",
        "created_at",
    )
    list_filter = ("transaction_type", "category", "transaction_date")
    search_fields = ("description", "category__name", "user__username", "user__email")
    date_hierarchy = "transaction_date"
