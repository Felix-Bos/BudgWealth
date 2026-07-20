from django.contrib import admin

from .models import Transaction


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "transaction_type",
        "amount",
        "category",
        "transaction_date",
        "created_at",
    )
    list_filter = ("transaction_type", "category", "transaction_date")
    search_fields = ("description", "category", "user__username", "user__email")
    date_hierarchy = "transaction_date"
