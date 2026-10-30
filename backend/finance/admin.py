from django.contrib import admin

from .models import (
    BalanceSnapshot,
    BankConnection,
    BankSync,
    Category,
    Instrument,
    MonthlyBudget,
    PortfolioOperation,
    RecurringItem,
    SavingsAccount,
    Subcategory,
    Transaction,
)


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
        "kind",
        "entry_type",
        "default_amount",
        "category",
        "start_date",
        "end_date",
        "user",
        "is_active",
    )
    list_filter = ("kind", "entry_type", "is_active", "category")
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
        "source",
        "created_at",
    )
    list_filter = ("transaction_type", "source", "category", "transaction_date")
    search_fields = ("description", "category__name", "user__username", "user__email")
    date_hierarchy = "transaction_date"


@admin.register(BankConnection)
class BankConnectionAdmin(admin.ModelAdmin):
    list_display = ("user", "postal_code", "last_sync_at")
    search_fields = ("user__username", "user__email")


@admin.register(BankSync)
class BankSyncAdmin(admin.ModelAdmin):
    list_display = ("user", "year", "month", "status", "created_count", "skipped_count", "started_at")
    list_filter = ("status",)
    search_fields = ("user__username", "user__email")


@admin.register(SavingsAccount)
class SavingsAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "institution", "user", "interest_rate", "ceiling")
    list_filter = ("kind",)
    search_fields = ("name", "institution", "user__username", "user__email")


@admin.register(BalanceSnapshot)
class BalanceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("account", "date", "balance", "source")
    list_filter = ("source",)
    date_hierarchy = "date"


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("name", "symbol", "isin", "currency", "asset_class", "zone", "ter", "last_price", "last_price_date", "user")
    list_filter = ("asset_class", "zone", "quote_type")
    search_fields = ("name", "symbol", "isin")


@admin.register(PortfolioOperation)
class PortfolioOperationAdmin(admin.ModelAdmin):
    list_display = ("account", "kind", "instrument", "date", "quantity", "unit_price", "fees", "amount")
    list_filter = ("kind", "account")
    date_hierarchy = "date"
