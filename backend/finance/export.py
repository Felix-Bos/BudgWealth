"""
Export des données de l'utilisateur : opérations en CSV (ouvrable dans
Excel, séparateur « ; » et virgule décimale à la française) ou classeur
Excel complet (opérations, épargne, portefeuille, budget, catégories).
"""

import csv
import io
from decimal import Decimal

from django.utils.translation import gettext as _

from .models import (
    BalanceSnapshot,
    Category,
    PortfolioOperation,
    SavingsAccount,
    Transaction,
)


def _transactions(user):
    return (
        Transaction.objects.filter(user=user)
        .select_related("category", "subcategory", "savings_project")
        .order_by("transaction_date", "id")
    )


def _transaction_row(t):
    sign = 1 if t.transaction_type == "INCOME" else -1
    return [
        t.transaction_date,
        t.actual_date or t.transaction_date,
        t.get_transaction_type_display(),
        str(t.category) if t.category else "",
        str(t.subcategory) if t.subcategory else "",
        t.description,
        t.bank_label,
        sign * t.amount,
        t.get_source_display(),
        _("Oui") if t.paid_with_meal_voucher else "",
        t.savings_project.name if t.savings_project else "",
    ]


def transaction_headers():
    return [
        _("Date (budget)"), _("Date réelle"), _("Type"), _("Catégorie"), _("Sous-catégorie"),
        _("Libellé"), _("Libellé bancaire"), _("Montant (€)"), _("Source"), _("Tickets restaurant"), _("Projet"),
    ]


def transactions_csv(user):
    """CSV des opérations, en UTF-8 avec BOM pour Excel."""

    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(transaction_headers())
    for t in _transactions(user):
        row = _transaction_row(t)
        writer.writerow([
            value.isoformat() if hasattr(value, "isoformat")
            else (str(value).replace(".", ",") if isinstance(value, Decimal) else value)
            for value in row
        ])
    return buffer.getvalue().encode("utf-8")


def workbook(user):
    """Classeur Excel (bytes) avec une feuille par type de données."""

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F6F54")

    def sheet(title, headers, rows, money_columns=(), first=False):
        ws = book.active if first else book.create_sheet()
        ws.title = title[:31]
        ws.append(headers)
        for cell in ws[1]:
            cell.font, cell.fill = header_font, header_fill
        for row in rows:
            ws.append([float(v) if isinstance(v, Decimal) else v for v in row])
        for column in money_columns:
            for cell in ws.iter_cols(min_col=column, max_col=column, min_row=2):
                for c in cell:
                    c.number_format = '#,##0.00 "€"'
        for index, header in enumerate(headers, start=1):
            values = [len(str(header))] + [len(str(c.value or "")) for c in ws[get_column_letter(index)][1:200]]
            ws.column_dimensions[get_column_letter(index)].width = min(48, max(values) + 2)
        ws.freeze_panes = "A2"
        return ws

    sheet(_("Opérations"), transaction_headers(), (_transaction_row(t) for t in _transactions(user)), (8,), first=True)

    accounts = SavingsAccount.objects.filter(user=user).order_by("order", "name")
    sheet(
        _("Comptes épargne"),
        [_("Compte"), _("Type"), _("Établissement"), _("Taux (%)"), _("Plafond (€)"), _("Ouvert le"), _("Liquidités (€)")],
        ([a.name, a.get_kind_display(), a.institution, a.interest_rate, a.ceiling, a.opened_on, a.cash_balance] for a in accounts),
        (5, 7),
    )
    snapshots = BalanceSnapshot.objects.filter(account__user=user).select_related("account").order_by("account__name", "date")
    sheet(
        _("Soldes des livrets"),
        [_("Compte"), _("Date"), _("Solde (€)"), _("Source")],
        ([s.account.name, s.date, s.balance, s.get_source_display()] for s in snapshots),
        (3,),
    )
    operations = PortfolioOperation.objects.filter(account__user=user).select_related("account", "instrument").order_by("date", "id")
    sheet(
        _("Portefeuille"),
        [_("Compte"), _("Date"), _("Opération"), _("Titre"), _("Symbole"), _("Quantité"), _("Prix unitaire"), _("Frais (€)"), _("Montant (€)"), _("Note")],
        ([o.account.name, o.date, o.get_kind_display(), o.instrument.name if o.instrument else "",
          o.instrument.symbol if o.instrument else "", o.quantity, o.unit_price, o.fees, o.amount, o.note] for o in operations),
        (8, 9),
    )
    plans = user.budget_plans.prefetch_related("lines__category", "lines__subcategory").order_by("-valid_from")
    sheet(
        _("Budget"),
        [_("À partir de"), _("Section"), _("Catégorie"), _("Sous-catégorie"), _("Libellé"), _("Montant / mois (€)")],
        ([plan.valid_from, line.get_section_display(), str(line.category), str(line.subcategory or ""), line.label, line.amount]
         for plan in plans for line in plan.lines.all()),
        (6,),
    )
    def category_rows():
        for category in Category.objects.filter(user=user).prefetch_related("subcategories"):
            subcategories = list(category.subcategories.all())
            if not subcategories:
                section = category.get_budget_section_display() if category.entry_type == "EXPENSE" else ""
                yield [category.get_entry_type_display(), str(category), "", section]
            for sub in subcategories:
                yield [category.get_entry_type_display(), str(category), str(sub), sub.get_budget_section_display()]

    sheet(_("Catégories"), [_("Type"), _("Catégorie"), _("Sous-catégorie"), _("Type dans le budget")], category_rows())

    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


