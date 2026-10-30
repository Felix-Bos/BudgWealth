"""
Pages du module Épargne : vue d'ensemble, livrets, portefeuille (PEA,
compte-titres) et frais. Les calculs sont dans wealth.py, l'accès à Yahoo
Finance dans market_data.py.
"""

from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from . import market_data
from . import wealth
from .forms import (
    BalanceAccountForm,
    BalanceSnapshotForm,
    InstrumentForm,
    PortfolioAccountForm,
    PortfolioOperationEditForm,
    PortfolioOperationForm,
)
from .models import (
    PORTFOLIO_KINDS,
    BalanceSnapshot,
    BalanceSource,
    BankConnection,
    Instrument,
    OperationKind,
    PortfolioOperation,
    SavingsAccount,
    SavingsKind,
)


def _form_errors(form):
    return " ".join(error for errors in form.errors.values() for error in errors)


def _next_url(request, default):
    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return default


def _chart_items(items):
    return [
        {"name": item["name"], "value": str(item["value"]), "percent": str(item["percent"])}
        for item in items
    ]


def _portfolio_scope(request):
    """Comptes-titres de l'utilisateur et compte sélectionné (?compte=<id>).
    Sans choix, un compte unique est sélectionné d'office."""

    accounts = list(SavingsAccount.objects.filter(user=request.user, kind__in=PORTFOLIO_KINDS))
    key = request.GET.get("compte", "")
    selected = None
    if key and key != "tous":
        selected = next((account for account in accounts if str(account.pk) == key), None)
    elif not key and len(accounts) == 1:
        selected = accounts[0]
    return accounts, selected


def _portfolio_url(account=None):
    url = reverse("finance:savings_portfolio")
    return f"{url}?compte={account.pk}" if account else url


def _refresh_urls():
    return {
        "refresh": reverse("finance:refresh_prices"),
        "search": reverse("finance:search_instruments"),
        "price": reverse("finance:instrument_price"),
    }


# ============================================================
# PAGES
# ============================================================

@login_required
def savings_overview_view(request):
    today = date.today()
    overview = wealth.savings_overview(request.user, today)
    context = {
        "savings_tab": "overview",
        "overview": overview,
        "portfolio": overview["portfolio"],
        "prices_stale": market_data.prices_are_stale(request.user),
        "today": today,
        "savings_data": {
            "history": overview["history"],
            "allocation": {
                "supports": _chart_items(overview["allocation"]["supports"]),
                "classes": _chart_items(overview["allocation"]["classes"]),
            },
            "urls": _refresh_urls(),
        },
    }
    return render(request, "finance/savings_overview.html", context)


@login_required
def savings_balances_view(request):
    today = date.today()
    accounts = (
        SavingsAccount.objects.filter(user=request.user)
        .exclude(kind__in=PORTFOLIO_KINDS)
        .prefetch_related("snapshots")
    )
    rows = wealth.balance_account_rows(accounts, today)
    for row in rows:
        pk = row["account"].pk
        row["snapshot_form"] = BalanceSnapshotForm(prefix=f"s{pk}", initial={"date": today})
        row["edit_form"] = BalanceAccountForm(instance=row["account"], prefix=f"a{pk}")

    context = {
        "savings_tab": "livrets",
        "rows": rows,
        "totals": wealth.balance_totals(rows),
        "account_form": BalanceAccountForm(),
        "has_bank_connection": BankConnection.objects.filter(user=request.user).exists(),
        "savings_data": {
            "sparklines": {
                str(row["account"].pk): {
                    "dates": [day.isoformat() for day, _ in row["series"]],
                    "values": [str(value) for _, value in row["series"]],
                }
                for row in rows
            },
        },
    }
    return render(request, "finance/savings_balances.html", context)


def _held_instruments(analysis):
    """Titres détenus, proposés en raccourci dans le formulaire d'ajout."""

    seen = {}
    for row in analysis["positions"]:
        instrument = row["instrument"]
        seen.setdefault(instrument.pk, {
            "symbol": instrument.symbol,
            "name": instrument.name,
            "isin": instrument.isin,
            "exchange": instrument.exchange,
            "quote_type": instrument.quote_type,
        })
    return list(seen.values())


@login_required
def savings_portfolio_view(request):
    today = date.today()
    accounts, selected = _portfolio_scope(request)
    analysis = wealth.analyse_portfolio([selected] if selected else accounts, today) if accounts else None

    if analysis:
        for row in analysis["positions"]:
            row["instrument_form"] = InstrumentForm(instance=row["instrument"], prefix=f"i{row['instrument'].pk}")
        analysis["operation_rows"] = [
            {"op": op, "form": PortfolioOperationEditForm(instance=op, prefix=f"op{op.pk}")}
            for op in analysis["operations"]
        ]

    context = {
        "savings_tab": "portefeuille",
        "accounts": accounts,
        "selected": selected,
        "analysis": analysis,
        "prices_stale": market_data.prices_are_stale(request.user),
        "operation_form": PortfolioOperationForm(
            user=request.user,
            initial={
                "kind": OperationKind.BUY,
                "date": today,
                "account": selected or (accounts[0] if accounts else None),
            },
        ),
        "account_form": PortfolioAccountForm(initial={"kind": SavingsKind.PEA}),
        "edit_account_form": PortfolioAccountForm(instance=selected, prefix="acc") if selected else None,
        "savings_data": {
            "history": analysis["history"] if analysis else None,
            "allocation": {
                key: _chart_items(items) for key, items in analysis["allocation"].items()
            } if analysis else None,
            "held": _held_instruments(analysis) if analysis else [],
            "urls": _refresh_urls(),
        },
    }
    return render(request, "finance/savings_portfolio.html", context)


@login_required
def savings_fees_view(request):
    today = date.today()
    accounts, selected = _portfolio_scope(request)
    analysis = wealth.analyse_portfolio([selected] if selected else accounts, today) if accounts else None
    fee_operations = [
        op for op in (analysis["operations"] if analysis else [])
        if op.kind == OperationKind.FEE or op.fees
    ]

    context = {
        "savings_tab": "frais",
        "accounts": accounts,
        "selected": selected,
        "analysis": analysis,
        "fee_operations": fee_operations,
        "prices_stale": market_data.prices_are_stale(request.user),
        "fee_form": PortfolioOperationForm(
            user=request.user,
            initial={
                "kind": OperationKind.FEE,
                "date": today,
                "account": selected or (accounts[0] if accounts else None),
                "note": "Droits de garde",
            },
        ),
        "savings_data": {
            "fees": analysis["history"] if analysis else None,
            "urls": _refresh_urls(),
        },
    }
    return render(request, "finance/savings_fees.html", context)


# ============================================================
# SUPPORTS (LIVRETS, PEA, CTO)
# ============================================================

@login_required
@require_POST
def add_balance_account(request):
    form = BalanceAccountForm(request.POST)
    if form.is_valid():
        account = form.save(commit=False)
        account.user = request.user
        account.save()
        balance = form.cleaned_data.get("balance")
        if balance is not None:
            BalanceSnapshot.objects.create(account=account, date=date.today(), balance=balance)
        messages.success(request, f"« {account.name} » ajouté.")
    else:
        messages.error(request, f"Impossible d'ajouter le livret. {_form_errors(form)}")
    return redirect("finance:savings_balances")


@login_required
@require_POST
def add_portfolio_account(request):
    form = PortfolioAccountForm(request.POST)
    if form.is_valid():
        account = form.save(commit=False)
        account.user = request.user
        account.save()
        messages.success(request, f"« {account.name} » créé. Ajoute maintenant tes lignes.")
        return redirect(_portfolio_url(account))
    messages.error(request, f"Impossible de créer le compte. {_form_errors(form)}")
    return redirect("finance:savings_portfolio")


@login_required
@require_POST
def edit_savings_account(request, pk):
    account = get_object_or_404(SavingsAccount, pk=pk, user=request.user)
    if account.is_portfolio:
        form = PortfolioAccountForm(request.POST, instance=account, prefix="acc")
        default = _portfolio_url(account)
    else:
        form = BalanceAccountForm(request.POST, instance=account, prefix=f"a{pk}")
        default = reverse("finance:savings_balances")
    if form.is_valid():
        form.save()
        messages.success(request, f"« {account.name} » mis à jour.")
    else:
        messages.error(request, f"Impossible de mettre à jour le support. {_form_errors(form)}")
    return redirect(_next_url(request, default))


@login_required
@require_POST
def delete_savings_account(request, pk):
    account = get_object_or_404(SavingsAccount, pk=pk, user=request.user)
    name = account.name
    is_portfolio = account.is_portfolio
    account.delete()
    messages.success(request, f"« {name} » supprimé.")
    return redirect("finance:savings_portfolio" if is_portfolio else "finance:savings_balances")


@login_required
@require_POST
def add_balance_snapshot(request, pk):
    account = get_object_or_404(SavingsAccount, pk=pk, user=request.user)
    form = BalanceSnapshotForm(request.POST, prefix=f"s{pk}")
    if account.is_portfolio:
        messages.error(request, "Un PEA ou un compte-titres se suit ligne par ligne.")
    elif form.is_valid():
        BalanceSnapshot.objects.update_or_create(
            account=account,
            date=form.cleaned_data["date"],
            defaults={"balance": form.cleaned_data["balance"], "source": BalanceSource.MANUAL},
        )
        messages.success(request, f"Solde de « {account.name} » mis à jour.")
    else:
        messages.error(request, f"Solde invalide. {_form_errors(form)}")
    return redirect(_next_url(request, reverse("finance:savings_balances")))


@login_required
@require_POST
def delete_balance_snapshot(request, pk):
    snapshot = get_object_or_404(BalanceSnapshot, pk=pk, account__user=request.user)
    snapshot.delete()
    messages.success(request, "Relevé supprimé.")
    return redirect("finance:savings_balances")


# ============================================================
# LIGNES DU PORTEFEUILLE
# ============================================================

def _ensure_prices(instrument, day, created):
    """Télécharge les cours d'un titre nouveau, ou plus anciens que ceux
    déjà en base. Renvoie False si Yahoo n'a pas répondu."""

    if not created and instrument.prices.filter(date__lte=day).exists():
        return True
    return market_data.refresh_instrument(instrument, market_data.first_needed_date(instrument))


def _check_sale(account, instrument, day, quantity, exclude_pk=None):
    held = wealth.held_quantity(account, instrument, day, exclude_pk=exclude_pk)
    if quantity > held:
        return (
            f"Vente impossible : tu détiens {wealth.clean_quantity(held)} part(s) de "
            f"« {instrument.name} » au {day:%d/%m/%Y}."
        )
    return None


@login_required
@require_POST
def add_portfolio_operation(request):
    form = PortfolioOperationForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, f"Ligne non ajoutée. {_form_errors(form)}")
        return redirect(_next_url(request, reverse("finance:savings_portfolio")))

    data = form.cleaned_data
    operation = form.save(commit=False)
    created = False
    if data.get("symbol"):
        operation.instrument, created = wealth.get_or_create_instrument(
            request.user,
            data["symbol"].strip().upper(),
            name=data.get("instrument_name") or "",
            isin=(data.get("isin") or "").strip().upper(),
            exchange=data.get("exchange") or "",
            quote_type=data.get("quote_type") or "",
        )

    if operation.kind == OperationKind.SELL:
        error = _check_sale(operation.account, operation.instrument, operation.date, operation.quantity)
        if error:
            messages.error(request, error)
            return redirect(_next_url(request, _portfolio_url(operation.account)))

    operation.save()

    label = {
        OperationKind.BUY: "Achat",
        OperationKind.SELL: "Vente",
        OperationKind.DIVIDEND: "Dividende",
        OperationKind.FEE: "Frais",
    }[operation.kind]
    target = f" de « {operation.instrument.name} »" if operation.instrument else ""
    if operation.instrument and not _ensure_prices(operation.instrument, operation.date, created):
        messages.warning(
            request,
            f"{label}{target} enregistré, mais les cours n'ont pas pu être récupérés "
            f"({operation.instrument.price_error}). La ligne est valorisée à son prix d'achat en attendant.",
        )
    else:
        messages.success(request, f"{label}{target} enregistré.")
    return redirect(_next_url(request, _portfolio_url(operation.account)))


@login_required
@require_POST
def edit_portfolio_operation(request, pk):
    operation = get_object_or_404(PortfolioOperation, pk=pk, account__user=request.user)
    form = PortfolioOperationEditForm(request.POST, instance=operation, prefix=f"op{pk}")
    default = _portfolio_url(operation.account)
    if not form.is_valid():
        messages.error(request, f"Ligne non modifiée. {_form_errors(form)}")
        return redirect(_next_url(request, default))

    operation = form.save(commit=False)
    if operation.kind == OperationKind.SELL:
        error = _check_sale(operation.account, operation.instrument, operation.date, operation.quantity, exclude_pk=pk)
        if error:
            messages.error(request, error)
            return redirect(_next_url(request, default))
    operation.save()
    if operation.instrument:
        _ensure_prices(operation.instrument, operation.date, created=False)
    messages.success(request, "Ligne mise à jour.")
    return redirect(_next_url(request, default))


@login_required
@require_POST
def delete_portfolio_operation(request, pk):
    operation = get_object_or_404(PortfolioOperation, pk=pk, account__user=request.user)
    account = operation.account
    operation.delete()
    messages.success(request, "Ligne supprimée.")
    return redirect(_next_url(request, _portfolio_url(account)))


@login_required
@require_POST
def delete_portfolio_position(request, account_pk, instrument_pk):
    """Supprime une ligne du portefeuille : tous ses achats, ventes et dividendes."""

    account = get_object_or_404(SavingsAccount, pk=account_pk, user=request.user)
    instrument = get_object_or_404(Instrument, pk=instrument_pk, user=request.user)
    count, _ = PortfolioOperation.objects.filter(account=account, instrument=instrument).delete()
    messages.success(
        request,
        f"« {instrument.name} » retiré de « {account.name} » ({count} opération{'s' if count > 1 else ''} supprimée{'s' if count > 1 else ''}).",
    )
    return redirect(_next_url(request, _portfolio_url(account)))


@login_required
@require_POST
def edit_instrument(request, pk):
    instrument = get_object_or_404(Instrument, pk=pk, user=request.user)
    form = InstrumentForm(request.POST, instance=instrument, prefix=f"i{pk}")
    if form.is_valid():
        form.save()
        messages.success(request, f"« {instrument.name} » mis à jour.")
    else:
        messages.error(request, f"Impossible de mettre à jour le titre. {_form_errors(form)}")
    return redirect(_next_url(request, reverse("finance:savings_portfolio")))


# ============================================================
# YAHOO FINANCE (JSON)
# ============================================================

@login_required
@require_GET
def search_instruments(request):
    try:
        results = market_data.search_instruments(request.GET.get("q", ""))
    except market_data.MarketDataError as error:
        return JsonResponse({"results": [], "error": str(error)}, status=502)
    return JsonResponse({"results": results})


@login_required
@require_GET
def instrument_price(request):
    symbol = (request.GET.get("symbol") or "").strip()
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return JsonResponse({"ok": False, "error": "Date invalide."}, status=400)
    if not symbol or day > date.today():
        return JsonResponse({"ok": False, "error": "Titre ou date invalide."}, status=400)
    try:
        price = market_data.price_on(symbol, day)
    except market_data.MarketDataError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=502)
    return JsonResponse({
        "ok": True,
        "date": price["date"].isoformat(),
        "close": str(price["close"]),
        "currency": price["currency"],
        "eur": str(price["eur"].quantize(Decimal("0.01")) if price["eur"] >= 1 else price["eur"]),
    })


@login_required
@require_POST
def refresh_prices(request):
    updated, errors = market_data.refresh_prices(request.user, force=request.POST.get("force") == "1")
    return JsonResponse({"ok": not errors, "updated": updated, "errors": errors})
