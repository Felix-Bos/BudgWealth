"""
Lecture de l'export CSV "Télécharger mes opérations" du Crédit Agricole.

Le fichier brut n'est pas un CSV à plat :
  - encodage Windows-1252, séparateur ";", montants "1 706,83" ;
  - un bloc par compte (titulaire, "<type> carte n° <numéro>", "Solde au ...") ;
  - des libellés sur plusieurs lignes :
        Paiement par carte
        X5982 FILS DU BOUCHERS CLI 20/09 - Les Fils Du Bouchers

Utilisation en ligne de commande (produit un CSV lisible, une ligne par opération) :
    python backend/finance/ca_csv.py chemin/vers/ca_operations_XXX.csv
"""

import csv
import io
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


# "Compte de Dépôt carte n° 111…", "Livret A carte n° 222…" ; « carte » peut manquer
ACCOUNT_RE = re.compile(r"^(?P<type>.*?)\s+(?:carte\s+)?n°?\s*(?P<number>\d{6,})")
BALANCE_RE = re.compile(r"^Solde au (?P<date>\d{2}/\d{2}/\d{4}) (?P<amount>-?[\d\s\xa0]+,\d{2})")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# "X5982 FILS DU BOUCHERS CLI 20/09 - Les Fils Du Bouchers"
CARD_RE = re.compile(
    r"^X\d{4}\s+(?P<raw>.*?)\s+(?P<day>\d{2})/(?P<month>\d{2})(?:\s+-\s+(?P<merchant>.+))?$"
)


@dataclass
class Operation:
    account: str            # "Compte de Dépôt", "Livret A"...
    account_number: str
    holder: str             # titulaire du compte, ex. "M. BOS FELIX"
    booking_date: date      # date d'enregistrement par la banque
    operation_date: date    # date d'achat pour les cartes, sinon = booking_date
    kind: str               # "Paiement par carte", "Virement émis"...
    label: str              # détail du libellé
    merchant: str           # nom lisible du commerçant / de la contrepartie
    amount: Decimal         # négatif = débit, positif = crédit


@dataclass
class AccountBalance:
    account: str
    account_number: str
    balance_date: date
    balance: Decimal


# Carte de compte du formulaire « Télécharger mes opérations » :
#   M. BOS FELIX / Livret Jeune Mozaic / N°12345678901 / Dernier téléchargement le 30/10/2026
CARD_NUMBER_RE = re.compile(r"^N°\s*(?P<number>[\d ]{6,})$", re.I)


@dataclass
class AccountCard:
    account: str
    account_number: str


def parse_account_card(text):
    """
    Libellé et numéro d'un compte d'après sa carte (le libellé est la ligne
    juste avant « N°… »). La carte n'affiche pas le solde.
    Renvoie None si aucun numéro n'est lisible.
    """

    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        match = CARD_NUMBER_RE.match(line)
        if match and index > 0:
            return AccountCard(account=lines[index - 1], account_number=re.sub(r"\D", "", match["number"]))
    return None


def parse_amount(text):

    text = text.replace(" ", "").replace("\xa0", "").replace("\u202f", "").replace(",", ".")

    return Decimal(text) if text else None


def normalize_name(text):

    return re.sub(r"\s+", " ", text or "").strip().upper()


def parse_date(text):

    return datetime.strptime(text, "%d/%m/%Y").date()


def card_operation_date(day, month, booking_date):
    """
    Le libellé carte ne donne que JJ/MM : l'année est celle de l'enregistrement,
    sauf achat de décembre enregistré en janvier.
    """

    year = booking_date.year

    if month > booking_date.month:
        year -= 1

    return date(year, month, day)


def split_label(kind, detail, booking_date):
    """
    Renvoie (date d'opération, nom lisible) à partir du détail du libellé.
    """

    card = CARD_RE.match(detail)

    if kind == "Paiement par carte" and card:

        operation_date = card_operation_date(
            int(card["day"]), int(card["month"]), booking_date
        )
        merchant = card["merchant"] or re.sub(r"\s+", " ", card["raw"]).strip()

        return operation_date, merchant

    # Virements : la contrepartie est avant " - ", la suite est une référence
    #   "VIR INST de BEATRICE ANNIBAL - Virement de ..." -> "BEATRICE ANNIBAL"
    #   "CAF DES HAUTS DE SEINE 46046018V - 8624116X..." -> "CAF DES HAUTS DE SEINE"
    #   "DE M.       BOS FELIX" / "WEB M. BOS FELIX"     -> "M. BOS FELIX"
    name = detail.split(" - ", 1)[0]
    name = re.sub(r"^(VIR INST de|VIR de|DE|WEB)\s+", "", name, flags=re.I)
    name = " ".join(word for word in name.split() if not re.search(r"\d", word))

    return booking_date, name or re.sub(r"\s+", " ", detail).strip()


def parse_ca_csv(path):
    """
    Lit un export Crédit Agricole et renvoie (opérations, soldes).
    """

    raw = Path(path).read_text(encoding="cp1252")

    operations = []
    balances = []
    account = account_number = holder = None
    last_single_cell = None

    for row in csv.reader(io.StringIO(raw), delimiter=";"):

        if not row:
            continue

        first = row[0].strip()

        header = ACCOUNT_RE.match(first)
        if header:
            account = header["type"].strip()
            account_number = header["number"]
            # Le titulaire est sur la ligne juste avant l'en-tête du compte
            holder = normalize_name(last_single_cell)
            continue

        if len([cell for cell in row if cell.strip()]) == 1:
            last_single_cell = first

        balance = BALANCE_RE.match(first)
        if balance:
            balances.append(AccountBalance(
                account=account,
                account_number=account_number,
                balance_date=parse_date(balance["date"]),
                balance=parse_amount(balance["amount"]),
            ))
            continue

        if len(row) < 4 or not DATE_RE.match(first):
            continue

        booking_date = parse_date(first)

        lines = [line.strip() for line in row[1].splitlines() if line.strip()]
        kind = lines[0] if lines else ""
        detail = " | ".join(lines[1:])

        debit = parse_amount(row[2])
        credit = parse_amount(row[3])
        amount = credit if credit is not None else -debit

        operation_date, merchant = split_label(kind, lines[1] if len(lines) > 1 else "", booking_date)

        operations.append(Operation(
            account=account,
            account_number=account_number,
            holder=holder,
            booking_date=booking_date,
            operation_date=operation_date,
            kind=kind,
            label=detail,
            merchant=merchant,
            amount=amount,
        ))

    return operations, balances


def mask_digits(text):
    """Chiffres remplacés par 9 : garde la forme d'un numéro ou d'un montant, pas sa valeur."""
    return re.sub(r"\d", "9", text or "")


def outline_ca_csv(path, limit=80):
    """
    Lignes du relevé hors opérations (titulaire, en-têtes de comptes,
    soldes, titres), chiffres masqués : pour voir comment la banque
    présente chaque compte sans exposer de numéro ni de montant.
    """

    raw = Path(path).read_text(encoding="cp1252")
    lines = []
    for row in csv.reader(io.StringIO(raw), delimiter=";"):
        cells = [cell.strip() for cell in row if cell.strip()]
        if not cells or DATE_RE.match(cells[0]) or cells[0] == "Date":
            continue
        lines.append(mask_digits(" | ".join(" ".join(cell.split()) for cell in cells)))
        if len(lines) >= limit:
            break
    return lines


def write_clean_csv(operations, path):
    """
    CSV lisible dans Excel / Numbers / VS Code : une ligne par opération.
    UTF-8 avec BOM et virgule décimale pour qu'Excel (FR) l'ouvre correctement.
    """

    with open(path, "w", encoding="utf-8-sig", newline="") as file:

        writer = csv.writer(file, delimiter=";")
        writer.writerow([
            "Compte", "Date opération", "Date banque", "Type",
            "Commerçant / contrepartie", "Montant", "Libellé complet",
        ])

        for op in operations:
            writer.writerow([
                op.account,
                op.operation_date.strftime("%d/%m/%Y"),
                op.booking_date.strftime("%d/%m/%Y"),
                op.kind,
                op.merchant,
                str(op.amount).replace(".", ","),
                op.label,
            ])


def main():

    source = Path(sys.argv[1])
    target = source.with_name(source.stem + "_propre.csv")

    operations, balances = parse_ca_csv(source)
    write_clean_csv(operations, target)

    for b in balances:
        print(f"{b.account or '?':<22} solde au {b.balance_date:%d/%m/%Y} : {b.balance:>10} €")

    print(f"\n{len(operations)} opérations -> {target}")


if __name__ == "__main__":
    main()
