from datetime import date

from .forms import TransactionForm


def quick_add_transaction_form(request):
    if not request.user.is_authenticated:
        return {}
    today = date.today()
    return {
        "quick_add_transaction_form": TransactionForm(
            user=request.user, initial={"transaction_date": today}
        ),
        "quick_add_year": today.year,
        "quick_add_month": today.month,
    }
