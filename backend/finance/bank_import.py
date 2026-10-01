"""
Import des opérations bancaires (export Crédit Agricole) en Transactions.

Règles :
  - seuls les comptes courants sont importés (les livrets servent d'épargne) ;
  - virement du compte courant vers un compte du titulaire -> dépense
    "Épargne & placements › Livret" ; virement dans l'autre sens -> ignoré ;
  - chaque opération a une empreinte (external_id) : resynchroniser un mois
    ne crée pas de doublons ;
  - la catégorie est devinée par mots-clés (BANK_CATEGORY_RULES), en attendant
    un modèle de classification.
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

from django.db import IntegrityError, transaction as db_transaction

from .ca_csv import normalize_name
from .models import (
    Category,
    EntryType,
    MonthlyBudget,
    Subcategory,
    Transaction,
    TransactionSource,
)


SAVINGS_ACCOUNT_RE = re.compile(r"livret|ldd|lep|pel\b|cel\b|[ée]pargne", re.I)

SAVINGS_CATEGORY = ("Épargne & placements", "Livret")

# (motif recherché dans "type + libellé", catégorie, sous-catégorie)
# Le premier motif qui correspond l'emporte.
BANK_CATEGORY_RULES = {
    EntryType.EXPENSE: [
        (r"cotisation|frais|commission|agios", "Impôts, taxes & frais", "Frais bancaires"),
        (r"retrait", "Retraits", None),
        (r"anthropic|claude|openai|chatgpt|perplexity|mistral ?ai|copilot|github|notion|cursor|midjourney|adobe|microsoft|office ?365", "Abonnements & services", "Outils IA & logiciels"),
        (r"netflix|disney|canal\+|prime video|youtube|paramount|crunchyroll|molotov|\bmax\b", "Abonnements & services", "Streaming vidéo"),
        (r"spotify|deezer|apple music|tidal|qobuz", "Abonnements & services", "Musique"),
        (r"icloud|apple\.com|google one|google storage|dropbox", "Abonnements & services", "Cloud & stockage"),
        (r"playstation|xbox|nintendo|steam|game ?pass", "Abonnements & services", "Jeux vidéo"),
        (r"wellpass|egym|basic.?fit|fitness|aquazena|piscine|decathlon|sport", "Loisirs & vacances", "Sport"),
        (r"cinema|cinéma|ugc|pathe|pathé|fnac spectacles|musee|musée|theatre|théâtre", "Loisirs & vacances", "Sorties culturelles"),
        (r"coiffeur|barber|esthetique|esthétique", "Loisirs & vacances", "Coiffeur, esthétique"),
        (r"bar\b|pub\b|club", "Loisirs & vacances", "Bars et clubs"),
        (r"carrefour|monoprix|franprix|auchan|leclerc|lidl|intermarche|intermarché|casino|picard|g20|super u|biocoop|naturalia|epicerie|épicerie", "Alimentation & restaurants", "Supermarché, épicerie"),
        (r"boulang|marche|marché|primeur", "Alimentation & restaurants", "Marché"),
        (r"restaurant|burger|pizz|sushi|kebab|coffee|cafe|café|boucher|brasserie|bistro|mcdo|mcdonald|kfc|uber ?eats|deliveroo|yumy|mari mari", "Alimentation & restaurants", "Restaurants, snacks"),
        (r"uber|bolt|heetch|taxi|g7", "Transport", "Taxi, VTC"),
        (r"ratp|navigo|sncf|transilien|velib|vélib|\blime\b", "Transport", "Transport en commun"),
        (r"edf|engie|total ?energies|eau de paris|veolia", "Logement & charges", "Eau, électricité, gaz"),
        (r"total access|esso|shell|\bbp\b|station.service", "Transport", "Carburant"),
        (r"air france|easyjet|ryanair|ouigo|trainline|blablacar", "Transport", "Avion, train, bateau"),
        (r"pharmacie", "Santé", "Pharmacie"),
        (r"docteur|medecin|médecin|doctolib|dentiste|kine|kiné", "Santé", "Médecin"),
        (r"mutuelle", "Santé", "Mutuelle"),
        (r"loyer", "Logement & charges", "Loyer"),
        (r"\bfree\b|orange|sfr|bouygues|sosh|red by", "Logement & charges", "Internet, téléphonie"),
        (r"amazon|zalando|zara|h&m|uniqlo|vinted", "Achats & shopping", "Vêtements, chaussures, accessoires"),
        (r"impot|impôt|dgfip|tresor public|trésor public", "Impôts, taxes & frais", "Impôts sur le revenu"),
    ],
    EntryType.INCOME: [
        (r"caf\b|caisse d.allocations|pole emploi|pôle emploi|france travail", "Aides (CAF et autres)", None),
        (r"salaire|paie|payroll", "Salaire", None),
        (r"rembours|avoir|refund", "Remboursements", None),
    ],
}


@dataclass
class ImportResult:
    created: int = 0
    skipped: int = 0


def operation_fingerprint(op, occurrence):
    """
    Empreinte stable d'une opération. `occurrence` distingue deux opérations
    identiques le même jour (ex. deux cafés au même prix).
    """

    key = "|".join([
        op.account_number or "",
        op.booking_date.isoformat(),
        op.kind,
        op.label,
        str(op.amount),
        str(occurrence),
    ])

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def is_internal_transfer(op):

    return "virement" in op.kind.lower() and normalize_name(op.merchant) == op.holder


def guess_category(user, entry_type, text, categories_cache):
    """
    Renvoie (Category, Subcategory) d'après les règles, ou (None, None).
    """

    for pattern, category_name, subcategory_name in BANK_CATEGORY_RULES[entry_type]:

        if re.search(pattern, text, re.I):
            return resolve_category(user, entry_type, category_name, subcategory_name, categories_cache)

    return None, None


def resolve_category(user, entry_type, category_name, subcategory_name, categories_cache):
    """
    Retrouve la catégorie de l'utilisateur par son nom (les catégories sont
    propres à chaque utilisateur et peuvent avoir été supprimées).
    """

    cache_key = (entry_type, category_name, subcategory_name)

    if cache_key not in categories_cache:

        category = Category.objects.filter(
            user=user, entry_type=entry_type, name=category_name
        ).first()

        subcategory = None
        if category and subcategory_name:
            subcategory = Subcategory.objects.filter(
                category=category, name=subcategory_name
            ).first()

        categories_cache[cache_key] = (category, subcategory)

    return categories_cache[cache_key]


def import_operations(user, operations):
    """
    Crée les Transactions correspondant aux opérations bancaires.
    """

    result = ImportResult()
    occurrences = Counter()
    categories_cache = {}
    budgets = {}

    for op in operations:

        signature = (op.account_number, op.booking_date, op.kind, op.label, op.amount)
        occurrences[signature] += 1
        external_id = operation_fingerprint(op, occurrences[signature])

        if SAVINGS_ACCOUNT_RE.search(op.account or ""):
            continue

        entry_type = EntryType.INCOME if op.amount > 0 else EntryType.EXPENSE
        category = subcategory = None

        if is_internal_transfer(op):

            # Retour d'argent depuis son propre livret : ni revenu ni dépense
            if entry_type == EntryType.INCOME:
                continue

            category, subcategory = resolve_category(
                user, EntryType.EXPENSE, *SAVINGS_CATEGORY, categories_cache
            )
            description = "Virement vers l'épargne"

        else:

            category, subcategory = guess_category(
                user, entry_type, f"{op.kind} {op.label}", categories_cache
            )
            description = op.merchant

        if Transaction.objects.filter(user=user, external_id=external_id).exists():
            result.skipped += 1
            continue

        month_key = (op.operation_date.year, op.operation_date.month)
        if month_key not in budgets:
            budgets[month_key] = MonthlyBudget.objects.filter(
                user=user, year=month_key[0], month=month_key[1]
            ).first()

        try:
            with db_transaction.atomic():
                Transaction.objects.create(
                    user=user,
                    monthly_budget=budgets[month_key],
                    transaction_type=entry_type,
                    amount=abs(op.amount),
                    description=description[:255],
                    category=category,
                    subcategory=subcategory,
                    transaction_date=op.operation_date,
                    source=TransactionSource.BANK,
                    external_id=external_id,
                    bank_label=f"{op.kind} — {op.label}"[:255],
                )
        except IntegrityError:
            # Même opération importée en parallèle
            result.skipped += 1
            continue

        result.created += 1

    return result
